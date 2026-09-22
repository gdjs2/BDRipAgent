"""Retire only known output files; source tracks and encoded video never expire."""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from shared.config import get_settings
from shared.db import session
from shared.models import Artifact, Event, MovieJob, Task, now
from shared.paths import artifact_root, contained

OUTPUT_KINDS = {
    "FINAL_MKV",
    "SMOKE_TEST_MKV",
    "RELEASE_MEDIA",
    "RELEASE_NFO",
    "RELEASE_MD5",
    "RELEASE_TORRENT",
    "RELEASE_BBCODE",
    "RELEASE_ENCODER_INFO",
}


def fingerprint(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def output_path(settings, job_id, storage, relative):
    """Limit cleanup to output areas even if an artifact's metadata is malformed."""
    parts = Path(relative).parts
    if storage == "completed":
        if not parts or parts[0] != job_id:
            raise ValueError("Backup is outside this job's completed directory")
    elif storage == "workspace":
        if len(parts) < 3 or parts[0] != "release":
            raise ValueError("Only release working files may expire")
    elif storage == "artifacts":
        if not parts or " [ART] " not in parts[0]:
            raise ValueError("Only ART output files may expire")
    else:
        raise ValueError("Invalid output storage")
    return contained(artifact_root(settings, job_id, storage), relative)


def retire_outputs(db, job, *, kinds, keep_paths=()):
    settings = get_settings()
    replaced = now()
    expires = replaced + timedelta(days=settings.output_backup_days) if settings.output_backup_days else None
    keep = set(keep_paths)
    for row in db.scalars(
        select(Artifact).where(Artifact.job_id == job.id, Artifact.artifact_type.in_(kinds))
    ):
        if (row.storage, row.path) in keep or row.info.get("backup"):
            continue
        candidates = [{"storage": row.storage, "path": row.path}]
        if row.info.get("staging"):
            candidates.append(row.info["staging"])
        elif row.artifact_type in {"RELEASE_MEDIA", "RELEASE_NFO", "RELEASE_MD5"}:
            # Older releases kept a second payload link in the working package.
            name = Path(row.path).parent.name
            candidates.append(
                {
                    "storage": "completed",
                    "path": f"{job.id}/releases/{row.task_id}/{name}/{Path(row.path).name}",
                }
            )
        files = []
        for item in candidates:
            path = output_path(settings, job.id, item["storage"], item["path"])
            if path.is_file():
                files.append({**item, "fingerprint": fingerprint(path)})
        row.info = {
            **row.info,
            "backup": {
                "replaced_at": replaced.isoformat(),
                "expires_at": expires.isoformat() if expires else None,
                "files": files,
            },
        }


def cleanup_expired_outputs():
    """Called periodically by the general worker, including while its queue is paused."""
    settings = get_settings()
    with session() as db:
        job_ids = db.scalars(
            select(Artifact.job_id)
            .where(Artifact.info["backup"]["expires_at"].as_string() <= now().isoformat())
            .distinct()
        ).all()
    for job_id in job_ids:
        try:
            with session() as db:
                job = db.scalar(
                    select(MovieJob).where(MovieJob.id == job_id).with_for_update(skip_locked=True)
                )
                if not job or db.scalar(
                    select(Task.id).where(Task.job_id == job_id, Task.status.in_(["QUEUED", "RUNNING"]))
                ):
                    continue
                current = {("completed", job.analysis.get("final_path"))}
                current.update(
                    (item["storage"], item["path"])
                    for item in job.analysis.get("release_result", {}).get("artifacts", [])
                )
                for row in db.scalars(select(Artifact).where(Artifact.job_id == job_id)):
                    backup = row.info.get("backup", {})
                    if row.artifact_type not in OUTPUT_KINDS or not backup.get("expires_at"):
                        continue
                    if datetime.fromisoformat(backup["expires_at"]) > now():
                        continue
                    files = backup.get("files", [])
                    # Validate the entire list before removing anything. Never delete
                    # a current output, a symlink target, or a user-replaced file.
                    paths = []
                    for item in files:
                        if (item["storage"], item["path"]) in current:
                            raise ValueError("Backup refers to a current output")
                        root = artifact_root(settings, job_id, item["storage"])
                        path = output_path(settings, job_id, item["storage"], item["path"])
                        if path != root.absolute() / item["path"]:
                            raise ValueError("Backup path has changed through a symlink")
                        if path.exists() and fingerprint(path) != item["fingerprint"]:
                            raise ValueError("Backup file was modified; retaining it")
                        paths.append((path, root))
                    for path, root in paths:
                        path.unlink(missing_ok=True)
                        parent = path.parent
                        while parent != root and parent.name != job_id:
                            try:
                                parent.rmdir()
                            except OSError:
                                break
                            parent = parent.parent
                    db.add(
                        Event(
                            job_id=job_id,
                            type="output_backup_expired",
                            data={"path": row.path, "storage": row.storage},
                        )
                    )
                    db.delete(row)
                db.commit()
        except (ValueError, OSError):
            logging.getLogger(__name__).exception("Backup cleanup deferred for job %s", job_id)
