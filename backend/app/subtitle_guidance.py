"""Continue a blocked subtitle review with a durable user message."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from backend.app import queue
from backend.app.services import enqueue, event, get_job, serialize
from backend.app.track_choices import source_key, source_peers
from shared.db import session
from shared.models import MovieJob, SourceTrackChoices, Task
from shared.subtitle_guidance import SUBTITLE_TASKS, guidance_history

router = APIRouter()


class ContinueReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=8000)
    recheck_completed: bool = False

    @field_validator("message")
    @classmethod
    def content(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Enter review guidance without null characters")
        return value.strip()


def subtitle_task(db, ident):
    task = db.get(Task, str(ident))
    if not task or task.type not in SUBTITLE_TASKS:
        raise LookupError("Subtitle review task not found")
    get_job(db, task.job_id)
    return task


@router.get("/tasks/{task_id}/subtitle-guidance")
def history(task_id: UUID):
    with session() as db:
        return guidance_history(db, subtitle_task(db, task_id))


@router.post("/tasks/{task_id}/continue-subtitle-review", status_code=202)
def continue_review(task_id: UUID, body: ContinueReview):
    with session() as db:
        queue.settings(db, lock=True)
        task = subtitle_task(db, task_id)
        job = get_job(db, task.job_id, lock=True)
        db.refresh(task)
        latest = db.scalar(
            select(Task)
            .where(Task.job_id == job.id, Task.stage == task.stage)
            .order_by(Task.created_at.desc())
            .limit(1)
        )
        if latest.id != task.id:
            raise ValueError("Continue the latest subtitle review attempt")
        from backend.app.subtitle_discovery import status
        from backend.app.subtitle_uploads import upload_allowed

        if task.type == "review_uploaded_subtitle":
            upload_allowed(db, job)
            allowed = task.status in ("FAILED", "CANCELLED")
        else:
            record = db.get(SourceTrackChoices, source_key(job))
            report = record.data.get("subtitle_discovery", {}) if record else {}
            allowed = task.status in ("FAILED", "CANCELLED") or (
                task.status == "SUCCEEDED"
                and report.get("task_id") == task.id
                and report.get("needs_review")
                and (
                    report.get("missing")
                    or any(
                        candidate.get("status") == "needs_review"
                        for candidate in report.get("candidates", [])
                    )
                )
            )
            if not status(db, job)["allowed"]:
                raise ValueError("Wait for track analysis or for the video to be ready for remux")
        if not allowed:
            raise ValueError("This subtitle review is not waiting for further guidance")
        if db.scalar(
            select(Task.id)
            .join(MovieJob, MovieJob.id == Task.job_id)
            .where(*source_peers(job), Task.lane == "tracks", Task.status.in_(["QUEUED", "RUNNING"]))
        ):
            raise ValueError("Wait for the current subtitle search or track review to finish")
        previous = guidance_history(db, task)
        if len(previous) >= 20 or sum(len(item["message"]) for item in previous) + len(body.message) > 32000:
            raise ValueError("This review has reached its guidance limit; start a new review")
        result = enqueue(db, job, retry_of=task)
        event(
            db,
            job.id,
            "subtitle_review_guidance",
            task_id=result.id,
            previous_task_id=task.id,
            message=body.message,
            recheck_completed=body.recheck_completed,
            previous_error=task.error_message or "Review the unresolved subtitle discovery report",
        )
        if task.type == "discover_subtitles":
            job.analysis = {**job.analysis, "subtitle_discovery_refresh": True}
        db.commit()
        return serialize(result)
