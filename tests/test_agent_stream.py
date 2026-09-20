import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent import codex_stream
from agent import main as agent_service
from agent.screenshot_agent import CodexScreenshotSelector
from backend.app.main import agent_events
from shared.db import session
from shared.models import Event, Task
from worker.adapters.screenshot_agent import consume_stream
from worker.runtime import TaskContext


@pytest.fixture
def fake_codex(environment, tmp_path):
    executable = tmp_path / "fake-codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, sys, time
from pathlib import Path
assert "API_TOKEN" not in os.environ and "AGENT_TOKEN" not in os.environ
assert sys.argv[1:3] == ["app-server", "--stdio"]
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    value = json.loads(line)
    method = value.get("method")
    if method == "initialize":
        send({"id": value["id"], "result": {}})
    elif method == "thread/start":
        assert value["params"]["sandbox"] == "read-only"
        assert value["params"]["approvalPolicy"] == "never"
        assert value["params"]["ephemeral"] is True
        send({"id": value["id"], "result": {"thread": {"id": "test-thread"}}})
    elif method == "turn/start":
        params = value["params"]
        assert params["outputSchema"]["properties"]["selected"]
        assert params["input"][1]["type"] == "localImage"
        assert Path(params["input"][1]["path"]).is_file()
        send({"id": value["id"], "result": {"turn": {"id": "turn"}}})
        send({"method": "item/reasoning/textDelta", "params": {"delta": "private reasoning"}})
        send({"method": "item/agentMessage/delta", "params": {"itemId": "answer", "delta": '{"selected":'}})
        if params["input"][0]["text"] == "Select the clearest frames":
            ack = Path(params["input"][1]["path"]).with_suffix(".ack")
            for _ in range(100):
                if ack.exists():
                    break
                time.sleep(0.02)
            assert ack.exists(), "Client did not forward the delta while the response was still running"
        time.sleep(0.4)
        if params["input"][0]["text"] == "timeout":
            time.sleep(10)
        send({"method": "item/agentMessage/delta", "params": {"itemId": "answer", "delta": '[]}'}})
        send({"method": "item/completed", "params": {"item": {"id": "answer", "type": "agentMessage", "phase": "final_answer", "text": '{"selected":[]}'}}})
        send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
"""
    )
    executable.chmod(0o755)
    environment.codex_bin = str(executable)
    image = tmp_path / "candidate.png"
    image.write_bytes(b"test image")
    return image


def test_codex_prompt_and_text_deltas_arrive_before_final_response(fake_codex):
    events = []

    def capture(event):
        events.append(event)
        if event["type"] == "delta":
            fake_codex.with_suffix(".ack").touch()

    selector = CodexScreenshotSelector(on_event=capture)
    selection, thread = selector._invoke("Select the clearest frames", [fake_codex])
    assert selection.selected == [] and thread == "test-thread"
    assert events[0]["type"] == "prompt"
    assert events[0]["text"] == "Select the clearest frames"
    assert events[0]["images"] == ["candidate.png"]
    assert [e["text"] for e in events if e["type"] == "delta"] == ['{"selected":', "[]}"]
    assert events[-1]["type"] == "complete"
    assert len({e["invocation_id"] for e in events}) == 1
    assert "private reasoning" not in json.dumps(events)


def test_codex_stream_timeout_kills_child(fake_codex, monkeypatch):
    real_popen = codex_stream.subprocess.Popen
    children = []

    def start(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(codex_stream.subprocess, "Popen", start)
    monkeypatch.setattr(codex_stream, "behavior", lambda: {"agent": {"timeout_seconds": 0.6}})
    with pytest.raises(TimeoutError, match="timed out"):
        CodexScreenshotSelector()._invoke("timeout", [fake_codex])
    assert all(child.poll() is not None for child in children)


def test_codex_stream_cancellation_stops_child(fake_codex, monkeypatch):
    real_popen = codex_stream.subprocess.Popen
    children, events = [], []

    def start(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def check():
        if any(event["type"] == "delta" for event in events):
            raise RuntimeError("Cancelled by user")

    monkeypatch.setattr(codex_stream.subprocess, "Popen", start)
    with pytest.raises(RuntimeError, match="Cancelled by user"):
        CodexScreenshotSelector(on_event=events.append, check=check)._invoke("select", [fake_codex])
    assert all(child.poll() is not None for child in children)


def test_agent_service_streams_result_and_releases_lock(environment, monkeypatch):
    from uuid import uuid4

    def select(self, root):
        self.on_event({"type": "prompt", "invocation_id": "run", "text": "Select"})
        self.on_event({"type": "delta", "invocation_id": "run", "item_id": "answer", "text": "hello"})
        return {"selected": []}

    monkeypatch.setattr(CodexScreenshotSelector, "select", select)
    with TestClient(agent_service.app) as client:
        response = client.post(
            "/select",
            json={"job_id": str(uuid4()), "task_id": str(uuid4())},
            headers={
                "Authorization": f"Bearer {environment.agent_token}",
                "Accept": "application/x-ndjson",
            },
        )
    assert response.status_code == 200
    records = [json.loads(line) for line in response.text.splitlines()]
    assert [r["type"] for r in records if r["type"] != "heartbeat"] == ["prompt", "delta", "result"]
    assert not agent_service.lock.locked()


def test_worker_persists_stream_and_task_endpoint_replays_it(client, new_job):
    import asyncio

    task_id = new_job["tasks"][0]["id"]
    with session() as db:
        task = db.get(Task, task_id)
        task.status, task.run_token = "RUNNING", "stream-test"
        db.commit()
    ctx = TaskContext(task_id, "stream-test")
    messages = [
        {"type": "prompt", "invocation_id": "run", "text": "Choose", "images": []},
        {"type": "delta", "invocation_id": "run", "item_id": "answer", "text": "part 1 "},
        {"type": "delta", "invocation_id": "run", "item_id": "answer", "text": "part 2"},
        {"type": "heartbeat"},
        {"type": "message", "invocation_id": "run", "item_id": "answer", "text": "part 1 part 2"},
        {"type": "result", "result": {"selected": []}},
    ]
    response = httpx.Response(200, text="\n".join(json.dumps(item) for item in messages))
    try:
        assert consume_stream(ctx, response) == {"selected": []}
    finally:
        ctx.close()
    with session() as db:
        records = db.scalars(select(Event).where(Event.type == "agent_output").order_by(Event.id)).all()
        assert [r.data["type"] for r in records] == ["prompt", "delta", "message"]
        assert records[1].data["text"] == "part 1 part 2"
        db.add(
            Event(
                job_id=ctx.job.id, type="agent_output", data={"task_id": "another-task", "text": "unrelated"}
            )
        )
        db.get(Task, task_id).status = "SUCCEEDED"
        db.commit()
        first_id = records[0].id

    async def read(last_id=None):
        request = SimpleNamespace(
            headers={} if last_id is None else {"last-event-id": str(last_id)},
            is_disconnected=AsyncMock(return_value=False),
        )
        with session() as db:
            response = await agent_events(task_id, request, db)
        return "".join([part async for part in response.body_iterator])

    output = asyncio.run(read())
    assert '"text": "Choose"' in output and "part 1 part 2" in output
    assert "unrelated" not in output and "event: terminal" in output
    resumed = asyncio.run(read(first_id))
    assert '"text": "Choose"' not in resumed and "part 1 part 2" in resumed
    client.headers.clear()
    assert client.get(f"/api/tasks/{task_id}/agent-events").status_code == 401
