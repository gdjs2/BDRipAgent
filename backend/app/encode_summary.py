"""Expose completed encoder statistics before release generation, including legacy jobs."""

import json
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select

from shared.config import get_settings
from shared.media_details import encoder_summary, video_bitrate
from shared.models import Artifact, EncodeConfig, Task
from shared.paths import artifact_root, contained


def encode_sizes(job, task, artifacts):
    """Compare the current video-only encode file with the immutable source file."""
    source = job.source_size if job.source_size and job.source_size > 0 else None
    encoded = None
    if not job.analysis.get("encoding_skipped"):
        path = job.analysis.get("encoded_path")
        current = [
            a
            for a in artifacts
            if a.artifact_type == "ENCODED_VIDEO"
            and a.task_id == task.id
            and not a.info.get("backup")
            and (not path or a.path == path)
        ]
        # Legacy successful jobs may only have the retained path in their manifest.
        if path or current:
            try:
                file = contained(get_settings().workspace_root / job.id, path or current[0].path, exists=True)
                encoded = file.stat().st_size if file.is_file() else None
            except (OSError, ValueError):
                pass
        if encoded is None and current:
            encoded = current[0].size
    return {
        "encoded_bytes": encoded,
        "source_bytes": source,
        "percent_of_source": round(encoded / source * 100, 2) if encoded is not None and source else None,
        "encoding_skipped": bool(job.analysis.get("encoding_skipped")),
    }


def summary(db, job):
    task = db.scalar(
        select(Task).where(Task.job_id == job.id, Task.type == "encode").order_by(Task.created_at.desc())
    )
    if not task or task.status != "SUCCEEDED":
        raise ValueError("Encoder information is available after encoding finishes")
    rows = list(db.scalars(select(Artifact).where(Artifact.job_id == job.id)))
    sizes = encode_sizes(job, task, rows)
    if job.analysis.get("encoding_skipped"):
        return {
            "sizes": sizes,
            "filename": "encoder.txt",
            "text": "SMOKE TEST: video encoding was skipped.",
            "truncated": False,
        }
    preferred = [a for a in rows if a.artifact_type == "ENCODER_INFO" and a.task_id == task.id]
    if job.analysis.get("release_result"):
        preferred += [
            a for a in rows if a.artifact_type == "RELEASE_ENCODER_INFO" and not a.info.get("backup")
        ]
    for item in preferred:
        try:
            path = contained(artifact_root(get_settings(), job.id, item.storage), item.path, exists=True)
            with path.open("rb") as stream:
                text = stream.read(1024 * 1024).decode("utf-8-sig", errors="replace")
            return {"filename": path.name, "text": text, "truncated": False, "sizes": sizes}
        except (OSError, ValueError):
            continue
    config = db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job.id))
    codec = config.data.get("codec") if config else job.analysis_profile.split("-")[0]
    # Final statistics are near the end even when progress logs are large.
    text = ""
    try:
        path = contained(
            get_settings().workspace_root / job.id, task.log_path or f"logs/{task.id}.log", exists=True
        )
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 2 * 1024 * 1024))
            text = encoder_summary(stream.read().decode("utf-8", errors="replace"), codec)
    except (OSError, ValueError):
        pass
    return {
        "filename": job.release_name + ".encoder.txt",
        "text": (text or "No encoder summary was found in the completed encoding log.") + "\n",
        "truncated": False,
        "sizes": sizes,
    }


@lru_cache(maxsize=96)
def read_metadata(path, size, mtime):
    if size > 8 * 1024 * 1024:
        return {}
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def source_bitrate(db, job):
    video = job.analysis.get("video", {})
    if video.get("bit_rate_scope") == "video":
        return video.get("bit_rate")
    metadata = {}
    names = {"ffprobe.json", "mediainfo.json", "mkvmerge.json"}
    for item in db.scalars(
        select(Artifact).where(Artifact.job_id == job.id, Artifact.artifact_type == "SOURCE_METADATA")
    ):
        if Path(item.path).name not in names:
            continue
        try:
            path = contained(artifact_root(get_settings(), job.id, item.storage), item.path, exists=True)
            stat = path.stat()
            metadata[path.name] = read_metadata(str(path), stat.st_size, stat.st_mtime_ns)
        except (OSError, ValueError):
            continue
    return video_bitrate(
        metadata.get("ffprobe.json"), metadata.get("mediainfo.json"), metadata.get("mkvmerge.json")
    )
