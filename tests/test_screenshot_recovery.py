import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from shared.db import session
from shared.models import MovieJob, Task
from shared.paths import write_json
from tests.test_agent import fixture_selection
from worker.adapters import screenshot_agent
from worker.pipeline import screenshots
from worker.runtime import Interrupted, TaskContext


@pytest.fixture
def request_context():
    logs, progress = [], []
    return SimpleNamespace(
        settings=SimpleNamespace(agent_url="http://agent:8001", agent_token="internal-test-token"),
        job=SimpleNamespace(id="job"),
        task_id="attempt",
        check=lambda: None,
        log=logs.append,
        progress=lambda value, **detail: progress.append(detail),
        logs=logs,
        updates=progress,
    )


def transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(
        screenshot_agent.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(screenshot_agent.time, "sleep", lambda seconds: None)


def test_disconnect_and_restart_reconnect_without_changing_task(request_context, monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        if len(requests) == 2:
            raise httpx.ConnectError("Agent is restarting")
        return httpx.Response(200, json={"selected": []})

    transport(monkeypatch, handle)
    assert screenshot_agent.select_screenshots(request_context, 60) == {"selected": []}
    assert len(requests) == 3
    assert all(json.loads(r.content) == {"job_id": "job", "task_id": "attempt"} for r in requests)
    assert all(r.headers["authorization"] == "Bearer internal-test-token" for r in requests)
    assert requests[-1].extensions["timeout"] == {"connect": 10, "read": 60, "write": 60, "pool": 60}
    assert any("without repeating frame preparation" in line for line in request_context.logs)


def test_busy_request_respects_agent_lock_and_retries(request_context, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(409 if len(calls) == 1 else 200, json={"selected": []})

    transport(monkeypatch, handle)
    assert screenshot_agent.select_screenshots(request_context, 60) == {"selected": []}
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 422, 502])
def test_permanent_agent_errors_are_not_retried(request_context, monkeypatch, status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "Selection failed"})

    transport(monkeypatch, handle)
    with pytest.raises(RuntimeError, match=f"returned {status}"):
        screenshot_agent.select_screenshots(request_context, 60)
    assert len(calls) == 1


def test_reconnect_exhaustion_reports_actionable_error(request_context, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ConnectError("offline")

    transport(monkeypatch, handle)
    with pytest.raises(RuntimeError, match="after 4 attempts.*prepared B-frame candidates are retained"):
        screenshot_agent.select_screenshots(request_context, 60)
    assert len(calls) == 4


def test_cancel_during_reconnect_stops_requests(request_context, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.RemoteProtocolError("connection lost")

    def cancelled():
        if calls:
            raise Interrupted("cancelled")

    request_context.check = cancelled
    transport(monkeypatch, handle)
    with pytest.raises(Interrupted, match="cancelled"):
        screenshot_agent.select_screenshots(request_context, 60)
    assert len(calls) == 1


def test_failed_selection_retains_verified_index_for_new_attempt(client, new_job, environment, monkeypatch):
    candidates, selected = fixture_selection()
    workspace = environment.workspace_root / new_job["id"]
    for candidate in candidates:
        candidate["path"] = f"candidate-{candidate['candidate_id']}.png"
        Image.new("RGB", (64, 36)).save(workspace / candidate["path"])
    write_json(workspace / "candidates.json", {"candidates": candidates, "contact_sheets": []})
    unverified = json.loads((workspace / "candidates.json").read_text())
    for candidate in unverified["candidates"]:
        candidate.pop("b_frames_verified")
    write_json(workspace / "candidates.json", unverified)
    task_id = new_job["tasks"][0]["id"]
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        job.state = "SCREENSHOT_AGENT_SELECTION"
        job.analysis = {"candidate_index": "candidates.json"}
        job.validation = {"metrics": {"source_duration": 1000}}
        job.screenshot_policy = {
            **job.screenshot_policy,
            "count": 4,
            "representative": 2,
            "encode_challenging": 2,
        }
        task = db.get(Task, task_id)
        task.stage, task.type, task.status, task.run_token = (
            job.state,
            "select_screenshots",
            "RUNNING",
            "lease1",
        )
        db.commit()
    checks = []
    monkeypatch.setattr(
        screenshots, "verify_b_frame_candidates", lambda *args: checks.append(True) or candidates
    )
    monkeypatch.setattr(screenshots, "contact_sheets", lambda *args: [])
    monkeypatch.setattr(screenshots, "render_pairs", lambda *args, **kwargs: {})

    def unavailable(*args):
        raise RuntimeError("Agent unavailable after reconnects")

    monkeypatch.setattr(screenshots, "select_screenshots", unavailable)
    ctx = TaskContext(task_id, "lease1")
    try:
        with pytest.raises(RuntimeError, match="Agent unavailable"):
            screenshots.select_frames(ctx)
    finally:
        ctx.close()
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        saved = job.analysis["candidate_index"]
        assert saved != "candidates.json"
        assert all(c["b_frames_verified"] for c in json.loads((workspace / saved).read_text())["candidates"])
        assert job.state == "SCREENSHOT_AGENT_SELECTION"
        db.get(Task, task_id).status = "FAILED"
        db.commit()
    retried = client.post(f"/api/tasks/{task_id}/retry", json={}).json()
    with session() as db:
        task = db.scalar(select(Task).where(Task.id == retried["id"]))
        task.status, task.run_token = "RUNNING", "lease2"
        db.commit()
    result = {"selected": selected, "shortlisted_ids": [1, 2, 3, 4], "thread_id": "fixture"}
    monkeypatch.setattr(screenshots, "select_screenshots", lambda *args: result)
    ctx = TaskContext(retried["id"], "lease2")
    try:
        assert callable(screenshots.select_frames(ctx))
    finally:
        ctx.close()
    assert checks == [True], "Retry must reuse the completed B-frame checks"
