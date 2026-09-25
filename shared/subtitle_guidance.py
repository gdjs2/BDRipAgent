"""Durable user follow-ups scoped to one subtitle task's retry ancestry."""

from sqlalchemy import select

from shared.models import Event, Task

SUBTITLE_TASKS = {"review_uploaded_subtitle", "discover_subtitles"}


def ancestry(db, task):
    rows, seen = [], set()
    while task and task.id not in seen:
        if rows and (task.job_id != rows[0].job_id or task.type != rows[0].type):
            break
        rows.append(task)
        seen.add(task.id)
        task = db.get(Task, task.retry_of) if task.retry_of else None
    return rows


def guidance_history(db, task):
    if task.type not in SUBTITLE_TASKS:
        return []
    ids = [row.id for row in ancestry(db, task)]
    return [
        {"id": row.id, "created_at": row.created_at.isoformat(), **row.data}
        for row in db.scalars(
            select(Event)
            .where(
                Event.job_id == task.job_id,
                Event.type == "subtitle_review_guidance",
                Event.data["task_id"].as_string().in_(ids),
            )
            .order_by(Event.id)
        )
    ]


def task_guidance(ctx):
    if not hasattr(ctx, "_subtitle_guidance"):
        from shared.db import session

        with session() as db:
            task = db.get(Task, ctx.task_id)
            ctx._subtitle_guidance = guidance_history(db, task) if task else []
    return ctx._subtitle_guidance


def previous_review(ctx):
    """Supply the previous attempt's actual response, including legacy failures."""
    if hasattr(ctx, "_previous_subtitle_review"):
        return ctx._previous_subtitle_review
    import json

    from shared.db import session
    from shared.paths import contained

    result = None
    messages = task_guidance(ctx)
    if messages:
        task_id = messages[-1]["previous_task_id"]
        try:
            state = json.loads(contained(ctx.workspace, f"subtitle-review-state/{task_id}.json").read_text())
            result = (state.get("answer") or {}).get("decision")
        except (OSError, ValueError, TypeError):
            pass
        if result is None:
            with session() as db:
                row = db.scalar(
                    select(Event)
                    .where(
                        Event.job_id == ctx.job.id,
                        Event.type == "agent_output",
                        Event.data["task_id"].as_string() == task_id,
                        Event.data["type"].as_string() == "message",
                    )
                    .order_by(Event.id.desc())
                    .limit(1)
                )
                if row:
                    try:
                        result = json.loads(row.data["text"])
                    except (ValueError, KeyError):
                        pass
        if not isinstance(result, dict):
            result = None
    ctx._previous_subtitle_review = result
    return result
