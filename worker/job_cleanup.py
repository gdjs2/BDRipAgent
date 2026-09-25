"""Durable deletion of generated files, with source and live-job ownership guards."""

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path

from sqlalchemy import delete, select

from backend.app import queue
from backend.app.track_choices import source_key, source_peers
from shared.config import get_settings
from shared.db import session
from shared.models import Artifact, Event, MovieJob, SourceTrackChoices, Task, now
from shared.paths import contained

logger = logging.getLogger(__name__)


def cache_keys(settings, job):
    keys = set(job.analysis.get("file_cleanup", {}).get("source_cache_keys", []))
    pointer = settings.workspace_root / job.id / "metadata/source-cache-key.json"
    if pointer.is_file() and not pointer.is_symlink():
        keys.add(json.loads(pointer.read_text())["key"])
    try:
        source = contained(settings.source_root, job.source_path, exists=True)
        stat = source.stat()
        if stat.st_size == job.source_size and str(stat.st_mtime_ns) == job.source_mtime_ns:
            identity = [str(source), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
            keys.add(hashlib.sha256(json.dumps(identity).encode()).hexdigest())
    except (ValueError, OSError):
        pass  # The source may have been moved; the saved identity still owns its cache.
    return {key for key in keys if isinstance(key, str) and re.fullmatch(r"[a-f0-9]{64}", key)}


def remove_owned(root, relative, sources, protected=(), *, tree=True):
    """Never follow directory symlinks or delete an input/live artifact."""
    root = root.resolve()
    part = Path(relative)
    if part.is_absolute() or not part.parts or ".." in part.parts:
        raise ValueError("Invalid cleanup path")
    path = root / part
    if path == root or path.parent.resolve() != path.parent:
        raise ValueError("Cleanup path crosses a symlink or storage root")
    if any(path == source or path in source.parents or source in path.parents for source in sources):
        raise ValueError("Cleanup path overlaps source storage")
    if any(path == item or path in item.parents for item in protected):
        return
    if path.is_symlink():
        path.unlink()  # Remove the generated link only, never its target.
    elif path.is_dir():
        if not tree:
            raise ValueError("A registered artifact must be a file")
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def purge_job(db, job, settings):
    if db.scalar(select(Task.id).where(Task.job_id == job.id, Task.status == "RUNNING")):
        return
    live = db.scalars(select(MovieJob).where(MovieJob.deleted_at.is_(None))).all()
    sources = {settings.source_root.resolve()}
    for other in db.scalars(select(MovieJob)):
        try:
            sources.add(contained(settings.source_root, other.source_path))
        except ValueError:
            pass
    protected = []
    for item in db.scalars(select(Artifact).join(MovieJob).where(MovieJob.deleted_at.is_(None))):
        if item.storage == "artifacts":
            protected.append(contained(settings.artifacts_root, item.path))
    keys = cache_keys(settings, job)
    # Save the identity before deleting its workspace; an interrupted purge is retryable.
    job.analysis = {**job.analysis, "file_cleanup": {"status": "pending", "source_cache_keys": sorted(keys)}}
    db.flush()
    bundles = set()
    legacy = []
    for item in db.scalars(
        select(Artifact).where(Artifact.job_id == job.id, Artifact.storage == "artifacts")
    ):
        part = Path(item.path)
        if part.is_absolute() or ".." in part.parts or not part.parts:
            raise ValueError("Invalid registered artifact path")
        if " [ART] " in part.parts[0]:
            bundles.add(part.parts[0])
        else:
            legacy.append(item.path)
    owner = settings.artifacts_root / ".owners" / f"{job.id}.json"
    if owner.is_file() and not owner.is_symlink():
        record = json.loads(owner.read_text())
        if record.get("job_id") != job.id:
            raise ValueError("ART owner does not match the deleted job")
        for bundle in [record["bundle"], *record.get("bundles", [])]:
            if Path(bundle).name != bundle or " [ART] " not in bundle:
                raise ValueError("Invalid owned ART bundle")
            bundles.add(bundle)
    for bundle in bundles:
        remove_owned(settings.artifacts_root, bundle, sources, protected)
    for relative in legacy:
        remove_owned(settings.artifacts_root, relative, sources, protected, tree=False)
    for suffix in ("json", "lock"):
        remove_owned(settings.artifacts_root, f".owners/{job.id}.{suffix}", sources, tree=False)
    # Same-source uploads and extraction caches belong to all surviving encodes.
    if not db.scalar(select(MovieJob.id).where(*source_peers(job))):
        key = source_key(job)
        remove_owned(settings.workspace_root, f"uploaded-subtitles/{key}", sources)
        remove_owned(settings.cache_root, f"subtitle-discovery/{key}", sources)
        remove_owned(settings.cache_root, f"screenshots/{key}", sources)
        live_keys = set().union(*(cache_keys(settings, peer) for peer in live))
        # Earlier removed siblings can own cache identities for the same source.
        for peer in db.scalars(
            select(MovieJob).where(
                MovieJob.source_path == job.source_path,
                MovieJob.source_size == job.source_size,
                MovieJob.source_mtime_ns == job.source_mtime_ns,
            )
        ):
            keys.update(cache_keys(settings, peer))
        for cache_key in keys - live_keys:
            for directory in ("source-tracks", "track-analysis"):
                remove_owned(settings.cache_root, f"{directory}/{cache_key}", sources)
        choices = db.get(SourceTrackChoices, key)
        if choices:
            db.delete(choices)  # Never restore selections pointing at deleted uploads.
    for root, relative in (
        (settings.workspace_root, job.id),
        (settings.completed_root, job.id),
        (settings.cache_root, f"agent/{job.id}"),
    ):
        remove_owned(root, relative, sources)
    db.execute(delete(Artifact).where(Artifact.job_id == job.id))
    job.analysis = {
        **job.analysis,
        "file_cleanup": {
            "status": "complete",
            "completed_at": now().isoformat(),
            "source_cache_keys": sorted(keys),
        },
    }
    db.add(Event(job_id=job.id, type="job_files_removed", data={"source_preserved": True}))


def cleanup_deleted_jobs():
    """Run outside task slots so removed jobs are cleaned even when the queue is paused."""
    settings = get_settings()
    with session() as db:
        job_ids = [
            job.id
            for job in db.scalars(select(MovieJob).where(MovieJob.deleted_at.is_not(None)))
            if job.analysis.get("file_cleanup", {}).get("status") != "complete"
        ]
    for job_id in job_ids:
        try:
            with session() as db:
                # Creation, upload and deletion share this lock: no new source peer
                # can appear between the last-owner check and unlinking shared files.
                queue.settings(db, lock=True)
                job = db.scalar(select(MovieJob).where(MovieJob.id == job_id).with_for_update())
                if (
                    job
                    and job.deleted_at
                    and job.analysis.get("file_cleanup", {}).get("status") != "complete"
                ):
                    purge_job(db, job, settings)
                db.commit()
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.exception("Generated-file cleanup deferred for job %s", job_id)
            with session() as db:
                job = db.scalar(select(MovieJob).where(MovieJob.id == job_id).with_for_update())
                job.analysis = {
                    **job.analysis,
                    "file_cleanup": {
                        **job.analysis.get("file_cleanup", {}),
                        "status": "pending",
                        "error": str(error),
                    },
                }
                db.commit()
