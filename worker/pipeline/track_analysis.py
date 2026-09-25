"""Checkpoint track reviews while local preparation overlaps the agent."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy

from sqlalchemy import select

from backend.app.services import event, get_job
from shared.db import session
from shared.models import MovieTrack, Task
from shared.paths import write_json
from shared.subtitles import SUBTITLE_ANALYSIS_VERSION
from shared.tracks import track_analysis_complete
from worker.adapters import track_review as track_reviews
from worker.adapters.source_tracks import extract_tracks, source_lock
from worker.adapters.track_cache import SharedTrackAnalysis
from worker.progress import plan
from worker.runtime import Interrupted


def save_analysis(db, job, result):
    owned = {
        "video",
        "crop",
        "crop_source",
        "tracks",
        "chapters",
        "container",
        "source_video_timestamps",
        "subtitle_analysis_version",
        "track_review_version",
        "audio_comparison",
        "audio_review_round",
        "audio_review_next",
        "track_analysis_signature",
        "shared_track_analysis",
    }
    job.analysis = {**job.analysis, **deepcopy({k: v for k, v in result.items() if k in owned})}
    rows = {row.track_id: row for row in db.scalars(select(MovieTrack).where(MovieTrack.job_id == job.id))}
    for track in result["tracks"]:
        row = rows.get(track["track_id"])
        if row is None:
            row = MovieTrack(job_id=job.id, track_id=track["track_id"], kind=track["kind"])
            db.add(row)
        row.info = deepcopy(track)


def checkpoint(ctx, result):
    ctx.check()
    with session() as db:
        job = get_job(db, ctx.job.id, lock=True)
        task = db.scalar(select(Task).where(Task.id == ctx.task_id).with_for_update())
        if (
            not job
            or not task
            or task.run_token != ctx.token
            or task.status != "RUNNING"
            or task.cancel_requested
        ):
            raise Interrupted("Track analysis lost its task lease")
        save_analysis(db, job, result)
        event(db, job.id, "tracks_updated", task_id=ctx.task_id)
        db.commit()
    ctx.job.analysis = deepcopy(result)


def await_preparation(ctx, future):
    while True:
        ctx.check()
        try:
            return future.result(timeout=0.5)
        except TimeoutError:
            if future.done():
                return future.result()  # A tool timeout is a real failure, not a polling timeout.


def analyze_tracks(ctx, *, scan, prepare_subtitle, classify_subtitle, review_tracks):
    result = deepcopy(ctx.job.analysis)
    shared = SharedTrackAnalysis(ctx)
    if track_analysis_complete(result) and not shared.needs_review(result):
        ctx.log("Reusing completed track analysis.")
        return lambda db, job: save_analysis(db, job, result)
    with source_lock(ctx, shared.root, phase="Waiting for the shared audio/subtitle analysis"):
        return analyze_shared_tracks(
            ctx,
            shared,
            scan=scan,
            prepare_subtitle=prepare_subtitle,
            classify_subtitle=classify_subtitle,
            review_tracks=review_tracks,
        )


def finish_analysis(ctx, result):
    path = ctx.output("metadata", "normalized.json")
    write_json(path, result)
    ctx.artifact(path, "SOURCE_METADATA")
    return lambda db, job: save_analysis(db, job, result)


def analyze_shared_tracks(ctx, shared, *, scan, prepare_subtitle, classify_subtitle, review_tracks):
    result = deepcopy(ctx.job.analysis)
    if shared.needs_review(result):
        # A retry under edited instructions reuses local samples/OCR, not old
        # agent conclusions. Manual overrides are preserved on each track.
        for track in result.get("tracks", []):
            track.pop("track_review", None)
            track.pop("subtitle_detection", None)
        for key in (
            "track_review_version",
            "subtitle_analysis_version",
            "audio_comparison",
            "audio_review_round",
            "audio_review_next",
            "shared_track_analysis",
        ):
            result.pop(key, None)
        ctx.log(
            "Agent prompts changed; reusing prepared evidence and reviewing it with the saved instructions."
        )

    def save_progress(value):
        value["track_analysis_signature"] = shared.signature
        checkpoint(ctx, value)
        shared.store(value)

    needs_scan = not all(key in result for key in ("video", "crop", "tracks"))
    steps = plan(
        ctx, scan=10 if needs_scan else 0, extraction=20, preparation=30, subtitles=25, comparison=25
    )
    if needs_scan:
        result = {**result, **scan(steps["scan"])}
        steps["scan"].done("Source scanned")
        checkpoint(ctx, result)
    result = shared.restore(result)
    extractable = [t for t in result["tracks"] if t.get("extractable") or t.get("codec_id") == "S_HDMV/PGS"]
    files, video_timestamps = extract_tracks(
        steps["extraction"], extractable, video_track_id=result["video"].get("track_id")
    )
    steps["extraction"].done("Source extraction ready")
    for track in extractable:
        data = files[track["track_id"]]
        audio = track["kind"] == "audio"
        relative = ctx.artifact(
            data["path"], "AUDIO" if audio else "SUBTITLE_ORIGINAL", info={"track_id": track["track_id"]}
        )
        track["source_track_path"] = relative
        if audio:
            track["source_timestamps_path"] = ctx.artifact(
                data["timestamps"], "AUDIO_TIMESTAMPS", info={"track_id": track["track_id"]}
            )
        else:
            track["subtitle_source_path"] = relative
    if video_timestamps is not None:
        result["source_video_timestamps"] = ctx.artifact(video_timestamps, "SOURCE_VIDEO_TIMESTAMPS")
    save_progress(result)
    if track_analysis_complete(result):
        ctx.progress(99.9, phase="Shared track analysis ready")
        return finish_analysis(ctx, result)
    pending = [
        t
        for t in extractable
        if t["kind"] == "subtitles"
        and t.get("subtitle_detection", {}).get("schema_version") != SUBTITLE_ANALYSIS_VERSION
    ]
    audio = [t for t in result["tracks"] if t["kind"] == "audio" and "audio_analysis" not in t]
    prepared_ctx = steps["preparation"].branch()
    weights = {str(t["track_id"]): 1 for t in pending}
    if audio:
        weights["audio"] = max(1, len(audio))
    prep_steps = plan(prepared_ctx, **weights) if weights else {}
    review_steps = plan(steps["subtitles"], **{str(t["track_id"]): 1 for t in pending}) if pending else {}
    if not weights:
        steps["preparation"].done("Reusing local track evidence")
    if not pending:
        steps["subtitles"].done("Reusing subtitle reviews")

    def prepare_step(step, function, *args):
        value = function(step, *args)
        step.done("Local track evidence ready")
        return value

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="track-preparation")
    try:
        # One local tool at a time bounds CPU use and keeps tool progress/logs distinct.
        # The agent lane consumes each prepared track while the next one is read.
        futures = [
            executor.submit(
                prepare_step,
                prep_steps[str(track["track_id"])],
                prepare_subtitle,
                track,
                files[track["track_id"]]["path"],
            )
            for track in pending
        ]
        audio_future = (
            executor.submit(
                prepare_step,
                prep_steps["audio"],
                track_reviews.analyze_audio,
                audio,
                result["video"]["duration"],
            )
            if audio
            else None
        )
        by_id = {t["track_id"]: t for t in result["tracks"]}
        for track, future in zip(pending, futures, strict=True):
            prepared = await_preparation(ctx, future)
            track_id = track["track_id"]
            ctx.log(f"Reviewing subtitle track {track_id}; other tracks are being prepared locally.")
            by_id[track_id] = classify_subtitle(
                review_steps[str(track_id)],
                track,
                files[track_id]["path"],
                prepared=prepared,
                require_confident=False,
                agent_review=True,
            )
            review_steps[str(track_id)].done(f"Subtitle track {track_id} reviewed")
            result["tracks"] = [by_id[t["track_id"]] for t in result["tracks"]]
            save_progress(result)
        if audio_future is not None:
            evidence = await_preparation(ctx, audio_future)
            result["tracks"] = [
                {**t, **({"audio_analysis": evidence[t["track_id"]]} if t["track_id"] in evidence else {})}
                for t in result["tracks"]
            ]
            save_progress(result)
    finally:
        prepared_ctx.local_cancel.set()
        executor.shutdown(wait=True, cancel_futures=True)
    result["subtitle_analysis_version"] = SUBTITLE_ANALYSIS_VERSION
    result["tracks"] = review_tracks(steps["comparison"], result, on_progress=save_progress)
    steps["comparison"].done("Track comparisons complete")
    result["track_review_version"] = 1
    save_progress(result)
    return finish_analysis(ctx, result)
