"""Program-first subtitle content classification, with streamed visual fallback."""

import json
import re
import sys
import time
from pathlib import Path

import httpx

from shared.config import behavior
from shared.naming import automatic_track_name, track_name
from shared.paths import contained, job_dir, write_json
from shared.subtitles import SUBTITLE_ANALYSIS_VERSION, SubtitleDecision, detected_language, validate_evidence
from shared.tracks import apply_flag_overrides
from worker.adapters.screenshot_agent import consume_stream
from worker.adapters.source_tracks import link_file
from worker.adapters.subtitle_rules import decide
from worker.progress import plan


def review(ctx, track_id=None, *, discovery=False, phase=None):
    label = "Track review agent" if track_id is None else "Subtitle agent"
    endpoint = "/review-tracks" if track_id is None else "/classify-subtitles"
    if discovery:
        label, endpoint = "Subtitle discovery agent", "/discover-subtitles"
    failures = 0
    busy_deadline = time.monotonic() + behavior()["agent"]["timeout_seconds"] + 60
    while True:
        ctx.check()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(behavior()["agent"]["timeout_seconds"] + 60, connect=10)
            ) as client:
                with client.stream(
                    "POST",
                    ctx.settings.agent_url + endpoint,
                    headers={
                        "Authorization": f"Bearer {ctx.settings.agent_token}",
                        "Accept": "application/x-ndjson",
                    },
                    json={
                        "job_id": ctx.job.id,
                        "task_id": ctx.task_id,
                        **({"track_id": track_id} if track_id is not None else {}),
                    },
                ) as response:
                    if response.status_code == 409:
                        reason = f"{label} is busy"
                    elif response.is_error:
                        response.read()
                        raise RuntimeError(f"{label} returned {response.status_code}: {response.text[:3000]}")
                    else:
                        if not response.headers.get("content-type", "").startswith("application/x-ndjson"):
                            raise ValueError(
                                f"{label} did not return an event stream; rebuild the agent service"
                            )
                        return consume_stream(ctx, response, label=label, phase=phase)
        except (httpx.NetworkError, httpx.RemoteProtocolError, httpx.ConnectTimeout) as error:
            failures += 1
            reason = f"{label} connection interrupted ({type(error).__name__})"
        if failures >= 4 or time.monotonic() >= busy_deadline:
            raise RuntimeError(f"{reason}; retry the task after checking the agent service")
        ctx.log(reason + "; waiting to retry")
        ctx.progress(
            None,
            phase=f"{phase} · Waiting for {label.lower()}" if phase else f"Waiting for {label.lower()}",
        )
        for _ in range(5 * 2 ** min(failures, 2)):
            ctx.check()
            time.sleep(1)


def prepare(ctx, track, source):
    track_id = track["track_id"]
    original = track.get("source_subtitle_metadata") or {
        "language": track.get("language", "und"),
        "name": track.get("name", ""),
        "hearing_impaired": track.get("source_properties", {}).get(
            "flag_hearing_impaired", track.get("hearing_impaired")
        ),
    }
    root = contained(
        job_dir(job_dir(ctx.settings.cache_root / "agent", ctx.job.id), ctx.task_id), f"subtitle-{track_id}"
    )
    root.mkdir(parents=True, exist_ok=True)
    ctx.progress(None, phase=f"Reading subtitle track {track_id}")
    buffer = ""

    def progress(chunk):
        nonlocal buffer
        buffer = (buffer + chunk)[-256:]
        matches = list(re.finditer(r"SUBTITLE_OCR (\d+)/(\d+)\n", buffer))
        if matches:
            match = matches[-1]
            buffer = buffer[match.end() :]
            return {
                "percentage": 95 * int(match[1]) / max(1, int(match[2])),
                "completed_cues": int(match[1]),
                "total_cues": int(match[2]),
                "phase": f"Reading subtitle track {track_id}: {match[1]}/{match[2]} cues",
            }

    limit = behavior()["integrations"].get("subtitle_detection", {}).get("sample_cues", 96)
    cache_path = contained(ctx.workspace, f"track-analysis/ocr-{track_id}.json")
    signature = None
    if source.is_file():
        stat = source.stat()
        signature = [stat.st_size, stat.st_mtime_ns, original["language"], limit, SUBTITLE_ANALYSIS_VERSION]
    report = None
    if signature is not None and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text())
            if cached["signature"] == signature:
                previous = contained(ctx.settings.cache_root, cached["root"])
                candidate = json.loads(contained(previous, "ocr.json", exists=True).read_text())
                names = [
                    "ocr.json",
                    *candidate["contact_sheets"],
                    *(sample["image"] for sample in candidate["samples"]),
                ]
                images = [contained(previous, name, exists=True) for name in names]
                if all(path.stat().st_size for path in images):
                    for name, path in zip(names, images, strict=True):
                        link_file(path, contained(root, name))
                    report = candidate
                    ctx.log(f"Reusing OCR evidence for subtitle track {track_id}.")
        except (OSError, ValueError, KeyError):
            pass  # A missing cache is rebuilt from the already extracted subtitle.
    if report is None:
        ctx.run(
            [
                sys.executable,
                Path(__file__).with_name("subtitle_ocr.py"),
                source,
                root,
                "--limit",
                str(limit),
                "--language",
                original["language"],
                "--sup2sup-source",
                Path(ctx.settings.sup2sup_bin).resolve().parents[2] / "src",
            ],
            progress_parser=progress,
        )
        report = json.loads(contained(root, "ocr.json", exists=True).read_text())
        ctx.check()
        if signature is not None:
            write_json(
                cache_path, {"signature": signature, "root": str(root.relative_to(ctx.settings.cache_root))}
            )
    preliminary = (
        decide(report, original["language"])
        if report["samples"]
        else SubtitleDecision(
            language="unknown",
            language_code="und",
            script="unknown",
            language_confident=False,
            hearing_impaired=None,
            sdh_confident=False,
            language_evidence=[],
            sdh_evidence=[],
            explanation="No readable subtitle images were found for content detection.",
        )
    )
    ctx.progress(100, phase=f"Subtitle track {track_id} evidence ready")
    return root, original, report, preliminary


def classify(ctx, track, source, *, require_confident=True, agent_review=False, prepared=None):
    track_id = track["track_id"]
    if prepared is None:
        steps = plan(ctx, evidence=70, review=30)
        root, original, report, preliminary = prepare(steps["evidence"], track, source)
        steps["evidence"].done("Subtitle evidence ready")
        ctx = steps["review"]
    else:
        root, original, report, preliminary = prepared
    result = {
        **report,
        "track_id": track_id,
        "source_metadata": original,
        "program": preliminary.model_dump(),
        "method": "program",
    }
    path = ctx.output("subtitles", f"track-{track_id}.detection.json")

    def save_report():
        write_json(path, result)
        ctx.artifact(path, "SUBTITLE_DETECTION", info={"track_id": track_id})

    save_report()
    decision = preliminary
    if report["samples"] and (
        agent_review or not preliminary.language_confident or not preliminary.sdh_confident
    ):
        ctx.progress(None, phase=f"Reviewing subtitle track {track_id} with agent")
        # The agent receives content and coverage, never original names or flags.
        inventory = {k: v for k, v in report.items() if k != "schema_version"}
        inventory.update(track_id=track_id, program=preliminary.model_dump())
        write_json(root / "inventory.json", inventory)
        try:
            answer = review(ctx, track_id)
            reviewed = SubtitleDecision.model_validate(answer["decision"])
            validate_evidence(reviewed, {s["id"] for s in report["samples"]})
            result.update(method="program + agent", agent=answer)
            merged = preliminary.model_dump()
            if agent_review or not preliminary.language_confident:
                for key in ("language", "language_code", "script", "language_confident", "language_evidence"):
                    merged[key] = getattr(reviewed, key)
            if agent_review or not preliminary.sdh_confident:
                for key in ("hearing_impaired", "sdh_confident", "sdh_evidence"):
                    merged[key] = getattr(reviewed, key)
            merged["explanation"] = reviewed.explanation
            decision = SubtitleDecision.model_validate(merged)
        except Exception as error:
            result["error"] = str(error)
            save_report()
            raise
    language = detected_language(decision)
    resolved = decision.language_confident and decision.sdh_confident
    result["decision"] = decision.model_dump()
    result["status"] = "resolved" if resolved else "inconclusive"
    save_report()
    manually_reviewed_sdh = isinstance(track.get("flag_overrides", {}).get("hearing_impaired"), bool)
    if require_confident and not (
        decision.language_confident and (decision.sdh_confident or manually_reviewed_sdh)
    ):
        raise ValueError(
            f"Subtitle track {track_id}: content detection is inconclusive; review SUBTITLE_DETECTION "
            "and the agent transcript. Increase integrations.subtitle_detection.sample_cues (up to 192) "
            "and retry the task. Source SDH flags will not be copied."
        )
    hearing_impaired = decision.hearing_impaired if decision.sdh_confident else None
    detection = {
        "schema_version": SUBTITLE_ANALYSIS_VERSION,
        "method": result["method"],
        "status": result["status"],
        "language_confident": decision.language_confident,
        "sdh_confident": decision.sdh_confident,
        "language": decision.language,
        "language_code": language,
        "language_evidence": decision.language_evidence,
        "sdh_evidence": decision.sdh_evidence,
        # Only cite cues that were actually supplied to the visual review. The
        # track reviewer receives these excerpts alongside that review's findings.
        "evidence_cues": [
            {k: sample[k] for k in ("id", "seconds", "text", "confidence")}
            for sample in report["samples"]
            if sample["id"] in set(decision.language_evidence + decision.sdh_evidence)
        ],
        "script": decision.script,
        "hearing_impaired": hearing_impaired,
        "sampled_cues": report["sampled_cues"],
        "unique_cues": report["unique_cues"],
        "explanation": decision.explanation,
        "report": str(path.relative_to(ctx.workspace)),
    }
    updated = {
        **track,
        "language": language,
        "hearing_impaired": hearing_impaired,
        "source_subtitle_metadata": original,
        "subtitle_detection": detection,
    }
    updated = apply_flag_overrides(updated)
    updated["base_name"] = automatic_track_name({**updated, "hearing_impaired": False, "forced": False})
    updated["suggested_name"] = automatic_track_name(updated)
    updated["mux_name"] = track_name(updated)
    ctx.log(
        f"Subtitle track {track_id}: {updated['mux_name']} ({result['method']}; "
        f"{report['sampled_cues']}/{report['unique_cues']} distinct cues inspected)"
    )
    return updated
