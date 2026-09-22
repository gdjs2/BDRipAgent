"""Source-shared, quarantined text uploads awaiting the subtitle review worker."""

from sqlalchemy import select

from backend.app import queue
from backend.app.track_choices import source_key, source_peers
from shared.db import session
from shared.models import MovieJob, SourceTrackChoices, Task
from shared.state import Stage
from shared.tracks import track_analysis_complete


def queue_upload(job_id, path, filename, language, hearing_impaired, upload_id):
    from backend.app.services import enqueue, get_job
    from backend.app.subtitle_uploads import upload_allowed, validate_subtitle
    from shared.config import get_settings

    validate_subtitle(path, path.suffix)
    with session() as db:
        queue.settings(db, lock=True)
        job = get_job(db, job_id, lock=True)
        upload_allowed(db, job)
        if not track_analysis_complete(job.analysis):
            raise ValueError("Wait for source track analysis before uploading text subtitles for alignment")
        if db.scalar(
            select(Task.id)
            .join(MovieJob, MovieJob.id == Task.job_id)
            .where(*source_peers(job), Task.lane == "tracks", Task.status.in_(["QUEUED", "RUNNING"]))
        ):
            raise ValueError("Wait for the current subtitle search or track review to finish")
        record = db.get(SourceTrackChoices, source_key(job))
        if record is None:
            record = SourceTrackChoices(source_key=source_key(job), revision=0, data={})
            db.add(record)
        task = enqueue(db, job, stage=Stage.REVIEWING_SUBTITLE_UPLOAD)
        entry = {
            "id": upload_id,
            "task_id": task.id,
            "job_id": job.id,
            "filename": filename,
            "language": language,
            "hearing_impaired": hearing_impaired,
            "path": str(path.relative_to(get_settings().workspace_root)),
        }
        record.data = {**record.data, "subtitle_imports": [*record.data.get("subtitle_imports", []), entry]}
        db.commit()
        return {"queued": True, "duplicate": False, "task_id": task.id, "upload_id": upload_id}


def imports_status(db, job):
    record = db.get(SourceTrackChoices, source_key(job))
    result = []
    for entry in record.data.get("subtitle_imports", []) if record else []:
        task = db.get(Task, entry["task_id"])
        if not task:
            continue
        # Generic task retries retain the immutable upload through their retry chain.
        while retry := db.scalar(
            select(Task).where(Task.retry_of == task.id).order_by(Task.created_at.desc())
        ):
            task = retry
        result.append(
            {
                "id": entry["id"],
                "filename": entry["filename"],
                "language": entry["language"],
                "task_id": task.id,
                "status": task.status,
                "detail": task.progress_detail,
                "error": task.error_message,
            }
        )
    return result
