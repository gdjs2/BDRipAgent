from shared.db import session
from shared.models import MovieJob, Task
from tests.test_queue_pools import tasks_for
from worker.tasks import dispatch, execute


def test_encoding_crf_and_other_use_separate_queues(client, environment, monkeypatch):
    ids = tasks_for(client, environment, ["encode", "crf_analysis", "analyze"])
    client.patch("/api/queue", json={"max_encoding_tasks": 2})
    sent = {}
    monkeypatch.setattr(execute, "apply_async", lambda **kw: sent.update({kw["args"][0]: kw["queue"]}))
    dispatch()
    assert {task_id: sent[task_id] for task_id in ids} == {
        ids[0]: "bdrip.encoding",
        ids[1]: "bdrip.crf",
        ids[2]: "bdrip.other",
    }
    assert all(value == "bdrip.other" for task_id, value in sent.items() if task_id not in ids)


def test_wrong_worker_cannot_claim_even_a_direct_or_legacy_delivery(client, environment, monkeypatch):
    from worker.pipeline.stages import HANDLERS

    ids = tasks_for(client, environment, ["encode", "analyze", "crf_analysis"])
    calls = []
    for kind in ("encode", "analyze", "crf_analysis"):
        monkeypatch.setitem(HANDLERS, kind, lambda ctx: calls.append(ctx.task_id))
    environment.worker_pool = "other"
    execute(ids[0])
    execute(ids[2])
    assert not calls
    with session() as db:
        assert db.get(Task, ids[0]).status == "QUEUED"
        assert db.get(Task, ids[0]).run_token is None
    environment.worker_pool = "encoding"
    execute(ids[1])
    execute(ids[2])
    assert not calls
    environment.worker_pool = "crf"
    execute(ids[0])
    execute(ids[1])
    assert not calls
    execute(ids[2])
    environment.worker_pool = "encoding"
    execute(ids[0])
    assert calls == [ids[2], ids[0]]
    with session() as db:
        assert db.get(Task, ids[0]).status == "SUCCEEDED"
        assert db.get(MovieJob, db.get(Task, ids[1]).job_id).state == "ANALYZING_SOURCE"


def test_legacy_delivery_is_relayed_to_encoder_without_claiming_it(client, environment, monkeypatch):
    ids = tasks_for(client, environment, ["encode"])
    environment.worker_pool = "other"
    sent = []
    monkeypatch.setattr(execute, "apply_async", lambda **kw: sent.append(kw))
    execute.push_request(delivery_info={"routing_key": "celery"})
    try:
        execute(ids[0])
    finally:
        execute.pop_request()
    assert sent == [{"args": [ids[0]], "task_id": ids[0], "queue": "bdrip.encoding"}]
    with session() as db:
        task = db.get(Task, ids[0])
        assert task.status == "QUEUED" and task.run_token is None and task.dispatched_at


def test_previously_dispatched_crf_is_requeued_to_crf_pool(client, environment, monkeypatch):
    from shared.models import now

    task_id = tasks_for(client, environment, ["crf_analysis"])[0]
    with session() as db:
        db.get(Task, task_id).dispatched_at = now()
        db.commit()
    environment.worker_pool = "encoding"
    execute.push_request(delivery_info={"routing_key": "bdrip.encoding"})
    try:
        execute(task_id)
    finally:
        execute.pop_request()
    with session() as db:
        task = db.get(Task, task_id)
        assert task.status == "QUEUED" and task.run_token is None and task.dispatched_at is None
    sent = {}
    monkeypatch.setattr(execute, "apply_async", lambda **kw: sent.update({kw["args"][0]: kw["queue"]}))
    dispatch()
    assert sent[task_id] == "bdrip.crf"
