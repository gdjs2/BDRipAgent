import pytest
from sqlalchemy import select

from agent.schemas import Selection, validate_selection
from shared.config import ScreenshotPolicy
from shared.db import session
from shared.models import MovieJob, Screenshot, Task
from shared.naming import audio_channels, language_name, release_audio_token, release_name, track_name
from shared.screenshot_rules import check_other_variants, conflicts, other_variant_frames
from tests.test_agent import fixture_selection
from worker.adapters.media import validate_sdr_progressive


def audio(codec, codec_id, **extra):
    return {
        "kind": "audio",
        "codec": codec,
        "codec_id": codec_id,
        "language": "eng",
        "channels": 6,
        "channel_layout": "L R C LFE Ls Rs",
        **extra,
    }


@pytest.mark.parametrize(
    "code,expected",
    [
        ("eng", "English"),
        ("en-US", "English"),
        ("jpn", "Japanese"),
        ("fre", "French"),
        ("chi", "Chinese"),
        ("und", "Undetermined"),
    ],
)
def test_readable_languages(code, expected):
    assert language_name(code) == expected


@pytest.mark.parametrize(
    "track,expected",
    [
        (audio("DTS", "A_DTS"), "English DTS 5.1"),
        (audio("DTS", "A_DTS", format_features="XLL"), "English DTS-MA 5.1"),
        (audio("DTS-HD Master Audio", "A_DTS", channels=8, channel_layout="7.1"), "English DTS-MA 7.1"),
        (audio("AC-3", "A_AC3"), "English Dolby Digital 5.1"),
        (audio("E-AC-3", "A_EAC3"), "English Dolby Digital Plus 5.1"),
        (
            audio("TrueHD", "A_TRUEHD", commercial_name="Dolby TrueHD with Dolby Atmos"),
            "English Dolby Atmos 5.1",
        ),
        (audio("PCM", "A_PCM/INT/LIT", channels=2, channel_layout="stereo"), "English LPCM 2.0"),
    ],
)
def test_audio_track_labels(track, expected):
    assert track_name(track) == expected


def test_channel_layout_distinguishes_six_full_range_channels():
    assert audio_channels(audio("FLAC", "A_FLAC", channel_layout="L R C Lb Rb Cs")) == "6.0"
    assert audio_channels(audio("FLAC", "A_FLAC", channel_layout="5.1(side)")) == "5.1"


@pytest.mark.parametrize("codec,fmt", [("S_HDMV/PGS", "PGS"), ("S_TEXT/UTF8", "SRT"), ("S_TEXT/ASS", "ASS")])
def test_subtitle_optional_flags_are_omitted_unless_set(codec, fmt):
    track = {"kind": "subtitles", "codec_id": codec, "codec": fmt, "language": "eng"}
    assert track_name(track) == f"English {fmt}"
    assert track_name({**track, "hearing_impaired": True, "forced": True}) == f"English {fmt} SDH Forced"


def test_release_naming_matches_upstream_core_first_and_codec_rules():
    dd = audio("AC-3", "A_AC3")
    ma = audio("DTS-HD Master Audio", "A_DTS", channels=8, channel_layout="7.1")
    assert (
        release_name("No Other Choice", 2025, "x264", [dd]) == "No.Other.Choice.2025.1080p.BluRay.x264-WiKi"
    )
    assert (
        release_name("Amélie: A Film", 2001, "x265", [ma])
        == "Amelie.A.Film.2001.1080p.BluRay.x265.10bit.DTS.MA7.1-WiKi"
    )
    assert release_audio_token([ma, dd]) == ""
    assert release_audio_token([audio("DTS", "A_DTS")]) == "DTS"
    assert (
        release_audio_token([audio("TrueHD", "A_TRUEHD", commercial_name="Dolby TrueHD with Dolby Atmos")])
        == "Atmos.TrueHD.5.1"
    )
    with pytest.raises(ValueError):
        release_name("Movie", None, "x264")
    with pytest.raises(ValueError):
        release_name("电影", 2025, "x264")


@pytest.mark.parametrize(
    "metadata",
    [
        {"color_transfer": "smpte2084"},
        {"color_transfer": "arib-std-b67"},
        {"side_data_list": [{"side_data_type": "DOVI configuration record"}]},
        {"side_data_list": [{"side_data_type": "Mastering display metadata"}]},
        {"field_order": "tt"},
        {"field_order": "bb"},
        {"field_order": "tb"},
    ],
)
def test_hdr_and_interlaced_are_rejected(metadata):
    with pytest.raises(ValueError):
        validate_sdr_progressive(metadata)
    validate_sdr_progressive({"color_transfer": "bt709", "field_order": "progressive"})


def test_default_selection_is_seven_and_requires_characters():
    policy = ScreenshotPolicy()
    assert (policy.count, policy.representative, policy.encode_challenging) == (7, 4, 3)
    candidates, choices = fixture_selection()
    for choice in choices:
        choice["character_visible"] = False
    errors = validate_selection(
        Selection(selected=choices),
        candidates,
        ScreenshotPolicy(count=4, representative=2, encode_challenging=2),
        1000,
    )
    assert any("visibly feature characters" in e for e in errors)


def create_pair_fixture(client, environment):
    (environment.source_root / "Movie.mkv").write_bytes(b"test")
    payload = {
        "source_path": "Movie.mkv",
        "title": "Movie",
        "year": 2025,
        "analysis_profile": "x264-live",
        "second_profile": "x265-live",
    }
    bad = client.post("/api/jobs/pair", json={**payload, "second_profile": "x264-live"})
    assert bad.status_code == 409
    assert client.get("/api/jobs").json() == []
    response = client.post("/api/jobs/pair", json=payload)
    assert response.status_code == 201, response.text
    jobs = response.json()
    assert {j["analysis_profile"] for j in jobs} == {"x264-live", "x265-live"}
    assert all(j["state"] == "ANALYZING_SOURCE" for j in jobs)
    assert all(j["screenshot_policy"]["count"] == 7 for j in jobs)
    assert jobs[0]["release_name"].endswith("x264-WiKi")
    return jobs


def test_cross_codec_reservations_and_manual_override_cannot_overlap(client, environment):
    jobs = create_pair_fixture(client, environment)
    with session() as db:
        first, second = (db.get(MovieJob, j["id"]) for j in jobs)
        db.add(
            Screenshot(
                job_id=first.id,
                candidate_id=1,
                selected=True,
                info={"source_frame_number": 2400, "timeline_seconds": 100, "scene_id": 1},
            )
        )
        second.state = "COMPLETE"
        second.validation = {"metrics": {"source_duration": 400}}
        second.screenshot_policy = {**second.screenshot_policy, "min_timeline_bins": 1}
        for task in db.scalars(select(Task).where(Task.job_id == second.id)):
            task.status = "SUCCEEDED"
        old = Screenshot(
            job_id=second.id,
            candidate_id=1,
            selected=True,
            info={"source_frame_number": 4800, "timeline_seconds": 200, "scene_id": 2},
        )
        new = Screenshot(
            job_id=second.id,
            candidate_id=2,
            selected=False,
            info={"source_frame_number": 2640, "timeline_seconds": 110, "scene_id": 1},
        )
        db.add_all([old, new])
        db.flush()
        reservations = other_variant_frames(db, second)
        assert conflicts(new.info, reservations)
        assert not conflicts(old.info, reservations)
        assert conflicts({**old.info, "source_frame_number": 2400}, reservations)
        assert not conflicts({**old.info, "timeline_seconds": 130}, reservations)
        with pytest.raises(ValueError, match="other codec"):
            check_other_variants(db, second, [new.info])
        old_id = old.id
        db.commit()
    result = client.post(f"/api/jobs/{jobs[1]['id']}/screenshots/{old_id}/replace", json={"candidate_id": 2})
    assert result.status_code == 409, result.text
    with session() as db:
        assert db.get(Screenshot, old_id).selected
    # Changing file identity means a different movie version and no reservations.
    with session() as db:
        second = db.get(MovieJob, jobs[1]["id"])
        second.source_mtime_ns = "different"
        assert other_variant_frames(db, second) == []


def test_pair_creation_is_atomic_and_keeps_both_human_gates(client, environment):
    create_pair_fixture(client, environment)
