"""Expose completed encoder statistics before release generation, including legacy jobs."""

import json
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select

from shared.config import get_settings
from shared.media_details import encoder_summary, video_bitrate
from shared.models import Artifact, EncodeConfig, Task
from shared.paths import artifact_root, contained


def summary(db, job):
    task = db.scalar(
        select(Task).where(Task.job_id == job.id, Task.type == "encode").order_by(Task.created_at.desc())
    )
    if not task or task.status != "SUCCEEDED":
        raise ValueError("Encoder information is available after encoding finishes")
    if job.analysis.get("encoding_skipped"):
        return {
            "filename": "encoder.txt",
            "text": "SMOKE TEST: video encoding was skipped.",
            "truncated": False,
        }
    rows = list(db.scalars(select(Artifact).where(Artifact.job_id == job.id)))
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
            return {"filename": path.name, "text": text, "truncated": False}
        except (OSError, ValueError):
            continue
    path = contained(
        get_settings().workspace_root / job.id, task.log_path or f"logs/{task.id}.log", exists=True
    )
    config = db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job.id))
    codec = config.data.get("codec") if config else job.analysis_profile.split("-")[0]
    # Final statistics are near the end even when progress logs are large.
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 2 * 1024 * 1024))
        text = encoder_summary(stream.read().decode("utf-8", errors="replace"), codec)
    if not text:
        raise ValueError("No encoder summary was found in the completed encoding log")
    return {"filename": job.release_name + ".encoder.txt", "text": text + "\n", "truncated": False}


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
