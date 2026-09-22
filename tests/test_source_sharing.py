import json
import shutil
from types import SimpleNamespace

from shared.db import session
from shared.models import MovieJob, MovieTrack
from shared.paths import contained, job_dir
from tests.test_track_review import agent_answer, fixture_tracks
from worker.adapters.track_cache import SharedTrackAnalysis


def peer(client):
    response = client.post(
        "/api/jobs",
        json={"source_path": "Movie.mkv", "title": "Movie", "year": 2026, "analysis_profile": "x264-live"},
    )
    assert response.status_code == 201
    return response.json()


def context(settings, job_id):
    with session() as db:
        job = db.get(MovieJob, job_id)
    workspace = job_dir(settings.workspace_root, job.id)
    artifacts = []

    def artifact(path, kind, **kwargs):
        artifacts.append((path, kind))
        return str(path.relative_to(workspace))

    return SimpleNamespace(
        settings=settings,
        job=job,
        workspace=workspace,
        source=lambda: contained(settings.source_root, job.source_path, exists=True),
        check=lambda: None,
        progress=lambda *a, **kw: None,
        log=lambda *a: None,
        artifact=artifact,
        artifacts=artifacts,
    )


def result(ctx):
    tracks = fixture_tracks()
    answer = agent_answer(tracks)
    for track, reviewed in zip(tracks, answer["tracks"], strict=True):
        track["track_review"] = {"schema_version": 1, **reviewed, "report": "review.json"}
        if track["kind"] == "audio":
            track["audio_analysis"] = {
                "samples": [{"id": 1, "path": "sample.wav", "segments": [{"text": "Dialogue"}]}]
            }
        elif track["codec_id"] == "S_HDMV/PGS":
            track["subtitle_detection"] = {
                "schema_version": 2,
                "hearing_impaired": False,
                "report": "subtitle.json",
            }
    for filename in ("sample.wav", "review.json", "subtitle.json"):
        (ctx.workspace / filename).write_text("evidence")
    return {
        **ctx.job.analysis,
        "tracks": tracks,
        "subtitle_analysis_version": 2,
        "track_review_version": 1,
        "audio_review_round": 2,
        "audio_review_next": None,
        "audio_comparison": {**answer["audio_comparison"], "status": "resolved"},
    }


def test_cache_is_portable_and_does_not_share_human_track_choices(client, new_job, environment):
    donor = context(environment, new_job["id"])
    recipient = context(environment, peer(client)["id"])
    value = result(donor)
    value["tracks"][0].update(
        name_override="Custom donor label", default=True, flag_overrides={"default": True}
    )
    value["release_details"] = {"chinese_name": "not analysis"}
    cache = SharedTrackAnalysis(donor)
    cache.store(value)
    shutil.rmtree(donor.workspace)
    base = {**recipient.job.analysis, "tracks": fixture_tracks(), "encoded_path": "own-video.mkv"}
    base["tracks"][1]["name_override"] = "My subtitle name"
    base["tracks"][1]["flag_overrides"] = {"forced": True}
    restored = SharedTrackAnalysis(recipient).restore(base)
    assert restored["shared_track_analysis"]["reused"]
    assert restored["tracks"][0]["default"] is False
    assert "name_override" not in restored["tracks"][0]
    assert restored["tracks"][1]["mux_name"] == "My subtitle name"
    assert restored["tracks"][1]["forced"] is True
    assert restored["encoded_path"] == "own-video.mkv" and "release_details" not in restored
    assert (recipient.workspace / "sample.wav").read_text() == "evidence"
    assert {kind for _, kind in recipient.artifacts} == {"AUDIO_SAMPLE", "TRACK_REVIEW", "SUBTITLE_DETECTION"}


def test_different_policies_and_changed_sources_cannot_use_old_cache(client, new_job, environment):
    donor = context(environment, new_job["id"])
    value = result(donor)
    cached = SharedTrackAnalysis(donor)
    cached.store(value)
    recipient = context(environment, peer(client)["id"])
    recipient.job.analysis = {"audio_review_policy": {"max_rounds": 1}}
    base = {**recipient.job.analysis, "tracks": fixture_tracks()}
    assert SharedTrackAnalysis(recipient).root != cached.root
    assert SharedTrackAnalysis(recipient).restore(base) == base
    (environment.source_root / "Movie.mkv").write_bytes(b"a different original source")
    changed = context(environment, peer(client)["id"])
    assert SharedTrackAnalysis(changed).root != cached.root
    assert SharedTrackAnalysis(changed).restore({"tracks": fixture_tracks()}) == {"tracks": fixture_tracks()}


def test_missing_evidence_and_mismatched_inventory_are_cache_misses(client, new_job, environment):
    donor = context(environment, new_job["id"])
    cached = SharedTrackAnalysis(donor)
    cached.store(result(donor))
    recipient = context(environment, peer(client)["id"])
    other_inventory = {"tracks": fixture_tracks()[:-1]}
    assert SharedTrackAnalysis(recipient).restore(other_inventory) == other_inventory
    (cached.root / "files/sample.wav").unlink()
    base = {"tracks": fixture_tracks()}
    assert SharedTrackAnalysis(recipient).restore(base) == base


def test_existing_completed_job_seeds_shared_analysis_without_its_manual_flags(client, new_job, environment):
    donor = context(environment, new_job["id"])
    value = result(donor)
    value["tracks"][0].update(default=True, flag_overrides={"default": True}, name_override="Personal name")
    with session() as db:
        db.get(MovieJob, donor.job.id).analysis = value
        db.commit()
    recipient = context(environment, peer(client)["id"])
    restored = SharedTrackAnalysis(recipient).restore({"tracks": fixture_tracks()})
    assert restored["shared_track_analysis"]["source_job_id"] == donor.job.id
    assert restored["tracks"][0]["default"] is False
    assert "name_override" not in restored["tracks"][0]


def test_api_returns_source_order_instead_of_row_insertion_or_track_id(client, new_job):
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        job.analysis = {"tracks": [{"track_id": i} for i in (9, 4, 12, 2)]}
        for i in (2, 12, 4, 9):
            db.add(
                MovieTrack(job_id=job.id, track_id=i, kind="audio" if i in (4, 9) else "subtitles", info={})
            )
        db.commit()
    for response in (
        client.get(f"/api/jobs/{new_job['id']}").json()["tracks"],
        client.get(f"/api/jobs/{new_job['id']}/tracks").json(),
    ):
        assert [t["track_id"] for t in response] == [9, 4, 12, 2]
        assert [t["info"]["source_order"] for t in response] == [0, 1, 2, 3]
    with session() as db:
        rows = db.query(MovieTrack).filter_by(job_id=new_job["id"]).all()
        for row in rows:
            row.info = {"source_order": {2: 1, 12: 0, 4: 3, 9: 2}[row.track_id]}
        db.commit()
    assert [t["track_id"] for t in client.get(f"/api/jobs/{new_job['id']}/tracks").json()] == [12, 2, 9, 4]


DETAILS = {
    "chinese_name": "电影",
    "source": "Blu-ray AVC DTS-HD MA",
    "extra_description": "字幕",
    "tracker": "https://tracker.example/announce",
    "upload_screenshots": False,
    "movie_description": "[b]电影介绍[/b]\nA multiline synopsis.",
}


def test_release_defaults_apply_to_existing_and_future_peers_without_copying_results(client, new_job):
    existing = peer(client)
    response = client.patch(f"/api/jobs/{new_job['id']}/release", json=DETAILS)
    assert response.status_code == 200
    future = peer(client)
    for item in (existing, future):
        value = client.get(f"/api/jobs/{item['id']}").json()
        assert value["analysis"]["release_details"] == DETAILS
        assert value["analysis"]["shared_release_details"]["source_job_id"] == new_job["id"]
        assert "release_result" not in value["analysis"]
        assert not any(task["type"] == "generate_release" for task in value["tasks"])
        assert client.get(f"/api/jobs/{item['id']}/analysis").json()["release_details"] == DETAILS
        with session() as db:
            assert "release_details" not in db.get(MovieJob, item["id"]).analysis  # GET is read-only.


def test_saved_release_information_stays_shared_and_other_sources_are_isolated(client, new_job, environment):
    other = peer(client)
    assert client.patch(f"/api/jobs/{new_job['id']}/release", json=DETAILS).status_code == 200
    local = {**DETAILS, "extra_description": "For x264"}
    assert client.patch(f"/api/jobs/{other['id']}/release", json=local).status_code == 200
    newer = {**DETAILS, "extra_description": "Updated source defaults"}
    assert client.patch(f"/api/jobs/{new_job['id']}/release", json=newer).status_code == 200
    assert client.get(f"/api/jobs/{other['id']}").json()["analysis"]["release_details"] == newer
    assert peer(client)["analysis"]["release_details"] == newer
    (environment.source_root / "Movie.mkv").write_bytes(b"different content")
    assert "release_details" not in peer(client)["analysis"]


def test_shared_ocr_evidence_survives_removing_original_job_and_agent_files(client, new_job, environment):
    donor = context(environment, new_job["id"])
    recipient = context(environment, peer(client)["id"])
    value = result(donor)
    track_id = next(t["track_id"] for t in value["tracks"] if t.get("codec_id") == "S_HDMV/PGS")
    original = environment.cache_root / "agent" / "original-ocr"
    original.mkdir(parents=True)
    (original / "cue.png").write_bytes(b"cue image")
    (original / "sheet.jpg").write_bytes(b"contact sheet")
    (original / "ocr.json").write_text(
        json.dumps({"contact_sheets": ["sheet.jpg"], "samples": [{"image": "cue.png", "text": "Bonjour"}]})
    )
    pointer_name = f"track-analysis/ocr-{track_id}.json"
    pointer = donor.workspace / pointer_name
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"signature": "native-subtitle-signature", "root": "agent/original-ocr"}))
    SharedTrackAnalysis(donor).store(value)
    shutil.rmtree(donor.workspace)
    shutil.rmtree(original)
    restored = SharedTrackAnalysis(recipient).restore({"tracks": fixture_tracks()})
    assert restored["shared_track_analysis"]["source_job_id"] == donor.job.id
    record = json.loads((recipient.workspace / pointer_name).read_text())
    assert record["signature"] == "native-subtitle-signature"
    shared = contained(environment.cache_root, record["root"])
    assert (shared / "cue.png").read_bytes() == b"cue image"
    assert (shared / "sheet.jpg").read_bytes() == b"contact sheet"
    assert json.loads((shared / "ocr.json").read_text())["samples"][0]["text"] == "Bonjour"
    SharedTrackAnalysis(recipient).store(restored)
    manifest = json.loads(SharedTrackAnalysis(recipient).manifest.read_text())
    assert manifest["job_id"] == donor.job.id
