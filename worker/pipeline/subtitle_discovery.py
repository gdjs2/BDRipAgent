"""Find, align, render and retain missing subtitles without changing source tracks."""

import copy
import hashlib
import json
import shutil
import time
from fractions import Fraction
from uuid import uuid4

from sqlalchemy import select

from backend.app import queue
from backend.app.services import event, get_job
from backend.app.subtitle_uploads import register_upload, validate_subtitle
from backend.app.track_choices import source_key, track_rows
from shared.config import behavior
from shared.db import session
from shared.models import SourceTrackChoices, Task, now
from shared.paths import contained, job_dir, write_json
from shared.subtitle_discovery import DiscoveryPolicy, SearchResult, SubtitleReview, covers, missing_languages
from shared.subtitles import SUBTITLE_ANALYSIS_VERSION
from worker.adapters.handbrake import Crop
from worker.adapters.integrations import Sup2supAdapter, pgs_dimensions
from worker.adapters.source_tracks import source_lock
from worker.adapters.subtitle_alignment import fit_alignment, read_text, retime_text
from worker.adapters.subtitle_cleanup import clean_subtitles
from worker.adapters.subtitle_download import fetch_candidate, public_url
from worker.adapters.subtitles import review
from worker.runtime import Interrupted


def source_references(ctx):
    cues = []
    for track in ctx.job.analysis.get("tracks", []):
        if track["kind"] != "subtitles" or track.get("forced"):
            continue
        detection = track.get("subtitle_detection", {})
        samples = detection.get("evidence_cues", [])
        if detection.get("report"):
            try:
                samples = json.loads(contained(ctx.workspace, detection["report"], exists=True).read_text())[
                    "samples"
                ]
            except (OSError, ValueError, KeyError):
                pass
        if not samples and track.get("source_track_path", "").endswith((".srt", ".ass", ".ssa")):
            _, _, report = read_text(contained(ctx.workspace, track["source_track_path"], exists=True))
            samples = report["sampled_cues"]
        for sample in samples[:192]:
            if sample.get("text", "").strip():
                cues.append(
                    {
                        "id": f"{track['track_id']}:{sample['id']}",
                        "track_id": track["track_id"],
                        "language": track.get("language", "und"),
                        "seconds": sample["seconds"],
                        "text": sample["text"][:500],
                    }
                )
    return cues[:768]


def agent_request(ctx, inventory):
    root = contained(
        job_dir(job_dir(ctx.settings.cache_root / "agent", ctx.job.id), ctx.task_id), "discovery"
    )
    write_json(root / "inventory.json", inventory)
    if inventory["mode"] == "clean":
        action = "Repairing" if inventory.get("previous_review") else "Cleaning"
        phase = (
            f"{action} {inventory['requested_language']} subtitles: "
            f"batch {inventory['batch']}/{inventory['total_batches']}"
        )
    elif inventory["mode"] == "align":
        phase = f"Aligning {inventory['candidate']['language']} subtitles against source dialogue"
    else:
        phase = "Searching for missing subtitles"
    ctx.progress(None, phase=phase)
    return review(ctx, discovery=True, phase=phase)


def checkpoint(ctx, report):
    ctx.check()
    path = ctx.output("subtitle-discovery", "report.json")
    write_json(path, report)
    with session() as db:
        queue.settings(db, lock=True)
        job = get_job(db, ctx.job.id, lock=True)
        task = db.scalar(select(Task).where(Task.id == ctx.task_id).with_for_update())
        if task.run_token != ctx.token or task.status != "RUNNING" or task.cancel_requested:
            raise Interrupted("Subtitle discovery lost its task lease")
        key = source_key(job)
        record = db.get(SourceTrackChoices, key)
        if record is None:
            record = SourceTrackChoices(source_key=key, revision=0, data={})
            db.add(record)
        record.data = {**record.data, "subtitle_discovery": copy.deepcopy(report)}
        record.updated_at = now()
        job.analysis = {**job.analysis, "subtitle_discovery": copy.deepcopy(report)}
        event(db, job.id, "subtitle_discovery_updated", task_id=ctx.task_id)
        db.commit()
    ctx.artifact(path, "SUBTITLE_DISCOVERY")


def render(ctx, text, output, language, video):
    width, height = video["width"], video["height"]
    font = {
        "zh-Hans": "Noto Sans CJK SC",
        "zh-Hant": "Noto Sans CJK TC",
        "ko": "Noto Sans CJK KR",
        "ja": "Noto Sans CJK JP",
    }.get(language, "Noto Sans")
    ctx.run(
        [
            ctx.settings.subtitleedit_bin,
            text,
            "bluraysup",
            f"--resolution:{width}x{height}",
            f"--fps:{float(Fraction(video['fps']))}",
            f"--font-name:{font}",
            f"--font-size:{max(12, round(height / 22.5))}",
            "--font-color:white",
            "--outline-color:black",
            "--outline-width:2",
            f"--bottom-top-margin:{max(10, height // 20)}",
            f"--output-folder:{output.parent}",
            f"--output-filename:{output.name}",
            "--overwrite",
        ]
    )
    validate_subtitle(output, ".sup")
    if pgs_dimensions(output) != (width, height):
        raise ValueError("Subtitle Edit output dimensions differ from the source video")


def process_file(ctx, candidate, path, filename, receipt, references, index, *, origin="discovery"):
    video = ctx.job.analysis["video"]
    duration = video["duration"]
    folder = path.parent
    ctx.progress(None, phase=f"Checking and aligning {candidate.language} subtitles")
    original_bytes = path.read_bytes()
    if path.suffix.lower() not in (".srt", ".ass", ".ssa"):
        raise ValueError(
            "An editable SRT/ASS subtitle is required for complete single-language cleanup; PGS-only downloads need human review"
        )
    text, _, _ = read_text(path, allow_corrupt_text=True)
    text, cleanup = clean_subtitles(ctx, text, candidate, agent_request, references=references)
    cleaned = folder / "cleaned.srt"
    text.save(str(cleaned), format_="srt", encoding="utf-8")
    ctx.artifact(cleaned, "SUBTITLE_DISCOVERY_CLEANED")
    write_json(folder / "cleanup.json", cleanup)
    ctx.artifact(folder / "cleanup.json", "SUBTITLE_DISCOVERY_CLEANUP")
    text, cues, quality = read_text(cleaned, allow_corrupt_text=True)
    inventory = {
        "mode": "align",
        "movie": {"title": ctx.job.title, "year": ctx.job.year, "imdb_id": ctx.job.imdb_id},
        "source_fps": video["fps"],
        "duration_seconds": duration,
        "candidate": candidate.model_dump(),
        "quality": {k: v for k, v in quality.items() if k != "sampled_cues"},
        "cleanup_report": {
            "critical_errors": cleanup["critical_errors"],
            "edited_cues": cleanup["edited_cues"],
        },
        "candidate_cues": quality["sampled_cues"],
        "reference_cues": references,
    }
    answer = agent_request(ctx, inventory)
    decision = SubtitleReview.model_validate(answer["decision"])
    if not covers(decision.language, candidate.language):
        raise ValueError("Downloaded subtitle content does not match the requested language/script")
    # Editorial defects are reported, independently of the verified timing fit.
    # Language and every anchor/FPS/residual check remain mandatory.
    alignment = fit_alignment(
        decision.model_copy(update={"usable": True}), quality["sampled_cues"], references, duration
    )
    critical_errors = list(cleanup["critical_errors"])
    critical_errors.extend(decision.issues)
    if not decision.usable:
        critical_errors.append(decision.explanation)
    critical_errors = list(dict.fromkeys(critical_errors))
    alignment["movie_fps"] = video["fps"]
    alignment["inferred_candidate_fps"] = float(Fraction(video["fps"])) * alignment["scale"]
    aligned = folder / "aligned.srt"
    pgs = folder / "aligned.sup"
    retime_text(text, aligned, alignment, duration)
    ctx.progress(None, phase=f"Rendering {candidate.language} PGS with Subtitle Edit")
    render(ctx, aligned, pgs, candidate.language, video)
    ctx.check()
    cropped = folder / "checked.cropped.sup"
    ctx.progress(None, phase=f"Checking {candidate.language} PGS with Sup2sup")
    Sup2supAdapter().crop(ctx, pgs, cropped, Crop(**ctx.job.analysis["crop"]), video)
    provenance = {
        "origin": origin,
        "critical_errors": critical_errors,
        "requires_attention": bool(critical_errors),
        "source_url": candidate.source_url,
        "download_url": receipt["url"],
        "source_filename": filename,
        "release": candidate.release,
        "selection_reason": candidate.reason,
        "original_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "review": decision.model_dump(),
        "quality": {k: v for k, v in quality.items() if k != "sampled_cues"},
        "alignment": alignment,
        "converter": "Subtitle Edit 5.2.0",
        "cleanup": cleanup,
        "crop": ctx.job.analysis["crop"],
        "source_video": {k: video[k] for k in ("width", "height")},
        "crop_checked": True,
        "fetched_at": now().isoformat(),
    }
    write_json(folder / "review.json", provenance)
    ctx.artifact(folder / "review.json", "SUBTITLE_DISCOVERY_REVIEW")
    ctx.artifact(cropped, "SUBTITLE_DISCOVERY_PREVIEW")
    upload_id = str(uuid4())
    destination = contained(
        ctx.settings.workspace_root, f"uploaded-subtitles/{source_key(ctx.job)}/{upload_id}/subtitle.sup"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pgs, destination)
    detection = {
        "schema_version": SUBTITLE_ANALYSIS_VERSION,
        "method": "discovery agent + cue alignment",
        "status": "resolved" if decision.hearing_impaired is not None else "inconclusive",
        "language_code": candidate.language,
        "language_confident": True,
        "sdh_confident": decision.hearing_impaired is not None,
        "hearing_impaired": decision.hearing_impaired,
        "sampled_cues": len(quality["sampled_cues"]),
        "unique_cues": quality["cues"],
        "explanation": decision.explanation,
    }
    registered = False
    try:
        result = register_upload(
            ctx.job.id,
            destination,
            filename,
            candidate.language,
            decision.hearing_impaired,
            upload_id,
            provenance=provenance,
            detection=detection,
            task_identity=(ctx.task_id, ctx.token),
            origin=origin,
        )
        registered = True
        if not result["duplicate"]:
            # Retain both versions outside a job directory for source-wide reuse.
            shutil.copy2(cropped, destination.with_name("cropped.sup"))
            shutil.copy2(cropped.with_suffix(".report.json"), destination.with_name("crop.report.json"))
            destination.with_name("original" + path.suffix).write_bytes(original_bytes)
            shutil.copy2(cleaned, destination.with_name("cleaned.srt"))
            write_json(destination.with_name("discovery.json"), provenance)
        return {"track_id": result["track"]["track_id"], "duplicate": result["duplicate"], **provenance}
    finally:
        if not registered:
            destination.unlink(missing_ok=True)
            destination.parent.rmdir()


def discover_subtitles(ctx):
    policy = DiscoveryPolicy.model_validate(ctx.job.analysis.get("subtitle_discovery_policy", {}))
    key = source_key(ctx.job)
    signature = {
        "discovery_policy_version": 3,  # English coverage, Chinese normalization, and evidence-ranked candidates.
        "title": ctx.job.title,
        "year": ctx.job.year,
        "original_languages": policy.original_languages,
    }
    limits = behavior()["integrations"].get("subtitle_discovery", {})
    deadline = time.monotonic() + max(60, min(7200, int(limits.get("max_seconds", 1800))))
    ctx = ctx.branch()
    original_check = ctx.check

    def check():
        original_check()
        if time.monotonic() > deadline:
            raise TimeoutError(
                "Subtitle discovery reached its time limit; review the partial report and retry if needed"
            )

    ctx.check = check
    with source_lock(
        ctx,
        ctx.settings.cache_root / "subtitle-discovery" / key,
        phase="Waiting for shared subtitle discovery",
    ):
        with session() as db:
            record = db.get(SourceTrackChoices, key)
            cached = record.data.get("subtitle_discovery", {}) if record else {}
            tracks = [t.info for t in track_rows(db, ctx.job).values()]
        if (
            cached.get("status") == "complete"
            and cached.get("signature") == signature
            and not ctx.job.analysis.get("subtitle_discovery_refresh")
        ):
            checkpoint(ctx, {**cached, "reused": True})
            return
        report = {
            "status": "searching",
            "signature": signature,
            "original_languages": policy.original_languages,
            "original_language_sources": [],
            "candidates": [],
            "added_tracks": [],
            "summary": "",
            "source_job_id": ctx.job.id,
            "task_id": ctx.task_id,
            "started_at": now().isoformat(),
        }
        checkpoint(ctx, report)
        ctx.progress(None, phase="Searching for missing original-language, English and Chinese subtitles")
        references = source_references(ctx)
        if policy.original_languages and not missing_languages(tracks, policy.original_languages):
            report.update(
                status="complete",
                missing=[],
                needs_review=False,
                original_language_unknown=False,
                summary="All requested subtitle languages are already present.",
                finished_at=now().isoformat(),
            )
            checkpoint(ctx, report)
            return
        search = SearchResult.model_validate(
            agent_request(
                ctx,
                {
                    "mode": "search",
                    "movie": {"title": ctx.job.title, "year": ctx.job.year, "imdb_id": ctx.job.imdb_id},
                    "original_languages": policy.original_languages,
                    "duration_seconds": ctx.job.analysis["video"]["duration"],
                    "source_fps": ctx.job.analysis["video"]["fps"],
                    "existing_subtitles": [
                        {k: t.get(k) for k in ("track_id", "kind", "language", "forced", "commentary")}
                        for t in tracks
                        if t["kind"] == "subtitles"
                    ],
                    "known_missing": missing_languages(tracks, policy.original_languages),
                },
            )["decision"]
        )
        originals = policy.original_languages or (
            search.original_languages if search.original_language_sources else []
        )
        report.update(
            original_languages=originals,
            original_language_sources=search.original_language_sources,
            summary=search.summary,
            status="processing",
        )
        missing = missing_languages(tracks, originals)
        remaining = set(missing)
        # Preserve the agent's quality/edition ranking; ASS or ZIP may contain
        # a better matching translation than a lower-ranked direct SRT.
        candidates = search.candidates
        maximum = max(1, min(12, int(limits.get("max_candidates", 9))))
        for index, candidate in enumerate(candidates[:maximum]):
            ctx.check()
            if not any(covers(candidate.language, code) for code in remaining):
                continue
            entry = {**candidate.model_dump(), "status": "checking", "issues": []}
            report["candidates"].append(entry)
            checkpoint(ctx, report)
            try:
                public_url(candidate.source_url)
                if not references:
                    raise ValueError(
                        "No readable existing source-subtitle cues are available for alignment; human alignment is required"
                    )
                directory = ctx.output("subtitle-discovery", f"candidate-{index}/placeholder").parent
                files = fetch_candidate(candidate, directory, ctx.check)
                for file_index, (path, filename, receipt) in enumerate(files[:3]):
                    try:
                        acquired = process_file(
                            ctx, candidate, path, filename, receipt, references, index * 3 + file_index
                        )
                        report["added_tracks"].append(acquired)
                        remaining = {code for code in remaining if not covers(candidate.language, code)}
                        entry.update(status="added", track_id=acquired["track_id"], filename=filename)
                        break
                    except (ValueError, RuntimeError, OSError) as error:
                        ctx.check()
                        entry["issues"].append(f"{filename}: {error}")
                if entry["status"] != "added":
                    entry["status"] = "needs_review"
            except (ValueError, RuntimeError, OSError) as error:
                ctx.check()
                entry.update(status="needs_review", issues=[str(error)])
            checkpoint(ctx, report)
        report.update(
            status="complete",
            missing=sorted(remaining),
            original_language_unknown=not originals,
            needs_review=bool(
                remaining or not originals or any(t.get("requires_attention") for t in report["added_tracks"])
            ),
            finished_at=now().isoformat(),
        )
        checkpoint(ctx, report)
        ctx.progress(
            99.9,
            phase="Subtitle discovery complete; review the added tracks"
            if report["added_tracks"]
            else "Subtitle search complete; review the report",
        )
