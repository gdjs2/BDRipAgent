import logging
import socket
import threading
import traceback
from datetime import timedelta
from uuid import uuid4

from celery import Celery
from celery.signals import worker_process_init, worker_ready
from sqlalchemy import select

from backend.app import queue
from backend.app.services import advance, event, get_job, manifest, reconcile
from shared.config import get_settings
from shared.db import session
from shared.models import MovieJob, Task, now
from worker.runtime import Interrupted, TaskContext

celery = Celery("bdripagent", broker=get_settings().redis_url)
celery.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 604800},
)


@worker_process_init.connect
def reset_database_pool(**kwargs):
    # Autoscaling forks after the dispatch thread has opened DB connections.
    # Child processes must never share the parent's PostgreSQL connections.
    from shared.db import engine

    if engine.cache_info().currsize:
        engine().dispose(close=False)


@celery.task(name="worker.dispatch")
def dispatch():
    with session() as db:
        reconcile(db)
        config = queue.settings(db, lock=True)
        tasks = queue.available(db, config)
        for task in tasks:
            # Re-delivery is safe: claim under DB lock before executing anything.
            if task.dispatched_at and task.dispatched_at.replace(tzinfo=now().tzinfo) > now() - timedelta(
                seconds=60
            ):
                continue
            execute.apply_async(args=[task.id], task_id=task.id)
            task.dispatched_at = now()
        db.commit()


@worker_ready.connect
def ready(**kwargs):
    # Runs in the worker parent, so an hours-long encode cannot starve dispatch/recovery.
    def loop():
        while True:
            try:
                dispatch()
            except Exception:
                logging.getLogger(__name__).exception("Queue dispatch failed; will retry")
            threading.Event().wait(10)

    threading.Thread(target=loop, name="database-outbox", daemon=True).start()


@celery.task(name="worker.execute")
def execute(task_id):
    from worker.pipeline.stages import HANDLERS

    token = str(uuid4())
    with session() as db:
        # All workers serialize admission through the singleton row. Recheck
        # eligibility here: broker messages can arrive after pause/hold/reorder.
        config = queue.settings(db, lock=True)
        task = db.get(Task, task_id)
        if not task:
            return
        job = db.scalar(
            select(MovieJob)
            .where(MovieJob.id == task.job_id, MovieJob.deleted_at.is_(None))
            .with_for_update()
        )
        db.refresh(task)
        if not job or task.status != "QUEUED" or job.state != task.stage:
            return
        if task.id not in {t.id for t in queue.available(db, config)}:
            task.dispatched_at = None
            db.commit()
            return
        task.status, task.run_token = "RUNNING", token
        task.worker_id = socket.gethostname()
        task.started_at = task.heartbeat_at = now()
        kind = task.type
        event(db, job.id, "task_started", task_id=task.id)
        db.commit()
    context = None
    try:
        context = TaskContext(task_id, token)
        result = HANDLERS[kind](context)
        context.check()
        with session() as db:
            job = get_job(db, context.job.id, lock=True)
            task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task.run_token != token or task.status != "RUNNING" or task.cancel_requested:
                raise Interrupted("Task lost its lease before completion")
            if result:
                result(db, job)
            task.status, task.progress, task.exit_code = "SUCCEEDED", 100, 0
            task.finished_at = now()
            task.can_pause, task.pause_requested, task.paused_at = False, False, None
            event(db, job.id, "task_completed", task_id=task_id)
            db.flush()  # Release unique active-task slot before creating the next task.
            advance(db, job)
            db.commit()
            manifest(db, job)
    except Exception as error:
        if context:
            context.log(traceback.format_exc())
        with session() as db:
            task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task and task.run_token == token and task.status == "RUNNING":
                task.status = "CANCELLED" if task.cancel_requested else "FAILED"
                task.error_message = str(error)
                task.exit_code = getattr(error, "exit_code", None)
                task.finished_at = now()
                task.can_pause, task.pause_requested, task.paused_at = False, False, None
                event(db, task.job_id, "task_failed", task_id=task.id, error=str(error))
                db.commit()
    finally:
        if context:
            try:
                context.artifact(context.log_path, "LOG")
            except Exception:
                pass  # Logs remain addressable through the task even if artifact registration fails.
            context.close()
