import threading

import pytest
from sqlalchemy import select

from backend.app import queue
from shared.db import session
from shared.models import MovieJob, Task, now


def tasks_for(client, environment, types):
    (environment.source_root / "Pools.mkv").write_bytes(b"pool admission fixture")
    tasks = []
    stages = {
        "encode": "ENCODING",
        "crf_analysis": "RUNNING_CRF_ANALYSIS",
        "analyze": "ANALYZING_SOURCE",
        "generate_release": "GENERATING_RELEASE",
    }
    for index, kind in enumerate(types):
        job = client.post(
            "/api/jobs", json={"source_path": "Pools.mkv", "title": f"Pool {index}", "year": 2026}
        ).json()
        task_id = job["tasks"][0]["id"]
        with session() as db:
            task = db.get(Task, task_id)
            task.type, task.stage = kind, stages[kind]
            db.get(MovieJob, job["id"]).state = stages[kind]
            db.commit()
        tasks.append(task_id)
    return tasks


def offered():
    with session() as db:
        return [t.id for t in queue.available(db, queue.settings(db))]


def test_each_pool_has_independent_slots_and_running_cancellations_keep_them(client, environment):
    ids = tasks_for(
        client,
        environment,
        ["encode", "encode", "crf_analysis", "crf_analysis", "analyze", "analyze", "analyze", "analyze"],
    )
    assert offered() == [ids[0], ids[2], *ids[4:7]]
    with session() as db:
        for ident in [ids[0], ids[2], *ids[4:7]]:
            db.get(Task, ident).status = "RUNNING"
        db.get(Task, ids[0]).pause_requested = True
        db.get(Task, ids[0]).paused_at = now()
        db.get(Task, ids[2]).cancel_requested = True
        db.commit()
    assert offered() == []
    snapshot = client.get("/api/queue").json()
    assert [snapshot[f"running_{pool}_tasks"] for pool in ("encoding", "crf", "other")] == [1, 1, 3]
    assert next(t for t in snapshot["queued"] if t["id"] == ids[3])["pool"] == "crf"
    assert client.patch("/api/queue", json={"max_crf_tasks": 2}).status_code == 200
    assert offered() == [ids[3]]
    assert client.patch("/api/queue", json={"max_crf_tasks": 1, "max_encoding_tasks": 2}).status_code == 200
    assert offered() == [ids[1]]
    assert client.patch("/api/queue", json={"max_encoding_tasks": 1}).status_code == 200
    with session() as db:
        db.get(Task, ids[4]).status = "SUCCEEDED"
        db.commit()
    assert offered() == [ids[7]]
    with session() as db:
        db.get(Task, ids[2]).status = "CANCELLED"
        db.commit()
    assert offered() == [ids[3], ids[7]]


def test_full_other_pool_does_not_block_crf_or_encoding_and_total_capacity_is_respected(client, environment):
    ids = tasks_for(
        client, environment, ["analyze", "analyze", "analyze", "analyze", "crf_analysis", "encode"]
    )
    with session() as db:
        for task in db.scalars(select(Task).where(Task.id.in_(ids[:3]))):
            task.status = "RUNNING"
        db.commit()
    assert offered() == ids[4:]
    original = environment.worker_capacity
    try:
        environment.worker_capacity = 3
        assert offered() == []
        environment.worker_capacity = 4
        assert offered() == [ids[4]]
    finally:
        environment.worker_capacity = original
    assert client.patch("/api/queue", json={"paused": True}).status_code == 200
    assert offered() == []
    assert client.patch("/api/queue", json={"paused": False}).status_code == 200
    assert client.patch(f"/api/queue/tasks/{ids[4]}", json={"held": True}).status_code == 200
    assert offered() == [ids[5]]


@pytest.mark.parametrize("field", ["max_encoding_tasks", "max_other_tasks"])
@pytest.mark.parametrize("value", [0, 65, True, 1.5, "2"])
def test_pool_limits_require_bounded_integers(client, field, value):
    assert client.patch("/api/queue", json={field: value}).status_code == 422


def test_pool_settings_persist_independently_and_reject_oversized_updates_atomically(client, environment):
    changed = client.patch(
        "/api/queue", json={"max_encoding_tasks": 2, "max_crf_tasks": 3, "max_other_tasks": 4}
    ).json()
    assert changed["max_encoding_tasks"] == 2 and changed["max_other_tasks"] == 4
    assert (
        client.patch(
            "/api/queue", json={"max_encoding_tasks": 3, "max_crf_tasks": environment.worker_capacity + 1}
        ).status_code
        == 409
    )
    current = client.get("/api/queue").json()
    assert (
        current["max_encoding_tasks"] == 2
        and current["max_crf_tasks"] == 3
        and current["max_other_tasks"] == 4
    )
    assert client.patch("/api/queue", json={"max_encoding_tasks": 1}).json()["max_other_tasks"] == 4
    assert client.patch("/api/queue", json={"max_concurrent_jobs": 2}).status_code == 422


def test_worker_claims_three_pools_concurrently_without_oversubscribing(client, environment, monkeypatch):
    from worker.pipeline.stages import HANDLERS
    from worker.tasks import execute

    ids = tasks_for(
        client,
        environment,
        ["crf_analysis", "encode", "analyze", "analyze", "analyze", "crf_analysis", "encode", "analyze"],
    )
    release = threading.Event()
    started = {task_id: threading.Event() for task_id in ids}
    calls = []

    def work(ctx):
        calls.append(ctx.task_id)
        started[ctx.task_id].set()
        assert release.wait(15)

    def stop_at_decision(db, job):
        job.state = "WAITING_FOR_ENCODE_SELECTION"

    for kind in ("encode", "crf_analysis", "analyze"):
        monkeypatch.setitem(HANDLERS, kind, work)
    monkeypatch.setattr("worker.tasks.advance", stop_at_decision)
    threads = [threading.Thread(target=execute, args=(task_id,)) for task_id in ids[:5]]
    try:
        for thread in threads:
            thread.start()
        assert all(started[task_id].wait(5) for task_id in ids[:5])
        snapshot = client.get("/api/queue").json()
        assert (
            snapshot["running_encoding_tasks"] == 1
            and snapshot["running_crf_tasks"] == 1
            and snapshot["running_other_tasks"] == 3
        )
        for ident in ids[5:]:
            execute(ident)
        assert set(calls) == set(ids[:5])
        # Recheck limits at claim time, even for already delivered broker messages.
        client.patch("/api/queue", json={"max_other_tasks": 1})
        assert len(client.get("/api/queue").json()["running"]) == 5
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    for ident in ids[5:]:
        execute(ident)
    execute(ids[5])
    assert len(calls) == 8 and len(set(calls)) == 8


def test_release_limit_applies_to_pending_batch_and_does_not_block_other_work(client, environment):
    ids = tasks_for(
        client, environment, ["generate_release", "generate_release", "analyze", "analyze", "encode"]
    )
    assert offered() == [ids[0], *ids[2:]]
    assert client.patch(f"/api/queue/tasks/{ids[0]}", json={"held": True}).status_code == 200
    assert offered() == ids[1:]
    assert client.patch("/api/queue", json={"max_other_tasks": 1}).status_code == 200
    assert offered() == [ids[1], ids[4]], "A release still consumes an other-task slot"
    assert client.patch("/api/queue", json={"paused": True}).status_code == 200
    assert offered() == []


@pytest.mark.parametrize("finished_status", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_running_release_retains_slot_until_stopped(client, environment, finished_status):
    ids = tasks_for(client, environment, ["generate_release", "generate_release", "analyze"])
    with session() as db:
        first = db.get(Task, ids[0])
        first.status, first.cancel_requested = "RUNNING", True
        db.commit()
    assert offered() == [ids[2]]
    status = client.get("/api/queue").json()
    assert status["max_release_tasks"] == 1 and status["running_release_tasks"] == 1
    assert status["running_other_tasks"] == 1
    with session() as db:
        db.get(Task, ids[0]).status = finished_status
        db.commit()
    assert offered() == ids[1:]
    assert client.get("/api/queue").json()["running_release_tasks"] == 0


def test_worker_serializes_release_claims_including_duplicate_delivery(client, environment, monkeypatch):
    from worker.pipeline.stages import HANDLERS
    from worker.tasks import execute

    ids = tasks_for(client, environment, ["generate_release", "generate_release", "analyze"])
    started = {ident: threading.Event() for ident in ids}
    finish = threading.Event()
    calls = []

    def work(ctx):
        calls.append(ctx.task_id)
        started[ctx.task_id].set()
        assert finish.wait(15)

    def stop_at_decision(db, job):
        job.state = "WAITING_FOR_ENCODE_SELECTION"

    monkeypatch.setitem(HANDLERS, "generate_release", work)
    monkeypatch.setitem(HANDLERS, "analyze", work)
    monkeypatch.setattr("worker.tasks.advance", stop_at_decision)
    threads = [threading.Thread(target=execute, args=(ident,)) for ident in ids]
    try:
        for thread in threads:
            thread.start()
        assert started[ids[0]].wait(5) and started[ids[2]].wait(5)
        threads[1].join(5)
        assert not threads[1].is_alive() and not started[ids[1]].is_set()
        execute(ids[0])  # A duplicate broker message cannot run the same release again.
        execute(ids[1])  # A delivered second release must be deferred at claim time.
        assert set(calls) == {ids[0], ids[2]}
        assert client.get("/api/queue").json()["running_release_tasks"] == 1
    finally:
        finish.set()
        for thread in threads:
            thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    execute(ids[1])
    assert calls.count(ids[0]) == calls.count(ids[1]) == 1
