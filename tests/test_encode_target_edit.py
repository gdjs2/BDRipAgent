from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import select

from shared.db import session
from shared.models import EncodeConfig, MovieJob, Task, now
from tests.conftest import gate


@pytest.fixture
def queued_encode(client, new_job):
    gate(new_job["id"], "WAITING_FOR_ENCODE_SELECTION", profile="x265-live")
    response = client.post(
        f"/api/jobs/{new_job['id']}/encode-selection",
        json={"codec": "x265", "profile": "x265-live", "rate_control": "bitrate", "bitrate_kbps": 8000},
    )
    assert response.status_code == 200, response.text
    job = response.json()
    return job, next(task for task in job["tasks"] if task["type"] == "encode")


def test_pending_target_change_keeps_queue_position_and_uses_saved_profile(
    client, queued_encode, monkeypatch
):
    from worker.pipeline.stages import HANDLERS
    from worker.tasks import execute

    job, task = queued_encode
    with session() as db:
        current = db.get(Task, task["id"])
        current.held, current.queue_priority, current.dispatched_at = True, 37, now()
        db.commit()
    response = client.patch(
        f"/api/jobs/{job['id']}/encode-selection", json={"rate_control": "bitrate", "bitrate_kbps": 12500}
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    saved = updated["encode_config"]["data"]
    assert saved["bitrate_kbps"] == 12500
    assert saved["profile_snapshot"] == job["encode_config"]["data"]["profile_snapshot"]
    assert saved["profile"] == "x265-live" and saved["codec"] == "x265"
    assert len(updated["tasks"]) == len(job["tasks"])
    with session() as db:
        current = db.get(Task, task["id"])
        assert current.status == "QUEUED" and current.held and current.queue_priority == 37
        assert current.dispatched_at is not None
        current.held = False
        db.commit()
    seen = []

    def encode(ctx):
        with session() as db:
            seen.append(
                db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job["id"])).data["bitrate_kbps"]
            )

    monkeypatch.setitem(HANDLERS, "encode", encode)
    execute(task["id"])
    assert seen == [12500]


@pytest.mark.parametrize("status", ["CANCELLED", "FAILED"])
def test_stopped_target_saves_without_retry_and_new_attempt_uses_it(client, queued_encode, status):
    job, task = queued_encode
    with session() as db:
        current = db.get(Task, task["id"])
        current.status, current.finished_at = status, now()
        current.command_json = [["HandBrakeCLI", "-b", "8000"]]
        db.commit()
    response = client.patch(f"/api/jobs/{job['id']}/encode-selection", json={"crf": 18})
    assert response.status_code == 200, response.text
    saved = response.json()["encode_config"]["data"]
    assert saved["crf"] == 18 and saved["rate_control"] == "crf"
    assert "bitrate_kbps" not in saved
    assert next(t for t in response.json()["tasks"] if t["id"] == task["id"])["status"] == status
    retry = client.post(f"/api/tasks/{task['id']}/retry", json={})
    assert retry.status_code == 202 and retry.json()["attempt"] == 2
    with session() as db:
        assert db.get(Task, task["id"]).command_json == [["HandBrakeCLI", "-b", "8000"]]
        assert db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job["id"])).data["crf"] == 18
    # Switching back clears the old CRF rather than persisting conflicting targets.
    result = client.patch(
        f"/api/jobs/{job['id']}/encode-selection", json={"rate_control": "bitrate", "bitrate_kbps": 9000}
    )
    assert result.status_code == 200 and "crf" not in result.json()["encode_config"]["data"]


@pytest.mark.parametrize("paused,cancelling", [(False, False), (True, False), (False, True)])
def test_running_paused_or_stopping_encode_target_cannot_change(client, queued_encode, paused, cancelling):
    job, task = queued_encode
    with session() as db:
        current = db.get(Task, task["id"])
        current.status, current.pause_requested, current.cancel_requested = "RUNNING", paused, cancelling
        db.commit()
    result = client.patch(
        f"/api/jobs/{job['id']}/encode-selection", json={"rate_control": "bitrate", "bitrate_kbps": 9000}
    )
    assert result.status_code == 409
    assert client.get(f"/api/jobs/{job['id']}").json()["encode_config"] == job["encode_config"]


@pytest.mark.parametrize(
    "target",
    [
        {"rate_control": "bitrate", "bitrate_kbps": 0},
        {"rate_control": "bitrate", "bitrate_kbps": 1000001},
        {"rate_control": "bitrate", "bitrate_kbps": 4.5},
        {"rate_control": "bitrate", "bitrate_kbps": True},
        {"rate_control": "bitrate", "bitrate_kbps": 9000, "crf": 18},
        {"rate_control": "bitrate", "bitrate_kbps": 9000, "profile": "x264-live"},
        {},
    ],
)
def test_invalid_edits_do_not_change_config(client, queued_encode, target):
    job, _ = queued_encode
    assert client.patch(f"/api/jobs/{job['id']}/encode-selection", json=target).status_code == 422
    assert client.get(f"/api/jobs/{job['id']}").json()["encode_config"] == job["encode_config"]


def test_profile_limits_completed_jobs_and_smoke_mode_cannot_be_bypassed(client, queued_encode):
    job, task = queued_encode
    url = f"/api/jobs/{job['id']}/encode-selection"
    maximum = job["encode_config"]["data"]["profile_snapshot"]["crf_max"]
    assert client.patch(url, json={"crf": maximum + 1}).status_code in (409, 422)
    with session() as db:
        db.get(MovieJob, job["id"]).state = "VALIDATING_ENCODE"
        db.get(Task, task["id"]).status = "SUCCEEDED"
        db.commit()
    assert client.patch(url, json={"crf": 18}).status_code == 409
    with session() as db:
        current = db.get(MovieJob, job["id"])
        current.state, current.analysis = "ENCODING", {"smoke_test": True}
        db.get(Task, task["id"]).status = "QUEUED"
        db.commit()
    assert client.patch(url, json={"crf": 18}).status_code == 409


def test_worker_claim_wins_against_stale_edit(client, queued_encode, monkeypatch):
    from worker.pipeline.stages import HANDLERS
    from worker.tasks import execute

    job, task = queued_encode
    started, finish = Event(), Event()
    seen = []

    def encode(ctx):
        with session() as db:
            seen.append(
                db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job["id"])).data["bitrate_kbps"]
            )
        started.set()
        assert finish.wait(10)

    monkeypatch.setitem(HANDLERS, "encode", encode)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(execute, task["id"])
        try:
            assert started.wait(10)
            assert (
                client.patch(
                    f"/api/jobs/{job['id']}/encode-selection",
                    json={"rate_control": "bitrate", "bitrate_kbps": 9000},
                ).status_code
                == 409
            )
        finally:
            finish.set()
        future.result(timeout=10)
    assert seen == [8000]
