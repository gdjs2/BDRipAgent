"""Enforce time separation across encode variants of the same immutable source."""

import hashlib

from sqlalchemy import select, text

from shared.config import profiles
from shared.encoding import is_smoke_test
from shared.models import EncodeConfig, MovieJob, Screenshot


def is_b_frame_pair(candidate):
    return (
        candidate.get("b_frames_verified") is True
        and candidate.get("picture_type") == "B"
        and candidate.get("encoded_picture_type") == "B"
    )


def lock_source(db, job):
    if db.get_bind().dialect.name == "postgresql":
        identity = f"{job.source_path}\0{job.source_size}\0{job.source_mtime_ns}"
        key = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def codec_for(db, job):
    config = db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job.id))
    return config.data["codec"] if config else profiles()[job.analysis_profile]["codec"]


def other_variant_frames(db, job):
    # Test selections must not reserve frames for a real release (or vice versa).
    if is_smoke_test(job):
        return []
    codec = codec_for(db, job)
    rows = db.execute(
        select(MovieJob, Screenshot)
        .join(Screenshot, Screenshot.job_id == MovieJob.id)
        .where(
            MovieJob.id != job.id,
            MovieJob.source_path == job.source_path,
            MovieJob.source_size == job.source_size,
            MovieJob.source_mtime_ns == job.source_mtime_ns,
            Screenshot.selected.is_(True),
        )
    ).all()
    return [
        {
            **shot.info,
            "job_id": peer.id,
            "spacing": max(
                job.screenshot_policy["min_spacing_seconds"], peer.screenshot_policy["min_spacing_seconds"]
            ),
        }
        for peer, shot in rows
        if not is_smoke_test(peer) and codec_for(db, peer) != codec
    ]


def conflicts(candidate, reserved):
    return any(
        candidate["source_frame_number"] == r["source_frame_number"]
        or abs(candidate["timeline_seconds"] - r["timeline_seconds"]) < r["spacing"]
        for r in reserved
    )


def check_other_variants(db, job, candidates):
    lock_source(db, job)
    reserved = other_variant_frames(db, job)
    if any(conflicts(c, reserved) for c in candidates):
        raise ValueError(
            "Screenshots overlap or are too close to the other codec's selection; select different frames"
        )


def check_manual_spacing(candidates, policy, duration):
    points = sorted(c["timeline_seconds"] for c in candidates)
    if any(b - a < policy["min_spacing_seconds"] for a, b in zip(points, points[1:])):
        raise ValueError("Replacement is too close to another selected frame")
    from collections import Counter

    if max(Counter(c["scene_id"] for c in candidates).values(), default=0) > policy["max_per_scene"]:
        raise ValueError("Replacement repeats a selected scene")
    if duration <= 0 or len({min(3, int(t / duration * 4)) for t in points}) < policy["min_timeline_bins"]:
        raise ValueError("Replacement loses the required timeline coverage")
