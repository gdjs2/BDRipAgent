import threading

import pytest
from sqlalchemy import select

from backend.app.services import advance, enqueue
from shared.db import session
from shared.models import MovieJob, Task, TrackSelection
from tests.conftest import gate
from worker.pipeline.stages import HANDLERS, update_analysis
from worker.pipeline.track_analysis import save_analysis
from worker.tasks import execute


def scan_result():
    return {"video": {"duration": 600, "track_id": 0}, "crop": {}, "tracks": []}


def test_crf_and_review_overlap_and_preserve_release_draft(client, new_job, monkeypatch):
    monkeypatch.setitem(
        HANDLERS, "analyze", lambda ctx: lambda db, job: save_analysis(db, job, scan_result())
    )
    execute(new_job["tasks"][0]["id"])
    job = client.get(f"/api/jobs/{new_job['id']}").json()
    assert job["state"] == "RUNNING_CRF_ANALYSIS" and not job["track_selection"]
    tasks = {t["type"]: t for t in job["tasks"] if t["status"] == "QUEUED"}
    assert set(tasks) == {"review_tracks", "crf_analysis"}
    entered = {kind: threading.Event() for kind in tasks}
    release = threading.Event()

    def review(ctx):
        entered["review_tracks"].set()
        assert release.wait(10)
        return lambda db, job: save_analysis(db, job, {**scan_result(), "track_review_version": 1})

    def crf(ctx):
        entered["crf_analysis"].set()
        assert release.wait(10)
        return update_analysis({"video_pipeline_result": "retained"})

    monkeypatch.setitem(HANDLERS, "review_tracks", review)
    monkeypatch.setitem(HANDLERS, "crf_analysis", crf)
    threads = [threading.Thread(target=execute, args=(t["id"],)) for t in tasks.values()]
    try:
        for thread in threads:
            thread.start()
        assert all(event.wait(5) for event in entered.values())
        queue = client.get("/api/queue").json()
        assert queue["max_other_tasks"] == 3 and len(queue["running"]) == 2
        details = {"chinese_name": "电影", "source": "Blu-ray", "tracker": "https://example.com/announce"}
        assert client.patch(f"/api/jobs/{job['id']}/release", json=details).status_code == 200
        assert client.post(f"/api/jobs/{job['id']}/release", json=details).status_code == 409
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
    current = client.get(f"/api/jobs/{job['id']}").json()
    assert current["state"] == "WAITING_FOR_ENCODE_SELECTION", current["tasks"]
    assert current["analysis"]["release_details"]["chinese_name"] == "电影"
    assert current["analysis"]["video_pipeline_result"] == "retained"
    assert current["track_analysis_complete"]
    assert all(t["status"] == "SUCCEEDED" for t in current["tasks"])


@pytest.mark.parametrize("early_choice", [True, False])
def test_video_validation_waits_for_tracks_only_before_remux(client, new_job, early_choice):
    gate(new_job["id"], "ENCODING", [{"track_id": 1, "kind": "audio", "info": {"extractable": True}}])
    url = f"/api/jobs/{new_job['id']}/tracks/selection"
    body = {"audio_track_ids": [1], "subtitle_track_ids": []}
    if early_choice:
        assert client.post(url, json=body).json()["state"] == "ENCODING"
        # Choices remain editable while encoding, with a single persisted selection.
        assert client.post(url, json={**body, "audio_track_ids": []}).status_code == 200
        assert client.post(url, json=body).status_code == 200
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        job.state = "VALIDATING_ENCODE"
        job.validation = {"valid": True}
        advance(db, job)
        db.commit()
        assert job.state == ("PREPARING_TRACKS" if early_choice else "WAITING_FOR_TRACK_SELECTION")
        assert len(db.scalars(select(TrackSelection).where(TrackSelection.job_id == job.id)).all()) == int(
            early_choice
        )
    if not early_choice:
        assert client.post(url, json=body).json()["state"] == "PREPARING_TRACKS"
    assert client.post(url, json=body).status_code == 409
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        for task in db.scalars(select(Task).where(Task.job_id == job.id)):
            task.status = "SUCCEEDED"
        db.flush()
        advance(db, job)
        db.commit()
        assert job.state == "REMUXING"


def test_review_failure_does_not_block_crf_and_retry_stays_in_review_lane(client, new_job, monkeypatch):
    monkeypatch.setitem(
        HANDLERS, "analyze", lambda ctx: lambda db, job: save_analysis(db, job, scan_result())
    )
    execute(new_job["tasks"][0]["id"])
    job = client.get(f"/api/jobs/{new_job['id']}").json()
    tasks = {t["type"]: t for t in job["tasks"]}
    monkeypatch.setitem(
        HANDLERS, "review_tracks", lambda ctx: (_ for _ in ()).throw(ValueError("review unavailable"))
    )
    monkeypatch.setitem(HANDLERS, "crf_analysis", lambda ctx: None)
    execute(tasks["review_tracks"]["id"])
    execute(tasks["crf_analysis"]["id"])
    retried = client.post(f"/api/tasks/{tasks['review_tracks']['id']}/retry", json={})
    assert retried.status_code == 202 and retried.json()["lane"] == "tracks"
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "WAITING_FOR_ENCODE_SELECTION"
    assert client.post(f"/api/tasks/{tasks['review_tracks']['id']}/retry", json={}).status_code == 409


def test_track_selection_rejects_active_review_even_while_encoding(client, new_job):
    gate(new_job["id"], "ENCODING")
    with session() as db:
        enqueue(db, db.get(MovieJob, new_job["id"]), stage="ANALYZING_TRACKS")
        db.commit()
    response = client.post(
        f"/api/jobs/{new_job['id']}/tracks/selection", json={"audio_track_ids": [], "subtitle_track_ids": []}
    )
    assert response.status_code == 409 and "review" in response.text


def test_old_waiting_job_can_start_video_work_without_track_selection(client, new_job):
    from backend.app.services import reconcile

    gate(new_job["id"], "WAITING_FOR_TRACK_SELECTION")
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        job.analysis = {**scan_result(), "track_review_version": 1}
        db.commit()
        reconcile(db)
        reconcile(db)
    job = client.get(f"/api/jobs/{new_job['id']}").json()
    assert job["state"] == "RUNNING_CRF_ANALYSIS"
    assert not job["track_selection"]
    assert [t["type"] for t in job["tasks"] if t["status"] == "QUEUED"] == ["crf_analysis"]


def test_active_lane_uniqueness_and_worker_capacity(environment, client, new_job):
    from sqlalchemy.exc import IntegrityError

    from backend.app import queue

    gate(new_job["id"], "ENCODING")
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        enqueue(db, job)
        enqueue(db, job, stage="ANALYZING_TRACKS")
        db.commit()
        assert len(queue.available(db, queue.settings(db))) == 2
        old_capacity = environment.worker_capacity
        environment.worker_capacity = 1
        try:
            assert len(queue.available(db, queue.settings(db))) == 1
        finally:
            environment.worker_capacity = old_capacity
        db.add(Task(job_id=job.id, type="encode", stage="ENCODING", lane="pipeline"))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


def test_audio_limit_is_configurable_for_new_and_paired_jobs(client, new_job):
    body = {"source_path": "Movie.mkv", "title": "Budget", "year": 2026, "audio_review_max_rounds": 2}
    pair = client.post("/api/jobs/pair", json=body)
    assert pair.status_code == 201, pair.text
    assert all(job["analysis"]["audio_review_policy"]["max_rounds"] == 2 for job in pair.json())
    assert new_job["analysis"]["audio_review_policy"]["max_rounds"] == 6
    for invalid in (0, 31, True, 1.5):
        assert client.post("/api/jobs", json={**body, "audio_review_max_rounds": invalid}).status_code == 422


def test_track_order_can_be_updated_while_encoding_and_locks_before_remux(client, new_job):
    tracks = [
        {"track_id": track_id, "kind": kind, "info": {"codec_id": codec, "extractable": True}}
        for track_id, kind, codec in [
            (4, "audio", "A_AC3"),
            (8, "audio", "A_AC3"),
            (9, "subtitles", "S_HDMV/PGS"),
            (12, "subtitles", "S_HDMV/PGS"),
        ]
    ]
    gate(new_job["id"], "ENCODING", tracks)
    url = f"/api/jobs/{new_job['id']}/tracks/selection"
    for audio, subtitles in [([8, 4], [12, 9]), ([4, 8], [9, 12])]:
        response = client.post(url, json={"audio_track_ids": audio, "subtitle_track_ids": subtitles})
        assert response.status_code == 200, response.text
        job = client.get(f"/api/jobs/{new_job['id']}").json()
        assert job["state"] == "ENCODING"
        assert job["track_selection"]["audio_track_ids"] == audio
        assert job["track_selection"]["subtitle_track_ids"] == subtitles
    with session() as db:
        assert (
            len(db.scalars(select(TrackSelection).where(TrackSelection.job_id == new_job["id"])).all()) == 1
        )
    gate(new_job["id"], "PREPARING_TRACKS")
    assert (
        client.post(url, json={"audio_track_ids": [8, 4], "subtitle_track_ids": [12, 9]}).status_code == 409
    )
    saved = client.get(f"/api/jobs/{new_job['id']}").json()["track_selection"]
    assert saved["audio_track_ids"] == [4, 8] and saved["subtitle_track_ids"] == [9, 12]
