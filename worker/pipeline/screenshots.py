import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import delete, select

from agent.schemas import Selection, review_policy, validate_selection
from shared.agent_prompts import default_text, task_prompts
from shared.config import ScreenshotPolicy, behavior, screenshot_selection_limits
from shared.db import session
from shared.encoding import is_smoke_test
from shared.models import AgentRun, MovieJob, Screenshot, Task
from shared.paths import contained, job_dir, write_json
from shared.screenshot_rules import conflicts, is_b_frame_pair, other_variant_frames
from worker.adapters.frame_index import frame_number, source_frame_index
from worker.adapters.handbrake import Crop
from worker.adapters.screenshot_agent import select_screenshots
from worker.adapters.screenshot_decoder import candidate_decoder, window_frames
from worker.progress import plan
from worker.runtime import Interrupted

PICTURE_TYPES = {1: "I", 2: "P", 3: "B", 4: "S", 5: "SI", 6: "SP", 7: "BI"}
PICTURE_DISPLAY = {"I": "I", "P": "P (Predicted)", "B": "B (Bi-dir predicted)"}


def rgb_image(frame, video=None):
    if not video:
        return frame.to_image()
    spaces = {
        "bt709": "ITU709",
        "smpte170m": "ITU601",
        "bt470bg": "ITU601",
        "smpte240m": "SMPTE240M",
        "fcc": "FCC",
    }
    matrix = spaces.get(video.get("color_space"), "ITU709" if video["height"] >= 720 else "ITU601")
    color_range = "JPEG" if video.get("color_range") == "pc" else "MPEG"
    return frame.to_image(
        src_colorspace=matrix, dst_colorspace=matrix, src_color_range=color_range, dst_color_range="JPEG"
    )


def cropped_image(frame, crop, video=None):
    image = rgb_image(frame, video)
    return image.crop((crop.left, crop.top, image.width - crop.right, image.height - crop.bottom))


def technical_metrics(image):
    gray = np.asarray(image.convert("L").resize((320, 180)), dtype=np.float32)
    laplacian = -4 * gray[1:-1, 1:-1] + gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:]
    small = np.asarray(image.convert("L").resize((9, 8)))
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    hash_value = sum(int(v) << i for i, v in enumerate(bits))
    histogram, _ = np.histogram(gray, bins=32, range=(0, 256), density=True)
    return {
        "brightness": float(gray.mean()),
        "contrast": float(gray.std()),
        "blur_score": float(laplacian.var()),
        "hash": f"{hash_value:016x}",
    }, histogram * 8


def acceptable(metrics, config):
    return (
        config["black_threshold"] < metrics["brightness"] < config["white_threshold"]
        and metrics["contrast"] >= config["minimum_contrast"]
        and metrics["blur_score"] >= config["minimum_blur_score"]
    )


def contact_sheets(candidates, image_root, output_root):
    sheets = []
    font = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
    for index in range(0, len(candidates), 16):
        sheet = Image.new("RGB", (1280, 800), "#16191d")
        draw = ImageDraw.Draw(sheet)
        name = f"sheet-{index // 16:03d}.jpg"
        for slot, candidate in enumerate(candidates[index : index + 16]):
            x, y = slot % 4 * 320, slot // 4 * 200
            with Image.open(contained(image_root, candidate["path"], exists=True)) as original:
                thumb = original.copy()
            thumb.thumbnail((320, 174))
            sheet.paste(thumb, (x + (320 - thumb.width) // 2, y))
            draw.text(
                (x + 7, y + 177),
                f"#{candidate['candidate_id']:03d}   {candidate['timeline_seconds']:.1f}s",
                font=font,
                fill="white",
            )
            candidate["sheet"] = name
        sheet.save(output_root / name, quality=92)
        sheets.append(name)
    return sheets


def sampling_windows(duration, target, attempts, window_seconds, *, pass_offset=0):
    """One position per timeline bucket per pass, then fill unsuccessful buckets."""
    bucket_seconds = duration / target
    for attempt in range(attempts):
        # Centre first, then alternate positions inside each bucket. Every pass
        # covers the entire movie, including when quality checks reject a region.
        fraction = (0.5 + (attempt + pass_offset) * 0.38196601125) % 1
        for bucket in range(target):
            anchor = min(duration - 0.05, (bucket + fraction) * bucket_seconds)
            yield (
                bucket,
                max(0, anchor - window_seconds / 2),
                anchor,
                min(duration, anchor + window_seconds / 2),
            )


def window_candidate(ctx, frames, points, crop, config, *, bucket, anchor):
    previous_hist, previous_metrics = None, None
    scene_start, scene_id, next_sample = None, 0, float("-inf")
    first_pts = ctx.job.validation["metrics"]["source_first_pts"]
    winner = None
    for frame in frames:
        pts = float(frame.pts * frame.time_base)
        t = pts - first_pts
        if t < next_sample:
            continue
        next_sample = t + config["candidate_sample_seconds"]
        image = cropped_image(frame, crop, ctx.job.analysis["video"])
        metrics, histogram = technical_metrics(image)
        if scene_start is None:
            scene_start = t
        scene_change = previous_hist is not None and (
            np.abs(histogram - previous_hist).sum() / 2 > config["scene_histogram_threshold"]
        )
        if scene_change:
            scene_start = t
            scene_id += 1
            if winner and t - winner["timeline_seconds"] < 0.75:
                winner = None
        unstable = (
            previous_metrics is not None and abs(metrics["brightness"] - previous_metrics["brightness"]) > 18
        )
        previous_hist, previous_metrics = histogram, metrics
        # Inspect only local neighbours: distant seek windows aren't scene cuts.
        # Stop accepting before the window end so the last sample can reject a cut.
        if (
            t < anchor
            or t > anchor + 0.5
            or t - scene_start < 1.0
            or unstable
            or not acceptable(metrics, config)
        ):
            continue
        quality = metrics["blur_score"] + metrics["contrast"] * 2
        if winner and winner["quality"] >= quality:
            continue
        winner = {
            "candidate_id": bucket + 1,
            "source_frame_number": frame_number(points, pts),
            "source_total_frames": len(points),
            "source_pts_seconds": pts,
            "timeline_seconds": t,
            # Scene IDs describe local detection windows, not an exhaustive
            # movie scene map. Spacing and visual deduplication apply globally.
            "scene_id": f"window-{bucket}-scene-{scene_id}",
            "metrics": metrics,
            "quality": quality,
        }
    return winner


def sample_candidates(ctx, *, existing=(), sampling_round=0, reserved=(), target=None):
    if not hasattr(ctx, "settings"):
        return scan_candidates(
            ctx, existing=existing, sampling_round=sampling_round, reserved=reserved, target=target
        )
    from worker.adapters.screenshot_pool import shared_sample

    return shared_sample(
        ctx,
        scan_candidates,
        existing=existing,
        sampling_round=sampling_round,
        reserved=reserved,
        target=target,
    )


def scan_candidates(ctx, *, existing=(), sampling_round=0, reserved=(), target=None, source_only=False):
    ctx.progress(None, phase="Preparing screenshot frame index")
    config, crop = behavior(), Crop(**ctx.job.analysis["crop"])
    points, frame_index_path = source_frame_index(ctx)
    first_pts = ctx.job.validation["metrics"]["source_first_pts"]
    duration = float(points[-1] - first_pts + points[-1] - points[-2])
    target = max(1, int(config["candidate_count"] if target is None else target))
    attempts = max(1, int(config.get("candidate_attempts_per_bucket", 4)))
    window_seconds = max(3.0, float(config.get("candidate_window_seconds", 3)))
    candidates, filled = list(existing), set()
    initial_count = len(candidates)
    id_offset = max(
        (c["candidate_id"] for c in candidates), default=sampling_round * int(config["candidate_count"])
    )
    stats = {
        "sampling_round": sampling_round,
        "decoded_frames": 0,
        "sampled_windows": 0,
        "target_candidates": target,
        "existing_candidates": initial_count,
        "source_total_frames": len(points),
    }
    started, progress = time.monotonic(), 5.0
    ctx.log(
        f"Seeking across {target} timeline buckets; at most {attempts} short windows per bucket. "
        "Stop after enough distinct, verified B-frame pairs; no sequential movie decode."
    )
    with candidate_decoder(ctx) as (container, _, decoder_info):
        for bucket, start, anchor, end in sampling_windows(
            duration, target, attempts, window_seconds, pass_offset=sampling_round * attempts
        ):
            ctx.check()
            if bucket in filled:
                continue
            stats["sampled_windows"] += 1
            frames = window_frames(ctx, container, first_pts + start, first_pts + end, stats)
            try:
                candidate = window_candidate(
                    ctx, frames, points, crop, config, bucket=bucket + id_offset, anchor=anchor
                )
            finally:
                frames.close()
            eligible = (
                verify_b_frame_candidates(ctx, [candidate], report=False, source_only=source_only)
                if candidate
                else []
            )
            for candidate in eligible:
                value = int(candidate["metrics"]["hash"], 16)
                duplicate = any(
                    candidate["source_frame_number"] == other["source_frame_number"]
                    or (value ^ int(other["metrics"]["hash"], 16)).bit_count()
                    < config["duplicate_hash_distance"]
                    for other in candidates
                )
                if duplicate or conflicts(candidate, reserved):
                    contained(ctx.workspace, candidate["path"]).unlink(missing_ok=True)
                    continue
                filled.add(bucket)
                candidates.append(candidate)
                ctx.artifact(
                    contained(ctx.workspace, candidate["path"]), "SCREENSHOT_CANDIDATE", info=candidate
                )
            progress = max(
                progress,
                5 + 90 * (len(candidates) - initial_count) / target,
                5 + 90 * stats["sampled_windows"] / (target * attempts),
            )
            ctx.progress(
                progress,
                phase="Sampling short video windows",
                candidate_count=len(candidates) - initial_count,
                total_candidates=len(candidates),
                **stats,
                **decoder_info,
            )
            if len(candidates) - initial_count >= target:
                break
    candidates.sort(key=lambda c: c["timeline_seconds"])
    decoder_info = {
        **decoder_info,
        **stats,
        "sampling": "seek_windows",
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    ctx.log(
        f"Screenshot sampling added {len(candidates) - initial_count} candidates from {stats['sampled_windows']} windows ({len(candidates)} total); "
        f"decoded {stats['decoded_frames']} scan frames instead of all {len(points)} source frames "
        f"in {decoder_info['elapsed_seconds']:.1f}s (plus short CPU B-frame verification seeks)."
    )
    ctx.progress(
        95,
        phase="Preparing candidate contact sheets",
        indeterminate=True,
        candidate_count=len(candidates),
        **decoder_info,
    )
    if len(candidates) < ctx.job.screenshot_policy["count"]:
        raise ValueError(
            f"Only {len(candidates)} eligible B-frame pairs remain; need {ctx.job.screenshot_policy['count']}. "
            "Both source and encoded video must contain B-frames. Increase candidate_attempts_per_bucket and rescan "
            "if more candidates are needed."
        )
    sheets_root = ctx.output("screenshots/contact-sheets", "placeholder").parent
    sheets = contact_sheets(candidates, ctx.workspace, sheets_root)
    for sheet in sheets:
        ctx.artifact(sheets_root / sheet, "CONTACT_SHEET")
    path = ctx.output("screenshots", "candidates.json")
    index = {
        "decoder": decoder_info,
        "candidates": candidates,
        "contact_sheets": [str((sheets_root / s).relative_to(ctx.workspace)) for s in sheets],
    }
    write_json(path, index)
    relative = ctx.artifact(path, "CANDIDATE_INDEX")
    return index, relative, frame_index_path


def generate(ctx):
    sampling_round = (
        ctx.job.analysis.get("screenshot_scan_decoder", {}).get(
            "sampling_round", 0 if ctx.job.analysis.get("candidate_index") else -1
        )
        + 1
    )
    with session() as db:
        reserved = other_variant_frames(db, ctx.job) if getattr(ctx.job, "id", None) else []
    append = ctx.job.analysis.get("screenshot_append")
    existing = []
    if append:
        existing = json.loads(
            contained(ctx.workspace, ctx.job.analysis["candidate_index"], exists=True).read_text()
        )["candidates"]
    options = {"existing": existing, "target": append["count"]} if append else {}
    index, relative, frame_index_path = sample_candidates(
        ctx, sampling_round=sampling_round, reserved=reserved, **options
    )
    candidates, decoder_info = index["candidates"], index["decoder"]

    def save(db, job):
        if append:
            rows = {
                r.candidate_id: r for r in db.scalars(select(Screenshot).where(Screenshot.job_id == job.id))
            }
            new_ids = []
            for c in candidates:
                if c["candidate_id"] not in rows:
                    db.add(Screenshot(job_id=job.id, candidate_id=c["candidate_id"], info=c))
                    new_ids.append(c["candidate_id"])
            job.analysis = {**job.analysis, "screenshot_append": {**append, "candidate_ids": new_ids}}
        else:
            # Confirmed finals still reserve their frames until the user replaces them.
            db.execute(delete(Screenshot).where(Screenshot.job_id == job.id, Screenshot.selected.is_(False)))
            for row in db.scalars(select(Screenshot).where(Screenshot.job_id == job.id)):
                row.shortlisted = False
                row.info = {**row.info, "recommendation_rank": None}
            for c in candidates:
                db.add(Screenshot(job_id=job.id, candidate_id=c["candidate_id"], info=c))
        job.analysis = {
            **job.analysis,
            "candidate_index": relative,
            "screenshot_scan_decoder": decoder_info,
            "screenshot_shared_revision": decoder_info.get("shared_revision", 0),
            "screenshot_shared_seen": decoder_info.get("shared_seen", []),
            "source_frame_index": frame_index_path,
        }

    return save


def select_frames(ctx):
    if ctx.job.analysis.get("screenshot_append"):
        return append_shortlist(ctx)
    steps = plan(ctx, preparation=25, agent=35, comparisons=35, thumbnails=5)
    steps["preparation"].progress(None, phase="Preparing screenshot review")
    index = json.loads(contained(ctx.workspace, ctx.job.analysis["candidate_index"], exists=True).read_text())
    root = job_dir(job_dir(ctx.settings.cache_root / "agent", ctx.job.id), ctx.task_id)
    root.mkdir(parents=True, exist_ok=True)
    candidates = index["candidates"]
    strategy = ctx.job.screenshot_policy.get("strategy", "local")
    reused_ids = ctx.job.analysis.get("review_shortlisted_ids", []) if strategy == "agent" else []
    if strategy == "agent":
        selected_prompt = task_prompts(ctx)["screenshot_selection"]
        prior_hash = ctx.job.analysis.get("screenshot_review", {}).get("prompt_sha256")
        if prior_hash != selected_prompt["sha256"] and (
            prior_hash is not None or selected_prompt["text"] != default_text("screenshot_selection")
        ):
            reused_ids = []
    if len(reused_ids) < ctx.job.screenshot_policy.get("best_count", 30) or len(reused_ids) > 40:
        reused_ids = []
    shortlisted_choices = []
    if reused_ids:
        with session() as db:
            candidates = [
                dict(row.info)
                for row in db.scalars(
                    select(Screenshot).where(
                        Screenshot.job_id == ctx.job.id, Screenshot.candidate_id.in_(reused_ids)
                    )
                )
            ]
            previous = db.scalar(
                select(AgentRun).where(AgentRun.job_id == ctx.job.id).order_by(AgentRun.created_at.desc())
            )
            if previous:
                choices = {
                    c["candidate_id"]: c
                    for run in previous.result.get("runs", [])
                    if run.get("stage") == "shortlist"
                    for c in run["output"]["selected"]
                }
                choices.update({c["candidate_id"]: c for c in previous.result.get("shortlisted_choices", [])})
                choices.update({c["candidate_id"]: c for c in previous.result.get("selected", [])})
                shortlisted_choices = [choices[i] for i in reused_ids if i in choices]
        if len(candidates) != len(reused_ids) or len(candidates) > 40:
            raise ValueError("The saved screenshot shortlist is incomplete")
        ctx.log(f"Reusing {len(candidates)} saved shortlist frames for review.")
    # Also enforce the rule for candidate indexes created before B-frame checks
    # existed. Only source images are sent to the visual agent.
    if any(not is_b_frame_pair(c) for c in candidates):
        candidates = verify_b_frame_candidates(steps["preparation"], candidates, progress_span=95)
        for candidate in candidates:
            ctx.artifact(contained(ctx.workspace, candidate["path"]), "SCREENSHOT_CANDIDATE", info=candidate)
        if len(candidates) >= ctx.job.screenshot_policy["count"]:
            # Commit successful frame preparation before the long agent request.
            # A later retry can reuse this index even if the agent restarts.
            path = ctx.output("screenshots", "verified-candidates.json")
            write_json(path, {**index, "candidates": candidates, "contact_sheets": []})
            relative = ctx.artifact(path, "CANDIDATE_INDEX")
            with session() as db:
                job = db.scalar(select(MovieJob).where(MovieJob.id == ctx.job.id).with_for_update())
                task = db.scalar(select(Task).where(Task.id == ctx.task_id).with_for_update())
                if task.status != "RUNNING" or task.run_token != ctx.token or task.cancel_requested:
                    raise Interrupted("Task lost its lease before saving verified candidates")
                job.analysis = {**job.analysis, "candidate_index": relative}
                db.commit()
            ctx.log("Saved verified B-frame candidates for screenshot selection retries.")
    with session() as db:
        reserved = other_variant_frames(db, ctx.job)
    # Check spacing after refinement, which can shift a candidate by a few frames.
    candidates = [c for c in candidates if not conflicts(c, reserved)]
    available_ids = {c["candidate_id"] for c in candidates}
    shortlisted_choices = [c for c in shortlisted_choices if c["candidate_id"] in available_ids]
    if len(candidates) < ctx.job.screenshot_policy["count"]:
        raise ValueError(
            "Too few verified B-frame pairs remain after excluding nearby frames from the other codec"
        )
    if strategy == "local":
        steps["preparation"].done("Locally filtered candidates ready")
        result = local_selection(candidates, ctx.job.screenshot_policy)
        steps["agent"].done("Local filtering complete — choose Best manually")
    else:
        candidates, result = agent_review(
            ctx, steps, candidates, index, root, reused_ids, shortlisted_choices
        )
    ctx.check()
    shortlist = result["shortlisted_ids"]
    path = ctx.output(
        "screenshots/selected", "local-result.json" if strategy == "local" else "agent-result.json"
    )
    write_json(path, result)
    ctx.artifact(path, "SCREENSHOT_LOCAL_DECISION" if strategy == "local" else "AGENT_DECISION")
    recommendations = {c["candidate_id"]: c for c in result["selected"]}
    review_rows = [
        SimpleNamespace(id=c["candidate_id"], candidate_id=c["candidate_id"], info=c)
        for c in candidates
        if c["candidate_id"] in recommendations
    ]
    comparisons = render_pairs(steps["comparisons"], review_rows, review=True) if review_rows else {}
    steps["comparisons"].done("Review comparisons ready")
    steps["thumbnails"].progress(None, phase="Preparing shortlist thumbnails")
    for candidate in candidates:
        if candidate["candidate_id"] not in shortlist:
            continue
        thumbnail = ctx.output("screenshots/thumbnails", f"candidate-{candidate['candidate_id']:03d}.jpg")
        with Image.open(contained(ctx.workspace, candidate["path"], exists=True)) as source_image:
            source_image.thumbnail((640, 360))
            source_image.convert("RGB").save(thumbnail, quality=85)
        candidate["thumbnail"] = ctx.artifact(thumbnail, "SCREENSHOT_THUMBNAIL")
    steps["thumbnails"].done("Screenshot review ready")

    def save(db, job):
        choices = {c["candidate_id"]: c for c in result.get("shortlisted_choices", [])}
        choices.update(recommendations)
        ranks = {c["candidate_id"]: rank for rank, c in enumerate(result["selected"], 1)}
        verified = {c["candidate_id"]: c for c in candidates}
        rows = {
            row.candidate_id: row for row in db.scalars(select(Screenshot).where(Screenshot.job_id == job.id))
        }
        for candidate_id, info in verified.items():
            if candidate_id not in rows:
                row = Screenshot(job_id=job.id, candidate_id=candidate_id, info=info)
                db.add(row)
                rows[candidate_id] = row
        for row in rows.values():
            if row.candidate_id in verified:
                row.info = {**row.info, **verified[row.candidate_id]}
            row.shortlisted = row.candidate_id in result["shortlisted_ids"]
            row.info = {
                **row.info,
                **choices.get(row.candidate_id, {}),
                "recommendation_rank": ranks.get(row.candidate_id),
            }
            if row.candidate_id in comparisons:
                row.info = {**row.info, "review_comparisons": comparisons[row.candidate_id]}
        if strategy == "agent":
            db.add(AgentRun(job_id=job.id, task_id=ctx.task_id, result=result))
            job.codex_thread_id = result.get("thread_id")
        job.analysis = {
            **{k: v for k, v in job.analysis.items() if k != "review_shortlisted_ids"},
            "screenshot_review": {
                "strategy": strategy,
                "prompt_sha256": result.get("prompt_sha256"),
                "requested": ctx.job.screenshot_policy.get("best_count", 30) if strategy == "agent" else 0,
                "available": len(result["selected"]),
                "candidate_count": len(candidates),
                "warning": result.get("warning"),
            },
        }

    return save


def append_shortlist(ctx):
    """Publish only the newly sampled frames; never rank or replace human choices."""
    request = ctx.job.analysis["screenshot_append"]
    ids = set(request.get("candidate_ids", []))
    index = json.loads(contained(ctx.workspace, ctx.job.analysis["candidate_index"], exists=True).read_text())
    additions = [c for c in index["candidates"] if c["candidate_id"] in ids]
    updates = {}
    for i, c in enumerate(additions):
        ctx.check()
        if not is_b_frame_pair(c):
            raise ValueError("Additional screenshots must be verified B-frame pairs")
        thumbnail = ctx.output("screenshots/thumbnails", f"candidate-{c['candidate_id']:03d}.jpg")
        with Image.open(contained(ctx.workspace, c["path"], exists=True)) as image:
            image.thumbnail((640, 360))
            image.convert("RGB").save(thumbnail, quality=85)
        updates[c["candidate_id"]] = {
            **c,
            "thumbnail": ctx.artifact(thumbnail, "SCREENSHOT_THUMBNAIL"),
            "recommendation_rank": None,
        }
        ctx.progress(
            (i + 1) / len(additions) * 100,
            phase="Adding new shortlist frames",
            completed_candidates=i + 1,
            total_candidates=len(additions),
        )

    def save(db, job):
        for row in db.scalars(
            select(Screenshot).where(Screenshot.job_id == job.id, Screenshot.candidate_id.in_(ids))
        ):
            row.info = {**row.info, **updates[row.candidate_id]}
            row.shortlisted = True
        job.analysis = {
            **{
                k: v
                for k, v in job.analysis.items()
                if k not in ("screenshot_append", "review_shortlisted_ids")
            },
            "screenshot_more": {
                "requested": request["count"],
                "added": len(additions),
                "candidate_ids": sorted(ids),
                "warning": f"Found {len(additions)} of {request['count']} additional quality frames. You can request another batch."
                if len(additions) < request["count"]
                else None,
            },
        }

    return save


def local_selection(candidates, policy):
    """Expose the filtered pool; only the user can add local candidates to Best."""
    return {
        "strategy": "local",
        "shortlisted_ids": [c["candidate_id"] for c in candidates],
        "selected": [],
        "warning": None,
    }


def checkpoint_pool(ctx, index, relative):
    """Keep expensive frame work across failed or timed-out agent requests."""
    with session() as db:
        job = db.scalar(select(MovieJob).where(MovieJob.id == ctx.job.id).with_for_update())
        task = db.scalar(select(Task).where(Task.id == ctx.task_id).with_for_update())
        if task.status != "RUNNING" or task.run_token != ctx.token or task.cancel_requested:
            raise Interrupted("Task lost its lease before saving screenshot candidates")
        rows = {r.candidate_id: r for r in db.scalars(select(Screenshot).where(Screenshot.job_id == job.id))}
        for c in index["candidates"]:
            if c["candidate_id"] not in rows:
                db.add(Screenshot(job_id=job.id, candidate_id=c["candidate_id"], info=c))
        job.analysis = {
            **job.analysis,
            "candidate_index": relative,
            "screenshot_scan_decoder": index["decoder"],
            "screenshot_shared_revision": index["decoder"].get("shared_revision", 0),
            "screenshot_shared_seen": index["decoder"].get("shared_seen", []),
        }
        db.commit()
    ctx.job.analysis = {**ctx.job.analysis, "candidate_index": relative}


class ScreenshotDeadline:
    def __init__(self, ctx, deadline):
        self.ctx, self.deadline = ctx, deadline

    def __getattr__(self, name):
        return getattr(self.ctx, name)

    def check(self):
        self.ctx.check()
        if time.monotonic() >= self.deadline:
            raise TimeoutError(
                "Screenshot selection reached its configured time limit; prepared candidates are retained"
            )


def agent_review(ctx, steps, candidates, index, root, reused_ids, shortlisted_choices):
    limits = screenshot_selection_limits()
    deadline = time.monotonic() + limits["max_seconds"]
    ctx = ScreenshotDeadline(ctx, deadline)
    steps = {name: ScreenshotDeadline(step, deadline) for name, step in steps.items()}
    sampling_round = index.get("decoder", {}).get("sampling_round", 0)
    previous = index.get("agent_progress", {})
    selected_prompt = task_prompts(ctx)["screenshot_selection"]
    if previous.get("prompt_sha256") != selected_prompt["sha256"]:
        previous = {}
    for review_round in range(limits["max_sampling_rounds"]):
        ctx.check()
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Screenshot selection reached its configured time limit; prepared candidates are retained"
            )
        for c in candidates:
            target = root / f"candidate-{c['candidate_id']:03d}.png"
            shutil.copyfile(contained(ctx.workspace, c["path"], exists=True), target)
            c["agent_image"] = target.name
        sheets = contact_sheets(candidates, ctx.workspace, root)
        final_round = review_round == limits["max_sampling_rounds"] - 1
        write_json(
            root / "inventory.json",
            {
                "candidates": candidates,
                "contact_sheets": sheets,
                "policy": ctx.job.screenshot_policy,
                "duration": ctx.job.validation["metrics"]["source_duration"],
                "shortlisted_ids": [c["candidate_id"] for c in candidates] if reused_ids else [],
                "agent_prompt": selected_prompt,
                "shortlisted_choices": previous.get("shortlisted_choices", shortlisted_choices),
                "reviewed_candidate_ids": previous.get("reviewed_candidate_ids", []),
                "allow_partial": final_round,
                "remaining_seconds": max(1, deadline - time.monotonic()),
            },
        )
        steps["agent"].progress(
            None,
            phase="Agent screenshot review",
            sampling_round=review_round + 1,
            target_best_count=ctx.job.screenshot_policy.get("best_count", 30),
            candidate_count=len(candidates),
        )
        result = select_screenshots(steps["agent"], max(1, deadline - time.monotonic()))
        result["prompt_sha256"] = selected_prompt["sha256"]
        if result.get("needs_more_candidates") and not final_round:
            previous = result
            sampling_round += 1
            reused_ids = []
            with session() as db:
                reserved = other_variant_frames(db, ctx.job)
            ctx.log(
                "Not enough suitable screenshots; sampling fresh timeline windows and retaining reviewed candidates."
            )
            index, relative, _ = sample_candidates(
                steps["preparation"], existing=candidates, sampling_round=sampling_round, reserved=reserved
            )
            candidates = index["candidates"]
            index["agent_progress"] = previous
            write_json(contained(ctx.workspace, relative), index)
            checkpoint_pool(ctx, index, relative)
            continue
        shortlist = result["shortlisted_ids"]
        if (
            not shortlist
            or len(shortlist) > 40
            or len(set(shortlist)) != len(shortlist)
            or not set(shortlist) <= {c["candidate_id"] for c in candidates}
        ):
            raise ValueError("Invalid agent shortlist")
        errors = validate_selection(
            Selection.model_validate({"selected": result["selected"]}),
            [c for c in candidates if c["candidate_id"] in shortlist],
            review_policy(ScreenshotPolicy(**ctx.job.screenshot_policy), len(shortlist)),
            ctx.job.validation["metrics"]["source_duration"],
        )
        if errors:
            raise ValueError("Invalid agent decision: " + "; ".join(errors))
        if len(result["selected"]) < ctx.job.screenshot_policy.get("best_count", 30):
            result["warning"] = (
                "Sampling limit reached before enough suitable frames were found. Review the available choices or increase the sampling limit."
            )
        steps["preparation"].done("Screenshot review inputs ready")
        steps["agent"].done("Screenshot agent review complete")
        return candidates, result
    raise ValueError("Screenshot sampling limit reached")


def extract_at(path, pts_seconds, tolerance):
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        container.seek(max(0, int((pts_seconds - 1) / stream.time_base)), stream=stream, backward=True)
        best, distance = None, float("inf")
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            pts = float(frame.pts * frame.time_base)
            delta = abs(pts - pts_seconds)
            if delta < distance:
                best, distance = frame, delta
            if pts >= pts_seconds:
                break
        if best is None or distance > tolerance:
            raise ValueError(f"No matching decoded frame within {tolerance:.6f}s at PTS {pts_seconds:.6f}")
        return best


def nearby_source_frames(path, pts_seconds, tolerance=0.003):
    """Yield the anchor and up to half a second after it, with exact frame offsets."""
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        container.seek(max(0, int((pts_seconds - 1) / stream.time_base)), stream=stream, backward=True)
        offset = None
        for frame in container.decode(stream):
            if frame.pts is None:
                raise ValueError("Source frame has no PTS")
            pts = float(frame.pts * frame.time_base)
            if offset is None:
                if abs(pts - pts_seconds) <= tolerance:
                    offset = 0
                elif pts > pts_seconds + tolerance:
                    break
                else:
                    continue
            if pts - pts_seconds > 0.5:
                break
            yield offset, frame
            offset += 1
        if offset is None:
            raise ValueError(f"Cannot locate source candidate at PTS {pts_seconds:.6f}")


def verify_b_frame_candidates(
    ctx, candidates, *, progress_start=0, progress_span=90, report=True, source_only=False, exact=False
):
    """Refine scan candidates to nearby pairs with CPU-verified B-picture types."""
    source = ctx.source()
    smoke = source_only or is_smoke_test(ctx.job)
    encoded = source if smoke else contained(ctx.workspace, ctx.job.analysis["encoded_path"], exists=True)
    metrics = ctx.job.validation["metrics"]
    crop, config = Crop(**ctx.job.analysis["crop"]), behavior()
    eligible = []
    last_report = 0
    if report:
        ctx.log(
            f"Checking {len(candidates)} candidate pairs: both source and encoded frames must be B-frames."
        )
    for index, candidate in enumerate(candidates):
        ctx.check()
        pts = candidate["source_pts_seconds"]
        anchor_histogram = None
        window = nearby_source_frames(source, pts)
        try:
            for offset, source_frame in window:
                if exact and offset:
                    break
                ctx.check()
                # Reject scene changes while looking for a nearby B-frame pair.
                image = cropped_image(source_frame, crop, ctx.job.analysis["video"])
                quality, histogram = technical_metrics(image)
                if anchor_histogram is None:
                    anchor_histogram = histogram
                elif np.abs(histogram - anchor_histogram).sum() / 2 > config["scene_histogram_threshold"]:
                    break
                if int(source_frame.pict_type) != 3 or not acceptable(quality, config):
                    continue
                source_pts = float(source_frame.pts * source_frame.time_base)
                encoded_pts = (
                    source_pts
                    if source_only
                    else source_pts - metrics["source_first_pts"] + metrics["encoded_first_pts"]
                )
                encoded_frame = source_frame if smoke else extract_at(encoded, encoded_pts, 0.003)
                if int(encoded_frame.pict_type) != 3:
                    continue
                path = ctx.output(
                    "screenshots/candidates", f"candidate-{candidate['candidate_id']:03d}-b.png"
                )
                image.save(path)
                eligible.append(
                    {
                        **candidate,
                        "source_frame_number": candidate["source_frame_number"] + offset,
                        "source_pts_seconds": source_pts,
                        "timeline_seconds": source_pts - metrics["source_first_pts"],
                        "path": str(path.relative_to(ctx.workspace)),
                        "metrics": quality,
                        "quality": quality["blur_score"] + quality["contrast"] * 2,
                        "picture_type": "B",
                        "picture_type_display": PICTURE_DISPLAY["B"],
                        "encoded_picture_type": "B",
                        "b_frames_verified": True,
                    }
                )
                break
        finally:
            window.close()
        if report and (time.monotonic() - last_report > 5 or index == len(candidates) - 1):
            ctx.progress(
                progress_start + (index + 1) / len(candidates) * progress_span,
                phase="Checking B-frame pairs",
                checked_candidates=index + 1,
                eligible_pairs=len(eligible),
            )
            last_report = time.monotonic()
    if report:
        ctx.log(f"B-frame check retained {len(eligible)} of {len(candidates)} candidate pairs.")
    return eligible


def overlay(image, frame_number, total_frames, picture_type, label, config=None):
    config = config or behavior()["screenshot_overlay"]
    font = None
    for path in config["font_paths"]:
        if Path(path).is_file():
            font = ImageFont.truetype(path, config["font_size"])
            break
    if font is None:
        raise RuntimeError("Install Liberation Sans Bold or configure an available bold sans-serif font")
    lines = [
        f"Frame Number: {frame_number} of {total_frames}",
        f"Picture Type: {PICTURE_DISPLAY.get(picture_type, picture_type)}",
        label,
    ]
    draw = ImageDraw.Draw(image)
    spacing = config.get("letter_spacing", 0)
    baseline = config["y"] + config.get("baseline_offset", round(config["font_size"] * 0.75))
    for i, line in enumerate(lines):
        x = float(config["x"])
        # Match the reference's baseline and tighter glyph spacing. Only text is
        # rasterized here; the underlying comparison frame is never resampled.
        for char in line:
            draw.text(
                (x, baseline + i * config["line_pitch"]),
                char,
                font=font,
                anchor="ls",
                fill=config["text_color"],
                stroke_width=config["outline_width"],
                stroke_fill=config["outline_color"],
            )
            x += font.getlength(char) + spacing
    return image


def render_pairs(ctx, selected, *, review=False):
    crop = Crop(**ctx.job.analysis["crop"])
    source = ctx.source()
    smoke = is_smoke_test(ctx.job)
    encoded = source if smoke else contained(ctx.workspace, ctx.job.analysis["encoded_path"], exists=True)
    metrics = ctx.job.validation["metrics"]
    tolerance = 0.003
    updates = {}
    ctx.progress(0, phase="Rendering screenshot comparisons", completed_pairs=0, total_pairs=len(selected))
    for index, shot in enumerate(selected):
        ctx.check()
        c = shot.info
        source_frame = extract_at(source, c["source_pts_seconds"], tolerance)
        encoded_pts = c["source_pts_seconds"] - metrics["source_first_pts"] + metrics["encoded_first_pts"]
        encoded_frame = source_frame if smoke else extract_at(encoded, encoded_pts, tolerance)
        if int(source_frame.pict_type) != 3 or int(encoded_frame.pict_type) != 3:
            raise ValueError(
                f"Candidate {shot.candidate_id} is not a B-frame in both source and encoded video; "
                "regenerate candidates and select verified B-frame pairs"
            )
        src_image = cropped_image(source_frame, crop, ctx.job.analysis["video"])
        enc_image = src_image.copy() if smoke else rgb_image(encoded_frame, ctx.job.analysis["video"])
        if src_image.size != enc_image.size:
            raise ValueError("Source/encode screenshot dimensions differ")
        filename = Path(ctx.job.analysis.get("final_path") or ctx.job.release_name + ".mkv").name
        encoded_label = f"SMOKE TEST - Source reused | {filename}" if smoke else filename
        output = {}
        for suffix, frame, image, label in [
            ("src", source_frame, src_image, "Source"),
            (
                "encode",
                encoded_frame,
                enc_image,
                encoded_label,
            ),
        ]:
            picture = PICTURE_TYPES.get(int(frame.pict_type), "unknown")
            if picture == "unknown":
                raise ValueError("Decoder did not provide picture type metadata")
            overlay(image, c["source_frame_number"], c["source_total_frames"], picture, label)
            prefix = "SMOKE-TEST." if smoke else ""
            path = ctx.output(
                "screenshots/review" if review else "screenshots/comparisons",
                f"{prefix}{c['source_frame_number']}.{suffix}.png",
            )
            image.save(path, format="PNG")
            kind = "SCREENSHOT_SOURCE" if suffix == "src" else "SCREENSHOT_ENCODE"
            if review:
                kind = "SCREENSHOT_REVIEW_SOURCE" if suffix == "src" else "SCREENSHOT_REVIEW_ENCODE"
            output[suffix] = ctx.artifact(
                path,
                kind,
                info={
                    "candidate_id": shot.candidate_id,
                    "smoke_test": smoke,
                    "picture_type": picture,
                    "overlay_label": label,
                    "pts_seconds": float(frame.pts * frame.time_base),
                },
            )
        updates[shot.id] = output
        ctx.progress(
            (index + 1) / len(selected) * 100,
            phase="Preparing review comparisons" if review else "Rendering screenshot comparisons",
            completed_pairs=index + 1,
            total_pairs=len(selected),
        )
    return updates


def render(ctx):
    with session() as db:
        selected = db.scalars(
            select(Screenshot).where(Screenshot.job_id == ctx.job.id, Screenshot.selected.is_(True))
        ).all()
    count = ctx.job.analysis.get("screenshot_selection", {}).get("count", ctx.job.screenshot_policy["count"])
    if len(selected) != count:
        raise ValueError("Final selection count does not match the confirmed screenshot selection")
    updates = render_pairs(ctx, selected)

    def save(db, job):
        for shot_id, paths in updates.items():
            row = db.get(Screenshot, shot_id)
            row.info = {**row.info, "comparisons": paths}
        job.analysis = {k: v for k, v in job.analysis.items() if k != "release_result"}

    return save


def sync_shared_pool(ctx):
    from worker.adapters.screenshot_pool import shared_sample

    with session() as db:
        existing = [
            dict(r.info) for r in db.scalars(select(Screenshot).where(Screenshot.job_id == ctx.job.id))
        ]
    index, relative, _ = shared_sample(ctx, scan_candidates, existing=existing, sync_only=True)
    old_ids = {c["candidate_id"] for c in existing}
    added = [c for c in index["candidates"] if c["candidate_id"] not in old_ids]
    for c in added:
        thumbnail = ctx.output("screenshots/thumbnails", f"candidate-{c['candidate_id']:03d}.jpg")
        with Image.open(contained(ctx.workspace, c["path"], exists=True)) as image:
            image.thumbnail((640, 360))
            image.convert("RGB").save(thumbnail, quality=85)
        c["thumbnail"] = ctx.artifact(thumbnail, "SCREENSHOT_THUMBNAIL")
        c["recommendation_rank"] = None

    def save(db, job):
        for c in added:
            db.add(Screenshot(job_id=job.id, candidate_id=c["candidate_id"], info=c, shortlisted=True))
        job.analysis = {
            **job.analysis,
            "candidate_index": relative,
            "screenshot_shared_revision": index["decoder"]["shared_revision"],
            "screenshot_shared_seen": index["decoder"]["shared_seen"],
            "screenshot_more": {
                "requested": len(added),
                "added": len(added),
                "candidate_ids": [c["candidate_id"] for c in added],
                "warning": None,
            },
        }

    return save
