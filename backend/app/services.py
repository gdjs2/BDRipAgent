from datetime import timedelta

import yaml
from sqlalchemy import select, update

from backend.app import queue
from backend.app.release_defaults import analysis_with_release_defaults
from backend.app.track_choices import (
    apply_shared_choices,
    choices_current,
    seed_existing_choices,
    shared_choices_status,
    shared_choices_view,
)
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
from shared.state import (
    HUMAN_GATES,
    POST_TRACK_STAGES,
    TASK_TYPES,
    TRACK_EDIT_STAGES,
    TRACK_TASK_STATES,
    Stage,
    next_stage,
)
from shared.tracks import track_analysis_complete


def event(db, job_id, kind, **data):
    db.add(Event(job_id=job_id, type=kind, data=data))


def get_job(db, job_id, *, lock=False):
    if lock and db.get_bind().dialect.name == "sqlite":
        db.execute(update(MovieJob).where(MovieJob.id == job_id).values(id=MovieJob.id))
    query = (
        select(MovieJob)
        .where(MovieJob.id == job_id, MovieJob.deleted_at.is_(None))
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    job = db.scalar(query)
    if not job:
        raise LookupError("Job not found")
    return job


def enqueue(db, job, *, retry_of=None, stage=None):
    stage = stage or (retry_of.stage if retry_of else job.state)
    lane = (
        "screenshots"
        if stage == Stage.SYNCING_SCREENSHOTS
        else "tracks"
        if stage in TRACK_TASK_STATES
        else "pipeline"
    )
    if stage not in TASK_TYPES:
        raise ValueError("This stage requires a user decision or is complete")
    active = db.scalar(
        select(Task).where(Task.job_id == job.id, Task.lane == lane, Task.status.in_(["QUEUED", "RUNNING"]))
    )
    if active:
        return active
    task = Task(
        job_id=job.id,
        type=TASK_TYPES[Stage(stage)],
        stage=stage,
        lane=lane,
        retry_of=retry_of.id if retry_of else None,
        attempt=retry_of.attempt + 1 if retry_of else 1,
    )
    db.add(task)
    db.flush()
    task.log_path = f"logs/{task.id}.log"
    event(db, job.id, "task_queued", task_id=task.id, stage=stage, lane=lane)
    return task


def advance(db, job):
    if job.state == Stage.ANALYZING_SOURCE:
        job.state = Stage.RUNNING_CRF_ANALYSIS.value
        ensure_track_analysis(db, job)
    elif job.state == Stage.PREPARING_TRACKS and not (
        job.validation.get("valid") or job.validation.get("smoke_test")
    ):
        # Existing jobs may already be preparing tracks under the earlier sequential workflow.
        job.state = Stage.RUNNING_CRF_ANALYSIS.value
    elif job.state == Stage.REMUXING and job.analysis.get("remux_revision", {}).get("reuse_screenshots"):
        job.state = Stage.SCREENSHOT_RENDERING.value
    else:
        job.state = next_stage(job.state).value
    apply_shared_choices(db, job)
    if (
        job.state == Stage.WAITING_FOR_TRACK_SELECTION
        and choices_current(db, job)
        and db.scalar(select(TrackSelection).where(TrackSelection.job_id == job.id))
    ):
        job.state = (
            Stage.REMUXING.value if "prepared_tracks" in job.analysis else Stage.PREPARING_TRACKS.value
        )
    event(db, job.id, "state_changed", state=job.state)
    if job.state in HUMAN_GATES:
        event(db, job.id, "user_action_required", state=job.state)
    elif job.state == Stage.COMPLETE:
        job.completed_at = now()
    else:
        enqueue(db, job)


def manifest(db, job):
    # Serialize snapshots as well as database updates across independent task lanes.
    job = get_job(db, job.id, lock=True)
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
    db.commit()


def ensure_track_analysis(db, job):
    """Review independently of the video pipeline; failures require an explicit retry."""
    if job.state not in TRACK_EDIT_STAGES or track_analysis_complete(job.analysis):
        return False
    if db.scalar(select(TrackSelection).where(TrackSelection.job_id == job.id)):
        return False
    if db.scalar(select(Task).where(Task.job_id == job.id, Task.type == "review_tracks")):
        return False
    enqueue(db, job, stage=Stage.ANALYZING_TRACKS)
    return True


def task_is_current(job, task):
    if task.lane == "screenshots":
        from shared.state import SCREENSHOT_SYNC_STATES

        return job.state in SCREENSHOT_SYNC_STATES
    if task.lane == "tracks":
        return job.state in TRACK_TASK_STATES.get(task.stage, set())
    return job.state == task.stage


def reconcile(db):
    """Lease fencing: a surviving worker must renew its lease or stop processing."""
    queue.settings(db, lock=True)
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
    # Legacy waiting jobs are upgraded automatically once. Inconclusive findings
    # are completed reviews, and failures stay visible for an explicit task retry.
    for job in db.scalars(
        select(MovieJob)
        .where(MovieJob.state.in_(TRACK_EDIT_STAGES), MovieJob.deleted_at.is_(None))
        .with_for_update(skip_locked=True)
    ):
        # Upgrade an old track-selection gate that still sits before CRF analysis.
        # A newly reached remux gate already has validation and must stay put.
        if (
            job.state == Stage.WAITING_FOR_TRACK_SELECTION
            and job.analysis.get("video")
            and "crop" in job.analysis
            and not job.validation
            and not db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job.id))
            and not db.scalar(
                select(Task.id).where(
                    Task.job_id == job.id, Task.lane == "pipeline", Task.status.in_(["QUEUED", "RUNNING"])
                )
            )
        ):
            job.state = (
                Stage.WAITING_FOR_ENCODE_SELECTION.value
                if db.scalar(select(CRFResult).where(CRFResult.job_id == job.id))
                else Stage.RUNNING_CRF_ANALYSIS.value
            )
            event(db, job.id, "state_changed", state=job.state)
            if job.state == Stage.RUNNING_CRF_ANALYSIS:
                enqueue(db, job)
        ensure_track_analysis(db, job)
        from backend.app.subtitle_discovery import ensure_discovery

        # Queue opt-in discovery before a saved selection can open the remux gate.
        ensure_discovery(db, job)
        seed_existing_choices(db, job)
        apply_shared_choices(db, job)
        if (
            job.state == Stage.WAITING_FOR_TRACK_SELECTION
            and choices_current(db, job)
            and db.scalar(select(TrackSelection).where(TrackSelection.job_id == job.id))
        ):
            advance(db, job)
    from backend.app.remux import sync_shared_choices

    for job in db.scalars(
        select(MovieJob)
        .where(MovieJob.state.in_(POST_TRACK_STAGES), MovieJob.deleted_at.is_(None))
        .order_by(MovieJob.id)
        .with_for_update(skip_locked=True)
    ):
        sync_shared_choices(db, job)
    db.commit()


def serialize(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def serialize_tracks(db, job, *, shared_view=None):
    from backend.app.track_choices import original_languages
    from shared.naming import language_name, normalize_audio_label
    from shared.original_languages import is_original

    originals = original_languages(db, job)

    # JSON scan order is authoritative for existing jobs; row/UUID order is not.
    scanned = {track["track_id"]: index for index, track in enumerate(job.analysis.get("tracks", []))}
    values = []
    from backend.app.track_choices import track_rows

    for row in track_rows(db, job).values():
        value = (
            serialize(row)
            if isinstance(row, MovieTrack)
            else {"track_id": row.track_id, "kind": row.kind, "job_id": job.id}
        )
        info = shared_view[1].get(row.track_id, row.info) if shared_view else row.info
        info = {
            **info,
            **{
                key: normalize_audio_label({**info, "kind": row.kind}, info[key])
                for key in ("mux_name", "name_override", "suggested_name", "base_name")
                if info.get(key)
            },
        }
        value["info"] = {
            **info,
            "language_name": language_name(info.get("language")),
            "original": is_original({**info, "kind": row.kind}, originals)
            if originals or row.kind == "video"
            else None,
            "source_order": row.info.get("source_order", scanned.get(row.track_id, row.track_id)),
        }
        values.append(value)
    return sorted(values, key=lambda value: (value["info"]["source_order"], value["track_id"]))


def job_detail(db, job):
    value = serialize(job)
    from backend.app.track_choices import original_languages
    from shared.naming import language_name

    value["original_languages"] = [
        {"code": code, "name": language_name(code)} for code in original_languages(db, job)
    ]
    value["analysis"] = analysis_with_release_defaults(db, job)
    if job.analysis.get("video"):
        from backend.app.encode_summary import source_bitrate

        value["analysis"]["video"] = {
            **job.analysis["video"],
            "bit_rate": source_bitrate(db, job),
            "bit_rate_scope": "video",
        }
    from backend.app.reencode import status as reencode_status

    value["reencode"] = reencode_status(db, job)
    value["track_analysis_complete"] = track_analysis_complete(job.analysis)
    value["tracks_editable"] = job.state in TRACK_EDIT_STAGES
    from backend.app.remux import remux_status

    value["remux"] = remux_status(db, job)
    value["shared_track_selection"] = shared_choices_status(db, job)
    shared_view = shared_choices_view(db, job)
    value["tracks"] = serialize_tracks(db, job, shared_view=shared_view)
    from backend.app.subtitle_discovery import status as discovery_status

    value["subtitle_discovery"] = discovery_status(db, job)
    from backend.app.subtitle_imports import imports_status

    value["subtitle_uploads"] = imports_status(db, job)
    for key, model in [("tasks", Task), ("artifacts", Artifact)]:
        value[key] = [serialize(r) for r in db.scalars(select(model).where(model.job_id == job.id))]
    for key, model in [
        ("track_selection", TrackSelection),
        ("encode_config", EncodeConfig),
        ("crf", CRFResult),
    ]:
        row = db.scalar(select(model).where(model.job_id == job.id))
        value[key] = serialize(row) if row else None
    if shared_view and value["track_selection"]:
        value["track_selection"] = {
            **value["track_selection"],
            "audio_track_ids": list(shared_view[0]["audio_track_ids"]),
            "subtitle_track_ids": list(shared_view[0]["subtitle_track_ids"]),
        }
    return value
