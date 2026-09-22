"""Authenticated, source-wide agent history and resumable live events."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from shared.db import session
from shared.models import Event, MovieJob, Task

router = APIRouter()
TYPES = ("agent_output", "task_completed", "task_failed", "task_cancelled")


def visible_events():
    return (
        select(Event, MovieJob, Task)
        .join(MovieJob, MovieJob.id == Event.job_id)
        .join(Task, Task.id == Event.data["task_id"].as_string())
        .where(MovieJob.deleted_at.is_(None), Event.type.in_(TYPES))
    )


def envelope(row):
    event, job, task = row
    return {
        **event.data,
        "type": event.data.get("type") if event.type == "agent_output" else "task_end",
        "event_id": event.id,
        "job_id": job.id,
        "title": job.title,
        "year": job.year,
        "profile": job.analysis_profile or "",
        "task_id": task.id,
        "task_type": task.type,
        "task_status": task.status,
        "created_at": event.created_at.isoformat(),
    }


@router.get("/agent/history")
def history(before_id: int | None = Query(default=None, ge=1), limit: int = Query(default=8, ge=1, le=20)):
    with session() as db:
        head = db.scalar(select(func.max(Event.id))) or 0
        upper = min(head, before_id - 1) if before_id is not None else head
        prompts = db.execute(
            visible_events()
            .where(
                Event.id <= upper, Event.type == "agent_output", Event.data["type"].as_string() == "prompt"
            )
            .order_by(Event.id.desc())
            .limit(limit + 1)
        ).all()
        chosen = prompts[:limit]
        if not chosen:
            return {"events": [], "cursor": head, "before_id": None, "has_more": False}
        lower = chosen[-1][0].id
        keys = {(row[2].id, row[0].data.get("invocation_id")) for row in chosen}
        rows = db.execute(
            visible_events().where(Event.id >= lower, Event.id <= head).order_by(Event.id)
        ).all()
        # A long-running invocation can overlap a history page. Only emit the
        # invocations whose prompt is included; never replay another one's deltas.
        events = [envelope(row) for row in rows if (row[2].id, row[0].data.get("invocation_id")) in keys]
        return {"events": events, "cursor": head, "before_id": lower, "has_more": len(prompts) > limit}


@router.get("/agent/events")
async def live_events(request: Request, after: int = Query(default=0, ge=0, le=2**63 - 1)):
    try:
        cursor = int(request.headers.get("last-event-id", str(after)))
        if not 0 <= cursor <= 2**63 - 1:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Invalid Last-Event-ID") from None

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            with session() as db:
                rows = db.execute(
                    visible_events().where(Event.id > cursor).order_by(Event.id).limit(200)
                ).all()
                messages = [envelope(row) for row in rows]
            for message in messages:
                cursor = message["event_id"]
                yield f"id: {cursor}\nevent: agent_activity\ndata: {json.dumps(message)}\n\n"
            if not messages:
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.05 if messages else 0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
