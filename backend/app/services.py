from datetime import timedelta

import yaml
from sqlalchemy import select

from shared.config import behavior, get_settings
from shared.models import (
    Artifact,
    CRFResult,
    EncodeConfig,
    Event,
    MovieJob,
    MovieTrack,
    Task,
    TrackSelection,
    now,
)
from shared.paths import atomic_text, job_dir
from shared.state import HUMAN_GATES, TASK_TYPES, Stage, next_stage


def event(db, job_id, kind, **data):
    db.add(Event(job_id=job_id, type=kind, data=data))


def get_job(db, job_id, *, lock=False):
    query = select(MovieJob).where(MovieJob.id == job_id, MovieJob.deleted_at.is_(None))
    if lock:
        query = query.with_for_update()
    job = db.scalar(query)
    if not job:
        raise LookupError("Job not found")
    return job


def enqueue(db, job, *, retry_of=None):
    if job.state not in TASK_TYPES:
        raise ValueError("This stage requires a user decision or is complete")
    active = db.scalar(select(Task).where(Task.job_id == job.id, Task.status.in_(["QUEUED", "RUNNING"])))
    if active:
        return active
    task = Task(
        job_id=job.id,
        type=TASK_TYPES[Stage(job.state)],
        stage=job.state,
        retry_of=retry_of.id if retry_of else None,
        attempt=retry_of.attempt + 1 if retry_of else 1,
    )
    db.add(task)
    db.flush()
    task.log_path = f"logs/{task.id}.log"
    event(db, job.id, "task_queued", task_id=task.id, stage=job.state)
    return task


def advance(db, job):
    job.state = next_stage(job.state).value
    event(db, job.id, "state_changed", state=job.state)
    if job.state in HUMAN_GATES:
        event(db, job.id, "user_action_required", state=job.state)
    elif job.state == Stage.COMPLETE:
        job.completed_at = now()
    else:
        enqueue(db, job)


def manifest(db, job):
    selection = db.scalar(select(TrackSelection).where(TrackSelection.job_id == job.id))
    encode = db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job.id))
    crf = db.scalar(select(CRFResult).where(CRFResult.job_id == job.id))
    value = {
        "job": {
            "id": job.id,
            "title": job.title,
            "year": job.year,
            "imdb_id": job.imdb_id,
            "state": job.state,
            "release_name": job.release_name,
        },
        "source": {"path": job.source_path, "size": job.source_size, "mtime_ns": job.source_mtime_ns},
        "analysis": job.analysis,
        "imdb_metadata": job.imdb_metadata,
        "tracks": {
            "audio": selection.audio_track_ids,
            "subtitles": selection.subtitle_track_ids,
            "selected_by": selection.selected_by,
        }
        if selection
        else {},
        "crf_studio": crf.data if crf else {},
        "encode": encode.data if encode else {},
        "validation": job.validation,
        "screenshots": job.screenshot_policy,
    }
    atomic_text(
        job_dir(get_settings().workspace_root, job.id) / "manifest.yaml",
        yaml.safe_dump(value, sort_keys=False, allow_unicode=True),
    )


def reconcile(db):
    """Lease fencing: a surviving worker must renew its lease or stop processing."""
    cutoff = now() - timedelta(seconds=behavior()["task_lease_seconds"])
    stale = db.scalars(
        select(Task)
        .where(Task.status == "RUNNING", Task.heartbeat_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for task in stale:
        task.status = "CANCELLED" if task.cancel_requested else "FAILED"
        task.error_message = "Worker lease expired; task interrupted. Retry resumes this stage."
        task.finished_at = now()
        task.run_token = None
        task.can_pause, task.pause_requested, task.paused_at = False, False, None
        event(db, task.job_id, "task_failed", task_id=task.id, error=task.error_message)
    db.commit()


def serialize(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def job_detail(db, job):
    value = serialize(job)
    for key, model in [("tracks", MovieTrack), ("tasks", Task), ("artifacts", Artifact)]:
        value[key] = [serialize(r) for r in db.scalars(select(model).where(model.job_id == job.id))]
    for key, model in [
        ("track_selection", TrackSelection),
        ("encode_config", EncodeConfig),
        ("crf", CRFResult),
    ]:
        row = db.scalar(select(model).where(model.job_id == job.id))
        value[key] = serialize(row) if row else None
    return value
