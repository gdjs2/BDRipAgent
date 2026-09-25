from types import SimpleNamespace

import pytest

from shared.original_languages import is_original, movie_languages
from tests.test_source_choices import CHOICES, read, ready, save
from tests.test_source_sharing import peer


@pytest.mark.parametrize(
    "actual,original,expected",
    [
        ("cmn", "zh", True),
        ("zh-Hans", "cmn", True),
        ("eng", "en", True),
        ("en-GB", "eng", True),
        ("fr", "en", False),
        ("zho", "zh-Hans", True),
        ("zh-Hant", "zh-Hans", True),
        ("yue-Hant", "zh", False),
        ("zh-Hans", "yue", False),
        ("jpn", "ja", True),
        ("fra", "fre", True),
        ("und", "en", False),
        ("mul", "en", False),
        (None, "en", False),
    ],
)
def test_matching(actual, original, expected):
    assert is_original({"kind": "audio", "language": actual}, [original]) is expected
    assert is_original({"kind": "subtitles", "language": actual}, [original]) is expected


def test_video_and_unknown_movie_language():
    assert is_original({"kind": "video", "language": "und"}, [])
    assert not is_original({"kind": "audio", "language": "en", "original": True}, [])
    assert is_original({"kind": "audio", "language": "ko"}, ["en", "kor"])


def test_shared_metadata_precedence_and_discovery_fallback():
    analysis = {"subtitle_discovery_policy": {"original_languages": ["en"]}}
    assert movie_languages(analysis, {"original_languages": ["jpn"]}) == ["ja"]
    assert movie_languages(analysis) == ["en"]
    assert movie_languages({}, {"subtitle_discovery": {"original_languages": ["kor"]}}) == ["ko"]
    assert movie_languages(
        {"original_languages": [], "subtitle_discovery": {"original_languages": ["fr"]}}
    ) == ["fr"]


def test_original_language_choices_shared_and_track_code_override(client, new_job):
    other = peer(client)
    for job in (new_job, other):
        ready(job["id"])
    response = save(
        client,
        new_job["id"],
        {
            **CHOICES,
            "original_languages": ["eng", "en"],
            "track_languages": {"9": "fr"},
        },
    )
    assert response.status_code == 200, response.text
    for job in (new_job, other):
        current = read(client, job["id"])
        assert current["original_languages"] == [{"code": "en", "name": "English"}]
        assert current["analysis"]["original_languages"] == ["en"]
        tracks = {t["track_id"]: t["info"] for t in current["tracks"]}
        assert tracks[4]["original"] and tracks[8]["original"] and tracks[12]["original"]
        assert not tracks[9]["original"]
    bad = save(client, new_job["id"], {**CHOICES, "original_languages": ["und"]})
    assert bad.status_code == 422


def test_mux_command_explicit_flags(tmp_path):
    from worker.pipeline.stages import mux_command

    for name in ("source.mkv", "video.mkv", "en.ac3", "fr.ac3", "en.sup"):
        (tmp_path / name).write_bytes(b"fixture")
    tracks = [
        {
            "kind": kind,
            "path": path,
            "language": code,
            "name_override": code,
            "default": False,
            "forced": False,
            "original": True,
        }
        for kind, path, code in [
            ("audio", "en.ac3", "eng"),
            ("audio", "fr.ac3", "fr"),
            ("subtitles", "en.sup", "en-US"),
        ]
    ]
    ctx = SimpleNamespace(
        source=lambda: tmp_path / "source.mkv",
        workspace=tmp_path,
        settings=SimpleNamespace(mkvmerge_bin="mkvmerge"),
        job=SimpleNamespace(
            title="Movie",
            year=2026,
            analysis={"encoded_path": "video.mkv", "prepared_tracks": tracks, "original_languages": ["en"]},
            validation={"metrics": {"source_first_pts": 0, "encoded_first_pts": 0}},
        ),
    )
    command = mux_command(ctx, tmp_path / "final.mkv")
    assert [command[i + 1] for i, value in enumerate(command) if value == "--original-flag"] == [
        "0:1",
        "0:1",
        "0:0",
        "0:1",
    ]


def test_imdb_language_codes_normalized_without_assuming_audio_language():
    from backend.app.movie_metadata import normalize_movie

    metadata = normalize_movie(
        "tt1234567",
        {
            "imdb_id": "tt1234567",
            "year": 2025,
            "title": "Film",
            "original_languages": ["spa", "es", "ase", "und", "not-a-language"],
        },
    )
    assert metadata["original_languages"] == ["es", "ase"]
    assert movie_languages({}, metadata=metadata) == ["es", "ase"]
    assert movie_languages({"original_languages": ["fr"]}, metadata=metadata) == ["fr"]
