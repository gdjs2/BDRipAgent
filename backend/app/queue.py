"""Database-owned queue: serialize admission, never hold a lock during media work."""

from sqlalchemy import func, select, update

from shared.config import get_settings
from shared.models import MovieJob, QueueSettings, Task


def settings(db, *, lock=False):
    # Migrations seed this row. Also support fresh databases created by tests.
    if db.get(QueueSettings, 1) is None:
        if db.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        db.execute(
            insert(QueueSettings).values(id=1, max_concurrent_jobs=1, paused=False).on_conflict_do_nothing()
        )
    if lock and db.get_bind().dialect.name == "sqlite":
        # SQLite has no SELECT FOR UPDATE; a write reserves this short transaction.
        db.execute(update(QueueSettings).where(QueueSettings.id == 1).values(id=1))
    query = select(QueueSettings).where(QueueSettings.id == 1).execution_options(populate_existing=True)
    return db.scalar(query.with_for_update() if lock else query)


def running_count(db):
    # Paused encodes retain their slot. Count cancellations until the process has stopped.
    return db.scalar(select(func.count()).select_from(Task).where(Task.status == "RUNNING"))


def pending(db, *, include_held=False):
    query = (
        select(Task)
        .join(MovieJob, MovieJob.id == Task.job_id)
        .where(Task.status == "QUEUED", MovieJob.deleted_at.is_(None), Task.stage == MovieJob.state)
        .order_by(Task.queue_priority.desc(), Task.created_at, Task.id)
    )
    if not include_held:
        query = query.where(Task.held.is_(False))
    return db.scalars(query).all()


def limit(config):
    return min(config.max_concurrent_jobs, get_settings().worker_capacity)


def available(db, config):
    if config.paused:
        return []
    return pending(db)[: max(0, limit(config) - running_count(db))]


def snapshot(db):
    config = settings(db)
    jobs = {j.id: j for j in db.scalars(select(MovieJob).where(MovieJob.deleted_at.is_(None)))}

    def row(task):
        job = jobs[task.job_id]
        return {
            "id": task.id,
            "job_id": job.id,
            "title": job.title,
            "profile": job.analysis_profile,
            "smoke_test": job.analysis.get("smoke_test") is True,
            "type": task.type,
            "status": task.status,
            "held": task.held,
            "progress": task.progress,
            "cancel_requested": task.cancel_requested,
            "can_pause": task.can_pause,
            "pause_requested": task.pause_requested,
            "paused_at": task.paused_at,
            "created_at": task.created_at,
        }

    return {
        "max_concurrent_jobs": config.max_concurrent_jobs,
        "effective_limit": limit(config),
        "capacity": get_settings().worker_capacity,
        "paused": config.paused,
        "running": [
            row(t)
            for t in db.scalars(select(Task).where(Task.status == "RUNNING").order_by(Task.started_at))
            if t.job_id in jobs
        ],
        "queued": [row(t) for t in pending(db, include_held=True)],
    }
