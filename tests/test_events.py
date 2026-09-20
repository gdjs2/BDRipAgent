import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from backend.app.main import events
from shared.db import session
from shared.models import Event


def test_new_event_stream_skips_history_and_delivers_subsequent_changes(client, new_job):
    with session() as db:
        rows = [Event(job_id=new_job["id"], type="artifact_created", data={"index": i}) for i in range(450)]
        db.add_all(rows)
        db.commit()
        last_id = rows[-1].id

    async def read():
        request = SimpleNamespace(headers={}, is_disconnected=AsyncMock(return_value=False))
        with session() as db:
            response = await events(UUID(new_job["id"]), request, db)
        stream = response.body_iterator
        try:
            assert await anext(stream) == f"id: {last_id}\nevent: ready\ndata: {{}}\n\n"
            with session() as db:
                row = Event(job_id=new_job["id"], type="task_completed", data={"new": True})
                db.add(row)
                db.commit()
                next_id = row.id
            message = await anext(stream)
            assert message.startswith(f"id: {next_id}\nevent: task_completed\n")
            assert '"new": true' in message
            assert response.headers["x-accel-buffering"] == "no"
        finally:
            await stream.aclose()

    asyncio.run(read())


def test_reconnecting_event_stream_replays_only_missed_events(client, new_job):
    with session() as db:
        first = Event(job_id=new_job["id"], type="task_started", data={})
        last = Event(job_id=new_job["id"], type="task_completed", data={})
        db.add_all([first, last])
        db.commit()
        first_id, last_id = first.id, last.id

    async def read():
        request = SimpleNamespace(
            headers={"last-event-id": str(first_id)}, is_disconnected=AsyncMock(return_value=False)
        )
        with session() as db:
            response = await events(UUID(new_job["id"]), request, db)
        stream = response.body_iterator
        try:
            assert f"id: {first_id}\nevent: ready" in await anext(stream)
            assert (await anext(stream)).startswith(f"id: {last_id}\nevent: task_completed\n")
        finally:
            await stream.aclose()

    asyncio.run(read())


@pytest.mark.parametrize("value", ["garbage", "-1", str(2**63)])
def test_event_stream_rejects_invalid_reconnect_ids(client, new_job, value):
    response = client.get(f"/api/jobs/{new_job['id']}/events", headers={"last-event-id": value})
    assert response.status_code == 400


def test_event_stream_still_requires_authentication(client, new_job):
    client.headers.clear()
    assert client.get(f"/api/jobs/{new_job['id']}/events").status_code == 401
