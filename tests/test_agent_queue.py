import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent import main as service
from agent.request_queue import AgentQueue


def test_fifo_cancel_timeout_and_failure_release():
    queue = AgentQueue()
    active, cancelled, expired, first, second = [queue.enqueue() for _ in range(5)]
    order, positions = [], []

    def run(ticket, name):
        with queue.slot(ticket, timeout=3, on_position=lambda p: positions.append((name, p))):
            order.append(name)
            assert queue.snapshot()["running"] == 1

    def cancel():
        raise RuntimeError("cancelled")

    with ThreadPoolExecutor(2) as pool:
        with queue.slot(active, timeout=1):
            with pytest.raises(RuntimeError, match="cancelled"):
                with queue.slot(cancelled, timeout=1, check=cancel):
                    pytest.fail("cancelled request ran")
            with pytest.raises(TimeoutError, match="prepared inputs are retained"):
                with queue.slot(expired, timeout=0.01):
                    pytest.fail("expired request ran")
            # Start the later ticket first; scheduling cannot bypass FIFO.
            b = pool.submit(run, second, "second")
            a = pool.submit(run, first, "first")
            time.sleep(0.03)
            assert not order
        a.result(timeout=4)
        b.result(timeout=4)
    assert order == ["first", "second"]
    assert ("second", 2) in positions
    with pytest.raises(ValueError):
        with queue.slot(queue.enqueue(), timeout=1):
            raise ValueError("model failed")
    assert queue.snapshot() == {"running": 0, "waiting": 0}


def test_all_endpoints_share_fifo(environment, monkeypatch):
    queue = AgentQueue()
    monkeypatch.setattr(service, "agent_queue", queue)
    calls = []
    for cls, method, label in [
        (service.CodexScreenshotSelector, "select", "screenshots"),
        (service.CodexSubtitleDiscovery, "run", "subtitles"),
        (service.CodexTrackReviewer, "review", "tracks"),
        (service.CodexSubtitleClassifier, "classify", "classification"),
    ]:

        def invoke(self, root, label=label):
            calls.append(label)
            assert queue.snapshot()["running"] == 1
            return {"label": label}

        monkeypatch.setattr(cls, method, invoke)

    def request(path, streaming=True):
        with TestClient(service.app) as client:
            response = client.post(
                path,
                json={"job_id": str(uuid4()), "task_id": str(uuid4()), "track_id": 0},
                headers={
                    "Authorization": f"Bearer {environment.agent_token}",
                    "Accept": "application/x-ndjson" if streaming else "application/json",
                },
            )
            assert response.status_code == 200
            return [json.loads(line) for line in response.text.splitlines()] if streaming else response.json()

    with ThreadPoolExecutor(5) as pool:
        futures = []
        with queue.slot(queue.enqueue(), timeout=1):
            for index, path in enumerate(
                ["/select", "/discover-subtitles", "/review-tracks", "/classify-subtitles", "/select"]
            ):
                futures.append(pool.submit(request, path, index != 4))
                deadline = time.monotonic() + 3
                while queue.snapshot()["waiting"] != index + 1:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
            assert not calls
        results = [future.result(timeout=5) for future in futures]
    assert calls == ["screenshots", "subtitles", "tracks", "classification", "screenshots"]
    for result in results[:-1]:
        assert any(m.get("agent_queue_state") == "waiting" for m in result)
        assert any(m.get("agent_queue_state") == "running" for m in result)
        assert result[-1]["type"] == "result"
    assert queue.snapshot() == {"running": 0, "waiting": 0}


def test_disconnect_while_queued_never_invokes_agent(monkeypatch, tmp_path):
    queue = AgentQueue()
    monkeypatch.setattr(service, "agent_queue", queue)
    calls = []
    monkeypatch.setattr(service.CodexScreenshotSelector, "select", lambda self, root: calls.append(root))

    async def disconnect():
        response = service.selection_stream(tmp_path)
        await anext(response.body_iterator)
        await response.body_iterator.aclose()

    with queue.slot(queue.enqueue(), timeout=1):
        asyncio.run(disconnect())
        deadline = time.monotonic() + 2
        while queue.snapshot()["waiting"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    assert not calls
    assert queue.snapshot() == {"running": 0, "waiting": 0}
