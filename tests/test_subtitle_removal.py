import pytest
from sqlalchemy import select

from backend.app.track_choices import source_key
from shared.db import session
from shared.models import MovieJob, SourceTrackChoices, Task, TrackSelection
from tests.test_source_choices import CHOICES, read, ready, save
from tests.test_source_sharing import peer
from tests.test_subtitle_uploads import registered_upload as upload


def discovered(client, job_id):
    response = upload(client, job_id)
    assert response.status_code == 201
    ident = response.json()["track"]["track_id"]
    with session() as db:
        record = db.get(SourceTrackChoices, source_key(db.get(MovieJob, job_id)))
        uploads = [{**t, "origin": "discovery"} for t in record.data["uploads"]]
        report = {
            "status": "complete",
            "summary": "Imported",
            "source_job_id": job_id,
            "original_languages": ["en"],
            "original_language_sources": [],
            "missing": ["zh-Hans", "zh-Hant"],
            "added_tracks": [{"track_id": ident}],
            "candidates": [{"track_id": ident, "status": "added"}],
        }
        record.data = {
            **record.data,
            "uploads": uploads,
            "discovery_version": 1,
            "subtitle_discovery": report,
        }
        db.commit()
    return response.json()["track"]


def test_removal_is_shared_retains_files_and_never_reuses_ids(client, new_job, environment):
    ident = new_job["id"]
    ready(ident)
    other = peer(client)["id"]
    ready(other)
    track = discovered(client, ident)
    tid = track["track_id"]
    path = environment.workspace_root / track["info"]["upload_path"]
    original = path.read_bytes()
    assert save(client, ident, {**CHOICES, "subtitle_track_ids": [tid, 9]}).status_code == 200
    url = f"/api/jobs/{other}/subtitles/discovered/{tid}"
    result = client.delete(url)
    assert result.status_code == 200, result.text
    for job_id in (ident, other):
        job = read(client, job_id)
        assert tid not in [t["track_id"] for t in job["tracks"]]
        assert job["track_selection"]["subtitle_track_ids"] == [9]
        assert job["subtitle_discovery"]["review_required"]
        assert job["subtitle_discovery"]["report"]["added_tracks"] == []
        assert job["subtitle_discovery"]["report"]["candidates"][0]["status"] == "removed"
    assert path.read_bytes() == original
    revision = read(client, ident)["shared_track_selection"]["revision"]
    assert client.delete(url).status_code == 200
    assert read(client, ident)["shared_track_selection"]["revision"] == revision
    future = peer(client)["id"]
    ready(future)
    assert tid not in [t["track_id"] for t in read(client, future)["tracks"]]
    uploaded = upload(client, ident)
    assert uploaded.status_code == 201 and not uploaded.json()["duplicate"]
    assert uploaded.json()["track"]["track_id"] > tid


@pytest.mark.parametrize("kind", ["mux", "prepare_tracks", "discover_subtitles", "review_tracks"])
def test_removal_blocks_shared_inflight_work(client, new_job, kind):
    ident = new_job["id"]
    ready(ident)
    track = discovered(client, ident)
    other = peer(client)["id"]
    ready(other)
    with session() as db:
        db.add(Task(job_id=other, type=kind, stage="REMUXING", status="RUNNING"))
        db.commit()
    assert not read(client, ident)["subtitle_discovery"]["removal"]["allowed"]
    response = client.delete(f"/api/jobs/{ident}/subtitles/discovered/{track['track_id']}")
    assert response.status_code == 409
    assert track["track_id"] in [t["track_id"] for t in read(client, ident)["tracks"]]


def test_removal_preserves_completed_snapshot_and_rejects_native_or_manual_tracks(client, new_job):
    ident = new_job["id"]
    ready(ident)
    track = discovered(client, ident)
    tid = track["track_id"]
    assert save(client, ident, {**CHOICES, "subtitle_track_ids": [tid, 9]}).status_code == 200
    with session() as db:
        job = db.get(MovieJob, ident)
        job.state = "COMPLETE"
        job.analysis = {**job.analysis, "final_path": "unchanged.mkv", "release_result": {"task_id": "old"}}
        db.commit()
    assert client.delete(f"/api/jobs/{ident}/subtitles/discovered/9").status_code == 404
    assert client.delete(f"/api/jobs/{ident}/subtitles/discovered/{tid}").status_code == 200
    result = read(client, ident)
    assert result["state"] == "COMPLETE" and result["analysis"]["final_path"] == "unchanged.mkv"
    with session() as db:
        assert db.scalar(select(TrackSelection).where(TrackSelection.job_id == ident)).subtitle_track_ids == [
            tid,
            9,
        ]
    with session() as db:
        db.get(MovieJob, ident).state = "ENCODING"
        db.commit()
    manual = upload(client, ident).json()["track"]["track_id"]
    assert client.delete(f"/api/jobs/{ident}/subtitles/discovered/{manual}").status_code == 409
    client.headers.clear()
    assert client.delete(f"/api/jobs/{ident}/subtitles/discovered/{manual}").status_code == 401
