"""Revision requests reuse the validated encode and the retained source inventory."""

from uuid import uuid4

from sqlalchemy import select

from backend.app import queue
from backend.app.track_choices import apply_shared_choices, source_key, source_peers, store_choices
from shared.config import get_settings
from shared.encoding import is_smoke_test
from shared.models import MovieJob, Screenshot, SourceTrackChoices, Task, now
from shared.paths import contained, job_dir
from shared.state import POST_TRACK_STAGES, REMUX_STATES, TRACK_EDIT_STAGES, Stage


def remux_status(db, job, *, ignore_task_id=None):
    settings = get_settings()
    result = {"available": False, "backup_days": settings.output_backup_days}
    if job.state not in REMUX_STATES:
        return {**result, "reason": "Finish the current stage before revising the mux."}
    active_tasks = select(Task.id).where(Task.job_id == job.id, Task.status.in_(["QUEUED", "RUNNING"]))
    if ignore_task_id is not None:
        active_tasks = active_tasks.where(Task.id != ignore_task_id)
    if db.scalar(active_tasks):
        return {**result, "reason": "Wait for the current task to finish."}
    if not (job.validation.get("valid") or job.validation.get("smoke_test")):
        return {**result, "reason": "A validated video is required."}
    try:
        contained(settings.completed_root, job.analysis.get("final_path", ""), exists=True)
        if not is_smoke_test(job):
            contained(
                job_dir(settings.workspace_root, job.id), job.analysis.get("encoded_path", ""), exists=True
            )
        source = contained(settings.source_root, job.source_path, exists=True)
        stat = source.stat()
        if stat.st_size != job.source_size or str(stat.st_mtime_ns) != job.source_mtime_ns:
            raise ValueError("Source changed")
    except (ValueError, OSError):
        return {**result, "reason": "The original source and retained encoded video must be available."}
    return {**result, "available": True, "reason": None}


def request_remux(db, job_id, body):
    from backend.app.services import advance, get_job

    queue.settings(db, lock=True)
    job = get_job(db, job_id, lock=True)
    status = remux_status(db, job)
    if not status["available"]:
        raise ValueError(status["reason"])
    previous_analysis = job.analysis
    record = store_choices(db, job, body)
    queue_remux(db, job, previous_analysis)
    for peer in db.scalars(
        select(MovieJob)
        .where(*source_peers(job), MovieJob.id != job.id)
        .order_by(MovieJob.id)
        .with_for_update()
    ):
        if sync_shared_choices(db, peer, record) and peer.state == Stage.WAITING_FOR_TRACK_SELECTION:
            advance(db, peer)
    db.commit()
    return job


def queue_remux(db, job, previous_analysis, *, automatic=False):
    from backend.app.services import enqueue, event

    previous = {
        "release_name": job.release_name,
        "final_path": previous_analysis.get("final_path"),
        "release_result": previous_analysis.get("release_result"),
    }
    cached = {**previous_analysis.get("prepared_track_cache", {})}
    for track in previous_analysis.get("prepared_tracks", []):
        cached[str(track["track_id"])] = track
    selected = db.scalars(
        select(Screenshot).where(Screenshot.job_id == job.id, Screenshot.selected.is_(True))
    ).all()
    expected = job.analysis.get("screenshot_selection", {}).get("count", job.screenshot_policy["count"])
    job.analysis = {
        **{key: value for key, value in job.analysis.items() if key != "release_result"},
        "prepared_track_cache": cached,
        "remux_revision": {
            "id": str(uuid4()),
            "requested_at": now().isoformat(),
            "previous": previous,
            "reuse_screenshots": bool(selected) and len(selected) == expected,
        },
    }
    job.state, job.completed_at = Stage.PREPARING_TRACKS.value, None
    enqueue(db, job)
    event(db, job.id, "remux_requested", revision=job.analysis["remux_revision"]["id"], automatic=automatic)
    event(db, job.id, "state_changed", state=job.state)


def sync_shared_choices(db, job, record=None):
    """Queue mutex and job lock are held by the caller, fencing worker claims."""
    from backend.app.services import event
    from backend.app.subtitle_discovery import active_discovery

    if job.state in TRACK_EDIT_STAGES:
        return apply_shared_choices(db, job, record)
    if job.state not in POST_TRACK_STAGES or job.deleted_at is not None:
        return False
    record = record or db.get(SourceTrackChoices, source_key(job))
    if (
        record is None
        or "audio_track_ids" not in record.data
        or job.analysis.get("shared_track_selection", {}).get("revision") == record.revision
        or active_discovery(db, job)
    ):
        return False
    tasks = db.scalars(
        select(Task).where(Task.job_id == job.id, Task.status.in_(["QUEUED", "RUNNING"]))
    ).all()
    # A running prepare/mux/release must retain its snapshot until it commits.
    if any(t.status == "RUNNING" or t.lane != "pipeline" for t in tasks):
        return False
    settings = get_settings()
    try:
        if not (job.validation.get("valid") or job.validation.get("smoke_test")):
            raise ValueError("A validated video is required for automatic remux.")
        if not is_smoke_test(job):
            contained(
                job_dir(settings.workspace_root, job.id), job.analysis.get("encoded_path", ""), exists=True
            )
        source = contained(settings.source_root, job.source_path, exists=True)
        stat = source.stat()
        if stat.st_size != job.source_size or str(stat.st_mtime_ns) != job.source_mtime_ns:
            raise ValueError("The original source changed; automatic remux cannot proceed.")
    except (ValueError, OSError):
        job.analysis = {
            **job.analysis,
            "shared_track_selection_error": "Automatic remux needs a validated encode and the unchanged source file. Restore the retained inputs to continue.",
        }
        return False
    previous_analysis = job.analysis
    if not apply_shared_choices(db, job, record, preparing_remux=True):
        return False
    for task in tasks:
        task.status, task.finished_at = "CANCELLED", now()
        task.cancel_requested = True
        event(db, job.id, "task_cancelled", task_id=task.id, reason="Superseded by shared track choices")
    db.flush()
    queue_remux(db, job, previous_analysis, automatic=True)
    return True
