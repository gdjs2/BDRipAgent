import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.app.agent_activity import live_events
from shared.db import session
from shared.models import Event, MovieJob


def record(job, kind, **data):
    with session() as db:
        row = Event(job_id=job["id"], type=kind, data={"task_id": job["tasks"][0]["id"], **data})
        db.add(row)
        db.commit()
        return row.id


def test_history_has_complete_prompts_pages_and_job_context(client, new_job):
    for i in range(3):
        record(
            new_job,
            "agent_output",
            type="prompt",
            invocation_id=str(i),
            text=f"Prompt {i}",
            stage="Subtitle cleanup",
        )
        record(
            new_job, "agent_output", type="delta", invocation_id=str(i), item_id="reply", text=f"Response {i}"
        )
        record(new_job, "agent_output", type="complete", invocation_id=str(i), text="Done")
    first = client.get("/api/agent/history?limit=2").json()
    assert first["has_more"]
    assert [e["text"] for e in first["events"] if e["type"] == "prompt"] == ["Prompt 1", "Prompt 2"]
    assert first["events"][0]["title"] == new_job["title"]
    assert first["events"][0]["job_id"] == new_job["id"]
    assert first["cursor"] >= first["events"][-1]["event_id"]
    older = client.get(f"/api/agent/history?limit=2&before_id={first['before_id']}").json()
    assert not older["has_more"]
    assert [e["text"] for e in older["events"]] == ["Prompt 0", "Response 0", "Done"]
    with session() as db:
        from shared.models import now

        db.get(MovieJob, new_job["id"]).deleted_at = now()
        db.commit()
    assert client.get("/api/agent/history").json()["events"] == []


def test_stream_resumes_after_snapshot_and_reports_task_failure(client, new_job):
    record(new_job, "agent_output", type="prompt", invocation_id="run", text="Prompt")
    cursor = client.get("/api/agent/history").json()["cursor"]
    next_id = record(
        new_job, "agent_output", type="delta", invocation_id="run", item_id="reply", text="Live response"
    )
    record(new_job, "task_failed", error="Agent connection lost")

    async def read():
        request = SimpleNamespace(
            headers={"last-event-id": str(cursor)}, is_disconnected=AsyncMock(return_value=False)
        )
        response = await live_events(request, after=0)
        stream = response.body_iterator
        try:
            return [await anext(stream), await anext(stream)]
        finally:
            await stream.aclose()

    messages = asyncio.run(read())
    assert f"id: {next_id}" in messages[0] and "Live response" in messages[0]
    assert "Prompt" not in "".join(messages)
    assert '"type": "task_end"' in messages[1] and "Agent connection lost" in messages[1]


def test_agent_activity_authentication_and_cursor_validation(client):
    for cursor in ["nope", "-1", str(2**63)]:
        assert client.get("/api/agent/events", headers={"Last-Event-ID": cursor}).status_code == 400
    assert client.get("/api/agent/history?limit=100").status_code == 422
    client.headers.clear()
    assert client.get("/api/agent/history").status_code == 401
    assert client.get("/api/agent/events").status_code == 401


def test_older_page_keeps_late_response_after_next_prompt(client, new_job):
    record(new_job, "agent_output", type="prompt", invocation_id="first", text="First prompt")
    record(new_job, "agent_output", type="prompt", invocation_id="second", text="Second prompt")
    record(
        new_job, "agent_output", type="message", invocation_id="first", item_id="a", text="Late first answer"
    )
    record(new_job, "agent_output", type="complete", invocation_id="first", text="Complete")
    latest = client.get("/api/agent/history?limit=1").json()
    older = client.get(f"/api/agent/history?limit=1&before_id={latest['before_id']}").json()
    assert [e["text"] for e in older["events"]] == ["First prompt", "Late first answer", "Complete"]
    assert [e["text"] for e in latest["events"]] == ["Second prompt"]
