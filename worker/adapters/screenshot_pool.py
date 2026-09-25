"""Immutable-source screenshot sampling, reused across encode variants."""

import json

from backend.app.track_choices import source_key
from shared.paths import contained, write_json
from worker.adapters.source_tracks import link_file, source_lock


def source_candidate(candidate):
    """Only source evidence belongs in the cache, never another encode's choices or images."""
    keys = {
        "candidate_id",
        "source_frame_number",
        "source_total_frames",
        "source_pts_seconds",
        "timeline_seconds",
        "scene_id",
        "metrics",
        "quality",
        "path",
        "picture_type",
        "picture_type_display",
    }
    return {k: v for k, v in candidate.items() if k in keys}


def pool_root(settings, job):
    return settings.cache_root / "screenshots" / source_key(job)


def read_pool(settings, job):
    path = pool_root(settings, job) / "pool.json"
    return json.loads(path.read_text()) if path.exists() else {"revision": 0, "candidates": [], "batches": {}}


def shared_sample(ctx, scan, *, existing=(), sampling_round=0, reserved=(), target=None, sync_only=False):
    from worker.adapters.frame_index import source_frame_index
    from worker.pipeline.screenshots import contact_sheets, verify_b_frame_candidates

    ctx.source()  # Validate the immutable source before trusting its cache.
    root = pool_root(ctx.settings, ctx.job)
    request = ctx.job.analysis.get("screenshot_append", {})
    batch = request.get("batch_id") or ("initial" if not existing else f"round-{sampling_round}-{target}")
    with source_lock(ctx, root, phase="Waiting for shared screenshot sampling"):
        pool = read_pool(ctx.settings, ctx.job)
        if not sync_only and batch not in pool["batches"]:
            # Adopt existing job frames too: adding a batch must not rediscover them.
            known = {c["source_frame_number"] for c in pool["candidates"]}
            for c in existing:
                if c["source_frame_number"] not in known:
                    item = dict(
                        source_candidate(c),
                        candidate_id=max((row["candidate_id"] for row in pool["candidates"]), default=0) + 1,
                    )
                    dest = root / f"frame-{item['candidate_id']}.png"
                    link_file(contained(ctx.workspace, c["path"], exists=True), dest)
                    item["path"] = dest.name
                    pool["candidates"].append(item)
                    known.add(c["source_frame_number"])
            previous = []
            for c in pool["candidates"]:
                dest = ctx.output("screenshots/shared-source", f"frame-{c['candidate_id']}.png")
                link_file(contained(root, c["path"], exists=True), dest)
                previous.append({**c, "path": str(dest.relative_to(ctx.workspace))})
            index, _, _ = scan(
                ctx,
                existing=previous,
                sampling_round=pool["revision"],
                target=target,
                source_only=True,
            )
            old_ids = {c["candidate_id"] for c in previous}
            added = []
            for c in index["candidates"]:
                if c["candidate_id"] in old_ids:
                    continue
                dest = root / f"frame-{c['candidate_id']}.png"
                link_file(contained(ctx.workspace, c["path"], exists=True), dest)
                item = {**source_candidate(c), "path": dest.name}
                pool["candidates"].append(item)
                added.append(c["candidate_id"])
            pool["revision"] += 1
            pool["batches"][batch] = added
            pool["decoder"] = index["decoder"]
            write_json(root / "pool.json", pool)

    # Each output has its own GOP and crop. Never copy another encode's B-frame claim.
    candidates = list(existing)
    seen = set(ctx.job.analysis.get("screenshot_shared_seen", [])) if existing else set()
    seen.update(c.get("shared_source_id") for c in existing)
    known_frames = {c["source_frame_number"] for c in existing}
    next_id = max((c["candidate_id"] for c in existing), default=0)
    if not existing and getattr(ctx.job, "id", None):
        from sqlalchemy import func, select

        from shared.db import session
        from shared.models import Screenshot

        # A rescan retains confirmed finals, whose IDs must never be reused.
        with session() as db:
            next_id = (
                db.scalar(select(func.max(Screenshot.candidate_id)).where(Screenshot.job_id == ctx.job.id))
                or 0
            )
    missing = [c for c in pool["candidates"] if c["candidate_id"] not in seen]
    for i, c in enumerate(missing):
        ctx.check()
        seen.add(c["candidate_id"])
        if c["source_frame_number"] in known_frames:
            continue
        next_id += 1
        item = {**c, "candidate_id": next_id, "shared_source_id": c["candidate_id"]}
        matches = verify_b_frame_candidates(ctx, [item], report=False, exact=True)
        for candidate in matches:
            ctx.artifact(contained(ctx.workspace, candidate["path"]), "SCREENSHOT_CANDIDATE", info=candidate)
            candidates.append(candidate)
        ctx.progress(
            None,
            phase="Checking shared frames for this encode",
            checked_candidates=i + 1,
            total_candidates=len(missing),
        )
    ctx.log(
        f"Reused source pool revision {pool['revision']}; added {len(candidates) - len(existing)} eligible frames for this encode."
    )
    sheets_root = ctx.output("screenshots/contact-sheets", "placeholder").parent
    sheets = contact_sheets(candidates, ctx.workspace, sheets_root)
    for sheet in sheets:
        ctx.artifact(sheets_root / sheet, "CONTACT_SHEET")
    index = {
        "candidates": candidates,
        "decoder": {
            **pool.get("decoder", {}),
            "shared_revision": pool["revision"],
            "shared_seen": sorted(x for x in seen if x is not None),
        },
        "contact_sheets": [str((sheets_root / s).relative_to(ctx.workspace)) for s in sheets],
    }
    path = ctx.output("screenshots", "candidates.json")
    write_json(path, index)
    _, frame_index_path = source_frame_index(ctx)
    return index, ctx.artifact(path, "CANDIDATE_INDEX"), frame_index_path
