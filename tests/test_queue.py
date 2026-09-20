import threading
from datetime import timedelta

import pytest

from backend.app import queue
from shared.db import session
from shared.models import MovieJob, Task, now


def more_jobs(client, new_job):
    return [
        new_job,
        *[
            client.post(
                "/api/jobs",
                json={
                    "source_path": "Movie.mkv",
                    "title": f"Movie {index}",
                    "year": 2026,
                },
            ).json()
            for index in range(2)
        ],
    ]


def test_queue_settings_are_persistent_and_authenticated(client):
    value = client.get("/api/queue").json()
    assert value["max_concurrent_jobs"] == 1 and not value["paused"]
    assert client.patch("/api/queue", json={"max_concurrent_jobs": 2, "paused": True}).status_code == 200
    value = client.get("/api/queue").json()
    assert value["effective_limit"] == 2 and value["paused"]
    with session() as db:
        assert queue.settings(db).max_concurrent_jobs == 2
    client.headers.clear()
    assert client.get("/api/queue").status_code == 401
    assert client.patch("/api/queue", json={"paused": False}).status_code == 401


@pytest.mark.parametrize("value", [0, 65, True, 1.5, "2"])
def test_queue_rejects_invalid_limits(client, value):
    assert client.patch("/api/queue", json={"max_concurrent_jobs": value}).status_code == 422


def test_queue_limit_cannot_exceed_worker_capacity(client, environment):
    assert (
        client.patch("/api/queue", json={"max_concurrent_jobs": environment.worker_capacity + 1}).status_code
        == 409
    )


def test_dispatch_obeys_pause_hold_order_capacity_and_delivery_reservations(client, new_job, monkeypatch):
    from worker.tasks import dispatch, execute

    jobs = more_jobs(client, new_job)
    ids = [j["tasks"][0]["id"] for j in jobs]
    sent = []
    monkeypatch.setattr(execute, "apply_async", lambda **kwargs: sent.append(kwargs["args"][0]))
    client.patch("/api/queue", json={"paused": True, "max_concurrent_jobs": 2})
    dispatch()
    assert not sent
    assert client.put("/api/queue/order", json={"task_ids": ids[::-1]}).status_code == 200
    client.patch(f"/api/queue/tasks/{ids[2]}", json={"held": True})
    client.patch("/api/queue", json={"paused": False})
    dispatch()
    assert sent == [ids[1], ids[0]]
    dispatch()
    assert len(sent) == 2
    # A lost broker delivery is retried without generating a second task row.
    with session() as db:
        db.get(Task, ids[1]).dispatched_at = now() - timedelta(minutes=2)
        db.commit()
    dispatch()
    assert sent == [ids[1], ids[0], ids[1]]
    assert client.get("/api/queue").json()["queued"][0]["held"]


def test_stale_reorder_and_duplicate_ids_are_rejected(client, new_job):
    task_id = new_job["tasks"][0]["id"]
    assert client.put("/api/queue/order", json={"task_ids": []}).status_code == 409
    assert client.put("/api/queue/order", json={"task_ids": [task_id, task_id]}).status_code == 422


def test_worker_admission_limits_concurrency_and_defers_stale_broker_messages(client, new_job, monkeypatch):
    from worker.pipeline.stages import HANDLERS
    from worker.tasks import execute

    jobs = more_jobs(client, new_job)
    ids = [j["tasks"][0]["id"] for j in jobs]
    release = threading.Event()
    both_started = threading.Event()
    calls = []

    def handler(ctx):
        calls.append(ctx.task_id)
        if len(calls) == 2:
            both_started.set()
        assert release.wait(15)

    monkeypatch.setitem(HANDLERS, "analyze", handler)
    # Direct delivery cannot jump ahead of the FIFO queue.
    execute(ids[2])
    assert not calls
    client.patch("/api/queue", json={"max_concurrent_jobs": 2})
    threads = [threading.Thread(target=execute, args=(task_id,)) for task_id in ids[:2]]
    try:
        for thread in threads:
            thread.start()
        assert both_started.wait(5)
        execute(ids[2])
        assert set(calls) == set(ids[:2])
        assert len(client.get("/api/queue").json()["running"]) == 2
        client.patch("/api/queue", json={"max_concurrent_jobs": 1, "paused": True})
        assert len(client.get("/api/queue").json()["running"]) == 2
        assert client.patch(f"/api/queue/tasks/{ids[0]}", json={"held": True}).status_code == 409
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    execute(ids[2])
    assert len(calls) == 2
    client.patch("/api/queue", json={"paused": False})
    execute(ids[2])
    execute(ids[2])  # Duplicate delivery cannot repeat successful work.
    assert len(calls) == 3
    assert not client.get("/api/queue").json()["queued"]
    assert all(j["state"] == "WAITING_FOR_TRACK_SELECTION" for j in client.get("/api/jobs").json())


def test_hold_blocks_already_delivered_work_until_resumed(client, new_job, monkeypatch):
    from worker.pipeline.stages import HANDLERS
    from worker.tasks import execute

    task_id = new_job["tasks"][0]["id"]
    calls = []
    monkeypatch.setitem(HANDLERS, "analyze", lambda ctx: calls.append(ctx.task_id))
    client.patch(f"/api/queue/tasks/{task_id}", json={"held": True})
    execute(task_id)
    assert not calls
    client.patch(f"/api/queue/tasks/{task_id}", json={"held": False})
    execute(task_id)
    assert calls == [task_id]


def test_removing_queued_job_cancels_task_and_ignores_stale_delivery(
    client, new_job, environment, monkeypatch
):
    from worker.pipeline.stages import HANDLERS
    from worker.tasks import execute

    job_id, task_id = new_job["id"], new_job["tasks"][0]["id"]
    calls = []
    monkeypatch.setitem(HANDLERS, "analyze", lambda ctx: calls.append(ctx.task_id))
    assert client.delete(f"/api/jobs/{job_id}?confirm=no").status_code == 409
    assert client.delete(f"/api/jobs/{job_id}?confirm={job_id}").json()["files_retained"]
    execute(task_id)
    assert not calls
    assert client.get("/api/jobs").json() == []
    assert client.get("/api/queue").json()["queued"] == []
    assert (environment.source_root / "Movie.mkv").is_file()
    assert (environment.workspace_root / job_id / "manifest.yaml").is_file()
    with session() as db:
        assert db.get(Task, task_id).status == "CANCELLED"
        assert db.get(MovieJob, job_id).deleted_at is not None


def test_removing_a_running_job_is_rejected(client, new_job):
    with session() as db:
        db.get(Task, new_job["tasks"][0]["id"]).status = "RUNNING"
        db.commit()
    response = client.delete(f"/api/jobs/{new_job['id']}?confirm={new_job['id']}")
    assert response.status_code == 409
    assert client.get(f"/api/jobs/{new_job['id']}").status_code == 200
