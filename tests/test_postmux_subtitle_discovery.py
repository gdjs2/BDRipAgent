"""Completed muxes can search again without changing their published track snapshot."""

from uuid import uuid4

import pytest

from backend.app import queue
from backend.app.services import enqueue, task_is_current
from backend.app.subtitle_discovery import ensure_discovery
from backend.app.subtitle_uploads import register_upload
from shared.db import session
from shared.models import MovieJob, Task
from shared.state import Stage
from tests import test_remux as remux_fixtures
from tests.test_source_choices import CHOICES, read
from tests.test_subtitle_uploads import SRT, upload
from worker.pipeline.stages import HANDLERS
from worker.runtime import Interrupted
from worker.tasks import dispatch, execute

completed_job = remux_fixtures.completed_job


@pytest.mark.parametrize(
    "stage", ["COMPLETE", "WAITING_FOR_RELEASE_DETAILS", "WAITING_FOR_SCREENSHOT_SELECTION"]
)
def test_search_again_import_and_remux_after_mux(client, completed_job, environment, stage, monkeypatch):
    job_id = completed_job
    with session() as db:
        job = db.get(MovieJob, job_id)
        job.state = stage
        job.analysis = {**job.analysis, "subtitle_discovery_policy": {"enabled": True}}
        assert not ensure_discovery(db, job), "Automatic discovery must not restart after muxing"
        db.commit()
    before = read(client, job_id)
    assert before["subtitle_discovery"]["allowed"]
    url = f"/api/jobs/{job_id}/subtitles/discover"
    response = client.post(url, json={"original_languages": ["en"]})
    assert response.status_code == 202, response.text
    task_id = response.json()["subtitle_discovery"]["active_task"]["id"]
    again = client.post(url, json={})
    assert again.status_code == 202
    assert again.json()["subtitle_discovery"]["active_task"]["id"] == task_id
    assert not again.json()["remux"]["available"]
    assert upload(client, job_id).status_code == 409
    assert client.post(f"/api/jobs/{job_id}/remux", json=CHOICES).status_code == 409
    sent = []
    monkeypatch.setattr(execute, "apply_async", lambda **kw: sent.append(kw))
    assert task_id in {t["id"] for t in client.get("/api/queue").json()["queued"]}
    dispatch()
    assert sent == [{"args": [task_id], "task_id": task_id, "queue": "bdrip.other"}]
    results = []

    def discover(ctx):
        with session() as db:
            task = db.get(Task, ctx.task_id)
            assert task.status == "RUNNING" and task.run_token == ctx.token
        path = environment.workspace_root / "discovered" / "subtitle.srt"
        path.parent.mkdir()
        path.write_bytes(SRT)
        results.append(
            register_upload(
                job_id,
                path,
                "English.srt",
                "en",
                False,
                str(uuid4()),
                provenance={"review": {"explanation": "Aligned dialogue"}},
                detection={"language_confident": True, "language_code": "en", "hearing_impaired": False},
                task_identity=(ctx.task_id, ctx.token),
            )
        )

    monkeypatch.setitem(HANDLERS, "discover_subtitles", discover)
    execute(task_id)
    current = next(t for t in read(client, job_id)["tasks"] if t["id"] == task_id)
    assert current["status"] == "SUCCEEDED", current
    track_id = results[0]["track"]["track_id"]
    after = read(client, job_id)
    assert after["state"] == stage
    assert after["track_selection"] == before["track_selection"]
    assert after["analysis"]["final_path"] == before["analysis"]["final_path"]
    assert after["remux"]["available"] and after["subtitle_discovery"]["allowed"]
    assert after["subtitle_discovery"]["review_required"]
    response = client.post(
        f"/api/jobs/{job_id}/remux",
        json={
            **CHOICES,
            "shared_revision": after["shared_track_selection"]["revision"],
            "subtitle_track_ids": [*CHOICES["subtitle_track_ids"], track_id],
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["track_selection"]["subtitle_track_ids"][-1] == track_id
    assert (environment.completed_root / before["analysis"]["final_path"]).read_bytes() == b"previous mux"


@pytest.mark.parametrize("blocker", ["missing_encode", "invalid_video", "active_mux"])
def test_postmux_search_preserves_readiness_guards(client, completed_job, environment, blocker):
    with session() as db:
        job = db.get(MovieJob, completed_job)
        if blocker == "missing_encode":
            (environment.workspace_root / completed_job / "encoded.mkv").unlink()
        elif blocker == "invalid_video":
            job.validation = {}
        else:
            enqueue(db, job, stage=Stage.REMUXING)
        db.commit()
    assert not read(client, completed_job)["subtitle_discovery"]["allowed"]
    assert client.post(f"/api/jobs/{completed_job}/subtitles/discover", json={}).status_code == 409


@pytest.mark.parametrize("blocker", ["stale_token", "cancelled", "wrong_type", "other_task"])
def test_discovery_import_requires_own_live_lease_and_no_other_work(
    client, completed_job, environment, blocker
):
    response = client.post(f"/api/jobs/{completed_job}/subtitles/discover", json={})
    assert response.status_code == 202
    task_id = response.json()["subtitle_discovery"]["active_task"]["id"]
    token = str(uuid4())
    with session() as db:
        task = db.get(Task, task_id)
        task.status, task.run_token = "RUNNING", token
        if blocker == "stale_token":
            task.run_token = str(uuid4())
        elif blocker == "cancelled":
            task.cancel_requested = True
        elif blocker == "wrong_type":
            task.type = "review_tracks"
        else:
            enqueue(db, db.get(MovieJob, completed_job), stage=Stage.REMUXING)
        db.commit()
    path = environment.workspace_root / "subtitle.srt"
    path.write_bytes(SRT)
    with pytest.raises(ValueError if blocker == "other_task" else Interrupted):
        register_upload(
            completed_job, path, "English.srt", "en", False, str(uuid4()), task_identity=(task_id, token)
        )
    assert not any(t["info"].get("origin") == "discovery" for t in read(client, completed_job)["tracks"])


def test_legacy_completed_mux_can_search_without_new_analysis_marker(client, completed_job):
    with session() as db:
        job = db.get(MovieJob, completed_job)
        job.analysis = {key: value for key, value in job.analysis.items() if key != "track_review_version"}
        db.commit()
    before = read(client, completed_job)
    assert not before["track_analysis_complete"]
    assert before["remux"]["available"] and before["subtitle_discovery"]["allowed"]
    response = client.post(f"/api/jobs/{completed_job}/subtitles/discover", json={})
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "COMPLETE"


@pytest.mark.parametrize("stage", list(Stage))
@pytest.mark.parametrize(
    "task_stage", [Stage.ANALYZING_TRACKS, Stage.FINDING_SUBTITLES, Stage.REVIEWING_SUBTITLE_UPLOAD]
)
def test_track_lane_queue_and_worker_agree_on_stage(client, new_job, stage, task_stage):
    from shared.state import REMUX_STATES, TRACK_EDIT_STAGES

    expected = stage in TRACK_EDIT_STAGES or (
        task_stage in (Stage.FINDING_SUBTITLES, Stage.REVIEWING_SUBTITLE_UPLOAD) and stage in REMUX_STATES
    )
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        job.state = stage
        task = enqueue(db, job, stage=task_stage)
        db.flush()
        assert (task.id in {t.id for t in queue.pending(db)}) == expected
        assert task_is_current(job, task) == expected


def test_postmux_search_respects_pause_and_hold(client, completed_job, monkeypatch):
    response = client.post(f"/api/jobs/{completed_job}/subtitles/discover", json={})
    task_id = response.json()["subtitle_discovery"]["active_task"]["id"]
    sent = []
    monkeypatch.setattr(execute, "apply_async", lambda **kw: sent.append(kw))
    assert client.patch("/api/queue", json={"paused": True}).status_code == 200
    dispatch()
    assert not sent
    assert client.patch(f"/api/queue/tasks/{task_id}", json={"held": True}).status_code == 200
    assert client.patch("/api/queue", json={"paused": False}).status_code == 200
    dispatch()
    assert not sent
    assert client.patch(f"/api/queue/tasks/{task_id}", json={"held": False}).status_code == 200
    dispatch()
    assert sent == [{"args": [task_id], "task_id": task_id, "queue": "bdrip.other"}]
