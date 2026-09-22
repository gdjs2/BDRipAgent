"""Database-owned queue: serialize admission, never hold a lock during media work."""

from sqlalchemy import func, or_, select, update

from shared.config import get_settings
from shared.models import MovieJob, QueueSettings, Task
from shared.state import TRACK_TASK_STATES

RELEASE_LIMIT = 1


def settings(db, *, lock=False):
    # Migrations seed this row. Also support fresh databases created by tests.
    if db.get(QueueSettings, 1) is None:
        if db.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        db.execute(
            insert(QueueSettings)
            .values(id=1, max_encoding_tasks=1, max_crf_tasks=1, max_other_tasks=3, paused=False)
            .on_conflict_do_nothing()
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
        .where(
            Task.status == "QUEUED",
            MovieJob.deleted_at.is_(None),
            or_(
                (Task.lane == "pipeline") & (Task.stage == MovieJob.state),
                *[
                    (Task.lane == "tracks") & (Task.stage == stage) & MovieJob.state.in_(states)
                    for stage, states in TRACK_TASK_STATES.items()
                ],
            ),
        )
        .order_by(Task.queue_priority.desc(), Task.created_at, Task.id)
    )
    if not include_held:
        query = query.where(Task.held.is_(False))
    return db.scalars(query).all()


def task_pool(task):
    return {"encode": "encoding", "crf_analysis": "crf"}.get(task.type, "other")


def limits(config):
    capacity = get_settings().worker_capacity
    return {
        "encoding": min(config.max_encoding_tasks, capacity),
        "crf": min(config.max_crf_tasks, capacity),
        "other": min(config.max_other_tasks, capacity),
    }


def available(db, config):
    if config.paused:
        return []
    running = db.scalars(select(Task).where(Task.status == "RUNNING")).all()
    remaining = limits(config)
    release_slots = RELEASE_LIMIT - sum(t.type == "generate_release" for t in running)
    # Paused encodes and cancellation requests keep their slot until the process stops.
    for task in running:
        remaining[task_pool(task)] -= 1
    slots = max(0, get_settings().worker_capacity - len(running))
    result = []
    for task in pending(db):
        if not slots:
            break
        pool = task_pool(task)
        if task.type == "generate_release" and release_slots <= 0:
            continue  # Serialize releases without holding up other kinds of work.
        if remaining[pool] <= 0:
            continue  # A full pool must not block ready work in another pool.
        remaining[pool] -= 1
        if task.type == "generate_release":
            release_slots -= 1
        result.append(task)
        slots -= 1
    return result


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
            "pool": task_pool(task),
            "lane": task.lane,
            "status": task.status,
            "held": task.held,
            "progress": task.progress,
            "cancel_requested": task.cancel_requested,
            "can_pause": task.can_pause,
            "pause_requested": task.pause_requested,
            "paused_at": task.paused_at,
            "created_at": task.created_at,
        }

    running = db.scalars(select(Task).where(Task.status == "RUNNING").order_by(Task.started_at)).all()
    return {
        "max_encoding_tasks": config.max_encoding_tasks,
        "max_crf_tasks": config.max_crf_tasks,
        "max_other_tasks": config.max_other_tasks,
        "max_release_tasks": RELEASE_LIMIT,
        "running_release_tasks": sum(t.type == "generate_release" for t in running),
        "effective_encoding_limit": limits(config)["encoding"],
        "effective_crf_limit": limits(config)["crf"],
        "effective_other_limit": limits(config)["other"],
        "running_encoding_tasks": sum(task_pool(t) == "encoding" for t in running),
        "running_crf_tasks": sum(task_pool(t) == "crf" for t in running),
        "running_other_tasks": sum(task_pool(t) == "other" for t in running),
        "capacity": get_settings().worker_capacity,
        "paused": config.paused,
        "running": [row(t) for t in running if t.job_id in jobs],
        "queued": [row(t) for t in pending(db, include_held=True)],
    }
