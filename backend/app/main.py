import asyncio
import json
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app import queue
from backend.app.agent_activity import router as agent_activity_router
from backend.app.agent_prompts import router as agent_prompts_router
from backend.app.auth import authenticated, issue_cookie
from backend.app.cpu_monitor import CpuMonitor
from backend.app.media_preview import MediaPreview
from backend.app.media_preview import router as media_preview_router
from backend.app.movie_metadata import IMDbLookupError, job_identity, lookup_imdb
from backend.app.release_defaults import (
    analysis_with_release_defaults,
    seed_existing_release_details,
    store_release_details,
)
from backend.app.remux import sync_shared_choices
from backend.app.schemas import (
    CreateJob,
    CreatePair,
    CurateScreenshots,
    HoldTask,
    Login,
    OrderQueue,
    ReleaseSource,
    ReplaceScreenshot,
    ReviewScreenshots,
    SampleMoreScreenshots,
    SaveReleaseDetails,
    SelectEncode,
    SelectScreenshotBestCount,
    SelectScreenshotDecoder,
    SelectScreenshots,
    SelectScreenshotStrategy,
    SelectTracks,
    UpdateQueue,
)
from backend.app.services import (
    advance,
    enqueue,
    event,
    get_job,
    job_detail,
    manifest,
    reconcile,
    serialize,
    serialize_tracks,
)
from backend.app.subtitle_guidance import router as subtitle_guidance_router
from backend.app.track_choices import source_peers, store_choices, track_rows
from shared.config import ScreenshotPolicy, behavior, get_settings, profiles
from shared.db import get_db, session
from shared.encoding import EncodeTarget, is_smoke_test
from shared.models import (
    Artifact,
    CRFResult,
    EncodeConfig,
    Event,
    MovieJob,
    Screenshot,
    Task,
    TrackSelection,
    now,
)
from shared.naming import release_name, source_description
from shared.paths import artifact_root, contained, job_dir
from shared.screenshot_rules import (
    check_manual_spacing,
    check_other_variants,
    is_b_frame_pair,
    other_variant_frames,
    reservation_for,
)
from shared.state import STAGES, TRACK_EDIT_STAGES, Stage, require_stage
from shared.subtitle_discovery import DiscoveryPolicy

DB = Annotated[Session, Depends(get_db)]


@asynccontextmanager
async def lifespan(app):
    if len(get_settings().api_token) < 24:
        raise RuntimeError("API_TOKEN must contain at least 24 characters")
    with session() as db:
        queue.settings(db, lock=True)
        seed_existing_release_details(db)
        reconcile(db)
        queue.settings(db)
        db.commit()
        for job in db.scalars(select(MovieJob).where(MovieJob.deleted_at.is_(None))):
            manifest(db, job)
    app.state.media_preview = MediaPreview()
    monitor = CpuMonitor(
        get_settings().cpu_stat_path, get_settings().cpu_loadavg_path, get_settings().cpu_info_path
    )
    monitor.sample()
    app.state.cpu_monitor = monitor
    sampling = asyncio.create_task(monitor.run())
    try:
        yield
    finally:
        await app.state.media_preview.close()
        sampling.cancel()
        with suppress(asyncio.CancelledError):
            await sampling


app = FastAPI(title="BDRip Agent", lifespan=lifespan)
api = APIRouter(prefix="/api", dependencies=[Depends(authenticated)])


@app.exception_handler(ValueError)
async def invalid(request, error):
    return JSONResponse({"detail": str(error)}, status_code=409)


@app.exception_handler(LookupError)
async def missing(request, error):
    return JSONResponse({"detail": str(error)}, status_code=404)


@app.exception_handler(IMDbLookupError)
async def imdb_error(request, error):
    return JSONResponse({"detail": str(error)}, status_code=error.status_code)


@app.exception_handler(IntegrityError)
async def conflict(request, error):
    return JSONResponse({"detail": "Conflicting concurrent update; refresh and try again"}, status_code=409)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/session")
def login(body: Login, response: Response):
    token = get_settings().api_token
    if not token or not secrets.compare_digest(body.token, token):
        raise HTTPException(401, "Invalid access token")
    response.set_cookie(
        "movie_session",
        issue_cookie(),
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="strict",
        max_age=86400,
    )
    return {"authenticated": True}


@app.delete("/api/session")
def logout(response: Response):
    response.delete_cookie("movie_session")
    return {"authenticated": False}


@api.get("/config")
def configuration():
    return {
        "profiles": profiles(),
        "screenshots": behavior()["screenshots"],
        "stages": STAGES,
        "audio_review": {
            "max_rounds": max(
                1, min(30, int(behavior()["integrations"].get("audio_review", {}).get("max_rounds", 6)))
            )
        },
        "release": {
            "upload_host": "TTG",
            "upload_configured": bool(get_settings().tu_ttg_token.get_secret_value().strip()),
        },
    }


@api.get("/system/cpu")
def cpu_usage(request: Request):
    return request.app.state.cpu_monitor.snapshot()


@api.get("/queue")
def queue_status(db: DB):
    return queue.snapshot(db)


@api.patch("/queue")
def update_queue(body: UpdateQueue, db: DB):
    config = queue.settings(db, lock=True)
    for field in ("max_encoding_tasks", "max_crf_tasks", "max_other_tasks"):
        value = getattr(body, field)
        if value is not None:
            if value > get_settings().worker_capacity:
                raise ValueError(
                    f"Each queue limit must be at most the worker capacity ({get_settings().worker_capacity} tasks)"
                )
            setattr(config, field, value)
    if body.paused is not None:
        config.paused = body.paused
    db.commit()
    return queue.snapshot(db)


@api.patch("/queue/tasks/{task_id}")
def hold_task(task_id: UUID, body: HoldTask, db: DB):
    queue.settings(db, lock=True)
    task = db.get(Task, str(task_id))
    if not task:
        raise LookupError("Task not found")
    job = get_job(db, task.job_id, lock=True)
    db.refresh(task)
    if task.status != "QUEUED":
        raise ValueError("Only queued tasks can be held or resumed")
    task.held, task.dispatched_at = body.held, None
    event(db, job.id, "queue_changed", task_id=task.id, held=body.held)
    db.commit()
    return queue.snapshot(db)


@api.put("/queue/order")
def order_queue(body: OrderQueue, db: DB):
    queue.settings(db, lock=True)
    tasks = {t.id: t for t in queue.pending(db, include_held=True)}
    order = [str(task_id) for task_id in body.task_ids]
    if set(order) != set(tasks):
        raise ValueError("The queue changed. Refresh before reordering it.")
    for index, task_id in enumerate(order):
        tasks[task_id].queue_priority = len(order) - index
        tasks[task_id].dispatched_at = None
    db.commit()
    return queue.snapshot(db)


@api.get("/sources")
def sources():
    root = get_settings().source_root
    found = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() != ".mkv" or any(p.endswith(".partial") for p in path.parts):
            continue
        try:
            safe = contained(root, path.relative_to(root), exists=True)
            found.append({"path": str(path.relative_to(root)), "size": safe.stat().st_size})
        except (ValueError, OSError):
            continue
    return found


@api.get("/metadata/imdb")
def imdb_metadata(imdb_id: str):
    return lookup_imdb(imdb_id)


def initialize_job(body: CreateJob, db, identity):
    queue.settings(db, lock=True)
    settings = get_settings()
    source = contained(settings.source_root, body.source_path, exists=True)
    if source.suffix.lower() != ".mkv" or any(p.endswith(".partial") for p in Path(body.source_path).parts):
        raise ValueError("Only completed MKV files are accepted")
    if body.analysis_profile not in profiles():
        raise ValueError("Unknown analysis profile")
    stat = source.stat()
    policy = body.screenshot_policy or ScreenshotPolicy.model_validate(behavior()["screenshots"])
    if policy.count != 7:
        raise ValueError("Each encode requires exactly seven comparison frames")
    job = MovieJob(
        source_path=str(source.relative_to(settings.source_root.resolve())),
        source_size=stat.st_size,
        source_mtime_ns=str(stat.st_mtime_ns),
        **identity,
        release_name=release_name(
            identity["title"], identity["year"], profiles()[body.analysis_profile]["codec"]
        ),
        analysis={
            "subtitle_discovery_policy": body.subtitle_discovery.model_dump(),
            "audio_review_policy": {
                "max_rounds": body.audio_review_max_rounds
                or max(
                    1, min(30, int(behavior()["integrations"].get("audio_review", {}).get("max_rounds", 6)))
                )
            },
        },
        analysis_profile=body.analysis_profile,
        screenshot_policy=policy.model_dump(),
    )
    db.add(job)
    db.flush()
    workspace = job_dir(settings.workspace_root, job.id)
    for folder in [
        "source",
        "metadata",
        "audio",
        "subtitles",
        "crf",
        "encode",
        "mux",
        "release",
        "logs",
        "screenshots/candidates",
        "screenshots/contact-sheets",
        "screenshots/selected",
        "screenshots/comparisons",
    ]:
        (workspace / folder).mkdir(parents=True, exist_ok=True)
    advance(db, job)
    return job


@api.post("/jobs", status_code=201)
def create_job(body: CreateJob, db: DB):
    job = initialize_job(body, db, job_identity(body))
    db.commit()
    manifest(db, job)
    return job_detail(db, job)


@api.post("/jobs/pair", status_code=201)
def create_pair(body: CreatePair, db: DB):
    available = profiles()
    names = [body.analysis_profile, body.second_profile]
    if any(n not in available for n in names) or {available[n]["codec"] for n in names} != {"x264", "x265"}:
        raise ValueError("Choose one x264 profile and one x265 profile")
    base = body.model_dump(exclude={"second_profile"})
    identity = job_identity(body)
    jobs = [initialize_job(CreateJob(**{**base, "analysis_profile": name}), db, identity) for name in names]
    db.commit()
    for job in jobs:
        manifest(db, job)
    return [job_detail(db, job) for job in jobs]


@api.get("/jobs")
def jobs(db: DB):
    return [
        job_detail(db, j)
        for j in db.scalars(
            select(MovieJob).where(MovieJob.deleted_at.is_(None)).order_by(MovieJob.created_at.desc())
        )
    ]


@api.get("/jobs/{job_id}")
def job(job_id: UUID, db: DB):
    return job_detail(db, get_job(db, str(job_id)))


@api.delete("/jobs/{job_id}")
def delete_job(job_id: UUID, db: DB, confirm: str):
    queue.settings(db, lock=True)
    job = get_job(db, str(job_id), lock=True)
    if confirm != job.id:
        raise ValueError("Explicit confirmation must equal the job ID")
    if db.scalar(select(Task).where(Task.job_id == job.id, Task.status == "RUNNING")):
        raise ValueError("Cancel the running task and wait for it to stop before removing the job")
    for task in db.scalars(select(Task).where(Task.job_id == job.id, Task.status == "QUEUED")):
        task.status, task.finished_at, task.dispatched_at = "CANCELLED", now(), None
        event(db, job.id, "task_cancelled", task_id=task.id, reason="Job removed from list")
    job.deleted_at = now()
    job.analysis = {**job.analysis, "file_cleanup": {"status": "pending"}}
    event(db, job.id, "job_removed", files_retained=False, file_cleanup="pending")
    db.commit()
    return {"deleted": True, "files_retained": False, "file_cleanup": "pending"}


@api.get("/jobs/{job_id}/analysis")
def analysis(job_id: UUID, db: DB):
    return analysis_with_release_defaults(db, get_job(db, str(job_id)))


@api.get("/jobs/{job_id}/tracks")
def tracks(job_id: UUID, db: DB):
    return serialize_tracks(db, get_job(db, str(job_id)))


@api.get("/languages/verify")
def verify_language(code: str):
    from shared.languages import language_tag
    from shared.naming import language_name

    canonical = language_tag(code.strip())
    return {"code": canonical, "language_name": language_name(canonical)}


@api.post("/jobs/{job_id}/tracks/upload", status_code=201)
async def upload_subtitle(job_id: UUID, request: Request, filename: str, code: str, hearing_impaired: bool):
    from backend.app.subtitle_uploads import receive_upload

    return await receive_upload(request, str(job_id), filename, code, hearing_impaired)


@api.get("/jobs/{job_id}/tracks/{track_id}/download")
def download_track(job_id: UUID, track_id: int, db: DB, variant: str = "track"):
    from backend.app.track_downloads import track_download

    if variant not in ("track", "original", "cleaned"):
        raise ValueError("Unknown track download version")
    return track_download(db, get_job(db, str(job_id)), track_id, variant)


@api.get("/jobs/{job_id}/subtitles/uploads/{upload_id}/download")
def download_subtitle_upload(job_id: UUID, upload_id: UUID, db: DB):
    from backend.app.track_downloads import uploaded_original

    return uploaded_original(db, get_job(db, str(job_id)), str(upload_id))


@api.get("/jobs/{job_id}/tracks/{track_id}/language")
def verify_track_language(job_id: UUID, track_id: int, code: str, db: DB):
    from shared.languages import language_tag
    from shared.naming import automatic_track_name, language_name

    job = get_job(db, str(job_id))
    row = track_rows(db, job).get(track_id)
    if row is None:
        raise LookupError("Track not found")
    canonical = language_tag(code.strip())
    info = {**row.info, "kind": row.kind, "language": canonical}
    return {
        "code": canonical,
        "language_name": language_name(canonical),
        "track_name": automatic_track_name(info),
        "base_name": automatic_track_name({**info, "forced": False, "hearing_impaired": False}),
    }


@api.delete("/jobs/{job_id}/subtitles/discovered/{track_id}")
def remove_discovered_subtitle(job_id: UUID, track_id: int, db: DB):
    from backend.app.subtitle_removal import remove_discovered

    job = remove_discovered(db, str(job_id), track_id)
    manifest(db, job)
    return job_detail(db, job)


@api.post("/jobs/{job_id}/subtitles/discover", status_code=202)
def discover_subtitles(job_id: UUID, body: DiscoveryPolicy, db: DB):
    from backend.app.subtitle_discovery import request_discovery

    queue.settings(db, lock=True)
    job = get_job(db, str(job_id), lock=True)
    request_discovery(db, job, body)
    db.commit()
    return job_detail(db, job)


@api.post("/jobs/{job_id}/tracks/analyze", status_code=202)
def analyze_tracks(job_id: UUID, db: DB):
    from backend.app.services import ensure_track_analysis

    job = get_job(db, str(job_id), lock=True)
    if db.scalar(select(TrackSelection).where(TrackSelection.job_id == job.id)):
        raise ValueError("Tracks have already been selected")
    if job.state not in TRACK_EDIT_STAGES | {Stage.ANALYZING_SOURCE}:
        raise ValueError("Track analysis is no longer available at this stage")
    ensure_track_analysis(db, job)
    db.commit()
    return job_detail(db, job)


@api.post("/jobs/{job_id}/tracks/selection")
def select_tracks(job_id: UUID, body: SelectTracks, db: DB):
    queue.settings(db, lock=True)
    job = get_job(db, str(job_id), lock=True)
    if job.state not in TRACK_EDIT_STAGES:
        raise ValueError("Track choices are locked once preparation for remux starts")
    if db.scalar(
        select(Task.id).where(
            Task.job_id == job.id, Task.lane == "tracks", Task.status.in_(["QUEUED", "RUNNING"])
        )
    ):
        raise ValueError("Wait for track review to finish before confirming tracks")
    record = store_choices(db, job, body)
    changed = [job]
    peers = db.scalars(
        select(MovieJob)
        .where(*source_peers(job), MovieJob.id != job.id)
        .order_by(MovieJob.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    for peer in peers:
        if sync_shared_choices(db, peer, record):
            changed.append(peer)
    for current in changed:
        if current.state == Stage.WAITING_FOR_TRACK_SELECTION:
            advance(db, current)
    db.commit()
    for current in changed:
        manifest(db, current)
    return job_detail(db, job)


@api.post("/jobs/{job_id}/remux", status_code=202)
def remux(job_id: UUID, body: SelectTracks, db: DB):
    from backend.app.remux import request_remux

    job = request_remux(db, str(job_id), body)
    manifest(db, job)
    return job_detail(db, job)


@api.get("/jobs/{job_id}/crf-analysis")
def crf(job_id: UUID, db: DB):
    return job_detail(db, get_job(db, str(job_id)))["crf"]


@api.post("/jobs/{job_id}/encode-selection")
def select_encode(job_id: UUID, body: SelectEncode, db: DB):
    job = get_job(db, str(job_id), lock=True)
    require_stage(job.state, Stage.WAITING_FOR_ENCODE_SELECTION)
    result = db.scalar(select(CRFResult).where(CRFResult.job_id == job.id))
    profile = result.data.get("profile_snapshot") if result else None
    if not profile or profile["codec"] != body.codec:
        raise ValueError("Codec and profile must match an installed profile")
    if body.profile != job.analysis_profile:
        raise ValueError(
            "Choose the profile used for CRF analysis; create a separate job for a different profile"
        )
    if body.rate_control == "crf" and not profile["crf_min"] <= body.crf <= profile["crf_max"]:
        raise ValueError("CRF is outside profile limits")
    db.add(
        EncodeConfig(
            job_id=job.id,
            data={
                **body.model_dump(exclude_none=True),
                "profile_snapshot": profile,
                "selected_by": "user",
                "selected_at": now().isoformat(),
            },
        )
    )
    advance(db, job)
    db.commit()
    manifest(db, job)
    return job_detail(db, job)


@api.patch("/jobs/{job_id}/encode-selection")
def update_encode_target(job_id: UUID, body: EncodeTarget, db: DB):
    # Match worker admission lock order: a queued encode either sees the new
    # target or starts first and makes the edit fail, never a partially changed target.
    queue.settings(db, lock=True)
    job = get_job(db, str(job_id), lock=True)
    require_stage(job.state, Stage.ENCODING)
    task = db.scalar(
        select(Task)
        .where(Task.job_id == job.id, Task.type == "encode")
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(1)
        .with_for_update()
    )
    if not task or task.status not in ("QUEUED", "CANCELLED", "FAILED"):
        raise ValueError(
            "The encode has started. Cancel it and wait for it to stop before changing its target."
        )
    config = db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job.id))
    if not config or is_smoke_test(job) or config.data.get("execution_mode") == "smoke":
        raise ValueError("This job has no editable encoding target")
    profile = config.data["profile_snapshot"]
    if body.rate_control == "crf" and not profile["crf_min"] <= body.crf <= profile["crf_max"]:
        raise ValueError("CRF is outside profile limits")
    previous = {
        key: config.data[key] for key in ("rate_control", "crf", "bitrate_kbps") if key in config.data
    }
    target = body.model_dump(exclude_none=True)
    config.data = {
        **{
            key: value
            for key, value in config.data.items()
            if key not in ("rate_control", "crf", "bitrate_kbps")
        },
        **target,
        "selected_by": "user",
        "selected_at": now().isoformat(),
    }
    event(db, job.id, "encode_target_updated", task_id=task.id, previous=previous, target=target)
    db.commit()
    manifest(db, job)
    return job_detail(db, job)


@api.post("/jobs/{job_id}/reencode", status_code=202)
def reencode(job_id: UUID, body: EncodeTarget, db: DB):
    from backend.app.reencode import request as request_reencode

    job = request_reencode(db, str(job_id), body)
    manifest(db, job)
    return job_detail(db, job)


@api.post("/jobs/{job_id}/smoke-test")
def start_smoke_test(job_id: UUID, db: DB):
    job = get_job(db, str(job_id), lock=True)
    require_stage(job.state, Stage.WAITING_FOR_ENCODE_SELECTION)
    result = db.scalar(select(CRFResult).where(CRFResult.job_id == job.id))
    profile = result.data.get("profile_snapshot") if result else None
    if not profile or not job.analysis.get("video"):
        raise ValueError("Complete source and CRF analysis first")
    db.add(
        EncodeConfig(
            job_id=job.id,
            data={
                "execution_mode": "smoke",
                "codec": profile["codec"],
                "profile": job.analysis_profile,
                "profile_snapshot": profile,
                "selected_by": "user",
                "selected_at": now().isoformat(),
            },
        )
    )
    job.analysis = {**job.analysis, "smoke_test": True}
    job.release_name = "SMOKE-TEST." + job.release_name.removesuffix("-WiKi")[:220] + "-WiKi"
    event(db, job.id, "smoke_test_selected", source_video_reused=True, encode_validation_skipped=True)
    advance(db, job)
    db.commit()
    manifest(db, job)
    return job_detail(db, job)


@api.get("/jobs/{job_id}/encode")
def encode_status(job_id: UUID, db: DB):
    detail = job_detail(db, get_job(db, str(job_id)))
    return {
        "config": detail["encode_config"],
        "validation": detail["validation"],
        "tasks": [t for t in detail["tasks"] if t["type"] in ["encode", "validate", "mux"]],
    }


def stage_endpoint(stage):
    def endpoint(job_id: UUID, db: DB):
        job = get_job(db, str(job_id), lock=True)
        require_stage(job.state, stage)
        task = enqueue(db, job)
        db.commit()
        return serialize(task)

    return endpoint


for route, stage in [
    ("analyze", Stage.ANALYZING_SOURCE),
    ("crf-analysis", Stage.RUNNING_CRF_ANALYSIS),
    ("encode", Stage.ENCODING),
    ("screenshots/generate-candidates", Stage.SCREENSHOT_CANDIDATE_GENERATION),
    ("screenshots/select", Stage.SCREENSHOT_AGENT_SELECTION),
    ("screenshots/render", Stage.SCREENSHOT_RENDERING),
]:
    api.add_api_route(
        "/jobs/{job_id}/" + route,
        stage_endpoint(stage),
        methods=["POST"],
        status_code=202,
        name=route.replace("/", "_"),
    )


@api.get("/jobs/{job_id}/screenshots")
def screenshots(job_id: UUID, db: DB):
    job = get_job(db, str(job_id))
    rows = db.scalars(
        select(Screenshot).where(Screenshot.job_id == str(job_id)).order_by(Screenshot.candidate_id)
    ).all()
    reserved = other_variant_frames(db, job)
    items = []
    for row in rows:
        match = reservation_for(row.info, reserved)
        items.append(
            {
                **serialize(row),
                "reservation": {
                    "job_id": match["job_id"],
                    "codec": match["codec"],
                    "frame_number": match["source_frame_number"],
                    "spacing_seconds": match["spacing"],
                }
                if match
                else None,
            }
        )
    return {
        "candidates": len(rows),
        "shortlisted": sum(r.shortlisted for r in rows),
        "recommended": sum(bool(r.info.get("recommendation_rank")) for r in rows),
        "final": sum(r.selected for r in rows),
        "items": items,
    }


@api.patch("/jobs/{job_id}/screenshots/decoder")
def screenshot_decoder(job_id: UUID, body: SelectScreenshotDecoder, db: DB):
    job = get_job(db, str(job_id), lock=True)
    if db.scalar(
        select(Task.id).where(
            Task.job_id == job.id, Task.type == "generate_candidates", Task.status == "RUNNING"
        )
    ):
        raise ValueError("The screenshot scan is running. Cancel it before changing the decoder, then retry.")
    job.screenshot_policy = {**job.screenshot_policy, "decoder": body.decoder}
    event(db, job.id, "screenshot_decoder_selected", decoder=body.decoder)
    db.commit()
    manifest(db, job)
    return job_detail(db, job)


@api.patch("/jobs/{job_id}/screenshots/strategy")
def screenshot_strategy(job_id: UUID, body: SelectScreenshotStrategy, db: DB):
    queue.settings(db, lock=True)
    job = get_job(db, str(job_id), lock=True)
    if db.scalar(
        select(Task.id).where(
            Task.job_id == job.id,
            Task.type.in_(["generate_candidates", "select_screenshots"]),
            Task.status == "RUNNING",
        )
    ):
        raise ValueError(
            "Cancel the running screenshot task before changing strategy, then retry or refresh."
        )
    job.screenshot_policy = {**job.screenshot_policy, "strategy": body.strategy}
    event(db, job.id, "screenshot_strategy_selected", strategy=body.strategy)
    db.commit()
    manifest(db, job)
    return job_detail(db, job)


@api.patch("/jobs/{job_id}/screenshots/best-count")
def screenshot_best_count(job_id: UUID, body: SelectScreenshotBestCount, db: DB):
    job = get_job(db, str(job_id), lock=True)
    if db.scalar(
        select(Task.id).where(
            Task.job_id == job.id, Task.type == "select_screenshots", Task.status == "RUNNING"
        )
    ):
        raise ValueError(
            "Screenshot review is running. Wait for it to finish or cancel it before changing the count."
        )
    job.screenshot_policy = {**job.screenshot_policy, "best_count": body.best_count}
    event(db, job.id, "screenshot_best_count_updated", best_count=body.best_count)
    db.commit()
    manifest(db, job)
    return job_detail(db, job)


@api.post("/jobs/{job_id}/screenshots/selection")
def choose_screenshots(job_id: UUID, body: SelectScreenshots, db: DB):
    job = get_job(db, str(job_id), lock=True)
    if job.state not in (
        Stage.WAITING_FOR_SCREENSHOT_SELECTION,
        Stage.WAITING_FOR_RELEASE_DETAILS,
        Stage.COMPLETE,
    ):
        raise ValueError("Wait for screenshot review before choosing the final pairs")
    rows = db.scalars(select(Screenshot).where(Screenshot.job_id == job.id)).all()
    choices = {row.candidate_id: row for row in rows}
    proposed = []
    for candidate_id in body.candidate_ids:
        row = choices.get(candidate_id)
        if not row or not row.shortlisted or not row.info.get("recommendation_rank"):
            raise ValueError("Choose final pairs from your best review options")
        if not is_b_frame_pair(row.info):
            raise ValueError("Both source and encoded images must be verified B-frames")
        proposed.append(row.info)
    check_other_variants(db, job, proposed)
    # A person may choose any subset; category and timeline coverage quotas
    # guide the recommendations, while frame/scene spacing remains mandatory.
    check_manual_spacing(
        proposed,
        {**job.screenshot_policy, "min_timeline_bins": 1},
        job.validation["metrics"]["source_duration"],
    )
    for row in rows:
        row.selected = row.candidate_id in body.candidate_ids
        if row.selected:
            row.info = {**row.info, "selected_by": "user"}
    job.analysis = {
        **{k: v for k, v in job.analysis.items() if k != "release_result"},
        "screenshot_selection": {
            "candidate_ids": body.candidate_ids,
            "count": len(proposed),
            "selected_by": "user",
        },
    }
    job.state, job.completed_at = Stage.SCREENSHOT_RENDERING.value, None
    event(db, job.id, "state_changed", state=job.state, selected_by="user")
    enqueue(db, job)
    db.commit()
    manifest(db, job)
    return job_detail(db, job)


@api.patch("/jobs/{job_id}/screenshots/best")
def curate_screenshots(job_id: UUID, body: CurateScreenshots, db: DB):
    job = get_job(db, str(job_id), lock=True)
    if job.state not in (
        Stage.WAITING_FOR_SCREENSHOT_SELECTION,
        Stage.WAITING_FOR_RELEASE_DETAILS,
        Stage.COMPLETE,
    ):
        raise ValueError("Wait for screenshot review before editing the best list")
    rows = db.scalars(select(Screenshot).where(Screenshot.job_id == job.id)).all()
    choices = {row.candidate_id: row for row in rows}
    for candidate_id in body.candidate_ids:
        row = choices.get(candidate_id)
        if not row or not row.shortlisted:
            raise ValueError("Add screenshots from this job's shortlist")
        if not is_b_frame_pair(row.info):
            raise ValueError("Both source and encoded images must be verified B-frames")
    ranks = {candidate_id: rank for rank, candidate_id in enumerate(body.candidate_ids, 1)}
    for row in rows:
        row.info = {**row.info, "recommendation_rank": ranks.get(row.candidate_id)}
    # Curation does not change rendered finals or reserve frames for a codec.
    # Spacing and cross-codec checks run when the person confirms the final set.
    event(db, job.id, "screenshot_best_updated", candidate_ids=body.candidate_ids)
    db.commit()
    manifest(db, job)
    return screenshots(job_id, db)


@api.post("/jobs/{job_id}/screenshots/review", status_code=202)
def prepare_screenshot_review(job_id: UUID, db: DB, body: ReviewScreenshots | None = None):
    queue.settings(db, lock=True)
    job = get_job(db, str(job_id), lock=True)
    if job.state not in (
        Stage.SCREENSHOT_AGENT_SELECTION,
        Stage.SCREENSHOT_CANDIDATE_GENERATION,
        Stage.WAITING_FOR_SCREENSHOT_SELECTION,
        Stage.WAITING_FOR_RELEASE_DETAILS,
        Stage.COMPLETE,
    ):
        raise ValueError("Wait for screenshot review before refreshing the best choices")
    if db.scalar(select(Task.id).where(Task.job_id == job.id, Task.status.in_(["QUEUED", "RUNNING"]))):
        raise ValueError("Wait for the current task to finish or cancel it before refreshing screenshots")
    ids = list(
        db.scalars(
            select(Screenshot.candidate_id).where(
                Screenshot.job_id == job.id, Screenshot.shortlisted.is_(True)
            )
        )
    )
    if not ids and not job.analysis.get("candidate_index") and not (body and (body.resample or body.append)):
        raise ValueError("This job has no saved shortlist to review")
    if body and body.best_count is not None:
        job.screenshot_policy = {**job.screenshot_policy, "best_count": body.best_count}
    if body and body.strategy is not None:
        job.screenshot_policy = {**job.screenshot_policy, "strategy": body.strategy}
    job.analysis = {
        **{k: v for k, v in job.analysis.items() if k != "screenshot_append"},
        "review_shortlisted_ids": ids,
    }
    if body and body.append:
        if not job.analysis.get("candidate_index"):
            raise ValueError("Prepare the initial screenshot pool before requesting more frames")
        job.analysis = {
            **job.analysis,
            "screenshot_append": {"count": body.sample_count, "batch_id": str(uuid4())},
        }
    job.state, job.completed_at = (
        (
            Stage.SCREENSHOT_CANDIDATE_GENERATION.value
            if body and (body.resample or body.append)
            else Stage.SCREENSHOT_AGENT_SELECTION.value
        ),
        None,
    )
    event(db, job.id, "state_changed", state=job.state, review_requested=True)
    result = enqueue(db, job)
    db.commit()
    manifest(db, job)
    return serialize(result)


@api.post("/jobs/{job_id}/screenshots/more", status_code=202)
def more_screenshots(job_id: UUID, body: SampleMoreScreenshots, db: DB):
    return prepare_screenshot_review(job_id, db, ReviewScreenshots(append=True, sample_count=body.count))


@api.post("/jobs/{job_id}/screenshots/{screenshot_id}/replace")
def replace(job_id: UUID, screenshot_id: UUID, body: ReplaceScreenshot, db: DB):
    job = get_job(db, str(job_id), lock=True)
    require_stage(job.state, Stage.COMPLETE)
    old = db.get(Screenshot, str(screenshot_id))
    new = db.scalar(
        select(Screenshot).where(Screenshot.job_id == job.id, Screenshot.candidate_id == body.candidate_id)
    )
    if not old or old.job_id != job.id or not old.selected or not new or new.selected:
        raise ValueError("Select a final screenshot and an unused candidate")
    if not is_b_frame_pair(new.info):
        raise ValueError("Replacement requires verified B-frames in both source and encoded video")
    proposed = [
        s.info
        for s in db.scalars(
            select(Screenshot).where(
                Screenshot.job_id == job.id, Screenshot.selected.is_(True), Screenshot.id != old.id
            )
        )
    ] + [new.info]
    check_other_variants(db, job, proposed)
    policy = job.screenshot_policy
    confirmed = job.analysis.get("screenshot_selection")
    if confirmed:
        policy = {**policy, "min_timeline_bins": 1}
    check_manual_spacing(proposed, policy, job.validation["metrics"]["source_duration"])
    old.selected = False
    new.selected = True
    new.info = {
        **new.info,
        "category": old.info.get("category"),
        "reason": "Manual user replacement",
        "selected_by": "user",
    }
    if confirmed:
        job.analysis = {
            **job.analysis,
            "screenshot_selection": {
                **confirmed,
                "candidate_ids": [
                    new.candidate_id if i == old.candidate_id else i for i in confirmed["candidate_ids"]
                ],
            },
        }
    job.state = Stage.SCREENSHOT_RENDERING
    job.completed_at = None
    event(db, job.id, "state_changed", state=job.state, manual_override=True)
    enqueue(db, job)
    db.commit()
    return {"queued": True}


@api.post("/release/source-description")
def convert_source_description(body: ReleaseSource):
    # Conversion is explicit, so user-edited space-style descriptions remain intact.
    return ReleaseSource(source=source_description(body.source)).model_dump()


def editable_release(db, job, *, generating=False):
    if not generating:
        if db.scalar(
            select(Task.id).where(
                Task.job_id == job.id, Task.type == "generate_release", Task.status.in_(["QUEUED", "RUNNING"])
            )
        ):
            raise ValueError("Wait for release generation to finish before editing its details")
        return
    if job.state not in (Stage.WAITING_FOR_RELEASE_DETAILS, Stage.GENERATING_RELEASE, Stage.COMPLETE):
        raise ValueError("Choose and render your final screenshots before preparing the release")
    if db.scalar(select(Task.id).where(Task.job_id == job.id, Task.status.in_(["QUEUED", "RUNNING"]))):
        raise ValueError("Wait for the current task to finish before changing release details")


@api.patch("/jobs/{job_id}/release")
def save_release_details(job_id: UUID, body: SaveReleaseDetails, db: DB):
    queue.settings(db, lock=True)
    job = get_job(db, str(job_id), lock=True)
    editable_release(db, job)
    record = store_release_details(db, job, body)
    event(db, job.id, "release_details_saved", shared_revision=record.revision)
    db.commit()
    manifest(db, job)
    return job_detail(db, job)


@api.post("/jobs/{job_id}/release", status_code=202)
def generate_release(job_id: UUID, body: SaveReleaseDetails, db: DB):
    from shared.release import selected_pairs

    queue.settings(db, lock=True)
    job = get_job(db, str(job_id), lock=True)
    editable_release(db, job, generating=True)
    selected_pairs(db, job, get_settings())
    if body.upload_screenshots and not get_settings().tu_ttg_token.get_secret_value().strip():
        raise ValueError(
            "Set TU_TTG_TOKEN in .env and restart the API and worker to enable screenshot uploads"
        )
    store_release_details(db, job, body)
    job.analysis = {k: v for k, v in job.analysis.items() if k != "release_result"}
    job.state, job.completed_at = Stage.GENERATING_RELEASE.value, None
    event(db, job.id, "state_changed", state=job.state)
    enqueue(db, job)
    db.commit()
    manifest(db, job)
    return job_detail(db, job)


@api.get("/jobs/{job_id}/tasks")
def job_tasks(job_id: UUID, db: DB):
    get_job(db, str(job_id))
    return [
        serialize(t)
        for t in db.scalars(select(Task).where(Task.job_id == str(job_id)).order_by(Task.created_at))
    ]


def get_task(db, task_id):
    task = db.get(Task, str(task_id))
    if not task:
        raise LookupError("Task not found")
    get_job(db, task.job_id)
    return task


@api.get("/tasks/{task_id}")
def task(task_id: UUID, db: DB):
    return serialize(get_task(db, task_id))


@api.post("/tasks/{task_id}/retry", status_code=202)
def retry(task_id: UUID, db: DB):
    queue.settings(db, lock=True)
    task = get_task(db, task_id)
    job = get_job(db, task.job_id, lock=True)
    db.refresh(task)
    from backend.app.services import task_is_current

    if task.status not in ["FAILED", "CANCELLED"] or not task_is_current(job, task):
        raise ValueError("Only failed/cancelled tasks at the current stage can be retried")
    newest = db.scalar(
        select(Task)
        .where(Task.job_id == job.id, Task.stage == task.stage)
        .order_by(Task.created_at.desc())
        .limit(1)
    )
    if newest.id != task.id:
        raise ValueError("Retry the latest attempt")
    if task.type == "review_uploaded_subtitle":
        from backend.app.subtitle_uploads import upload_allowed

        upload_allowed(db, job)
        if db.scalar(
            select(Task.id)
            .join(MovieJob, MovieJob.id == Task.job_id)
            .where(*source_peers(job), Task.lane == "tracks", Task.status.in_(["QUEUED", "RUNNING"]))
        ):
            raise ValueError("Wait for the current subtitle search or review to finish")
    result = enqueue(db, job, retry_of=task)
    db.commit()
    return serialize(result)


def set_encoding_pause(db, task_id, paused):
    task = get_task(db, task_id)
    job = get_job(db, task.job_id, lock=True)
    task = db.scalar(
        select(Task).where(Task.id == task.id).with_for_update().execution_options(populate_existing=True)
    )
    if (
        task.type != "encode"
        or task.status != "RUNNING"
        or job.state != Stage.ENCODING
        or task.cancel_requested
    ):
        raise ValueError("Only an active encoding task can be paused or resumed")
    if not task.can_pause:
        raise ValueError("The encoder is not ready for pause/resume, or its worker does not support it")
    if task.pause_requested != paused:
        task.pause_requested = paused
        event(db, task.job_id, "task_pause_requested", task_id=task.id, paused=paused)
        db.commit()
    return serialize(task)


@api.post("/tasks/{task_id}/pause", status_code=202)
def pause_encoding(task_id: UUID, db: DB):
    return set_encoding_pause(db, task_id, True)


@api.post("/tasks/{task_id}/resume", status_code=202)
def resume_encoding(task_id: UUID, db: DB):
    return set_encoding_pause(db, task_id, False)


@api.post("/tasks/{task_id}/cancel")
def cancel(task_id: UUID, db: DB):
    task = get_task(db, task_id)
    get_job(db, task.job_id, lock=True)
    db.refresh(task)
    if task.status == "QUEUED":
        task.status = "CANCELLED"
        task.finished_at = now()
    elif task.status == "RUNNING":
        task.cancel_requested = True
    else:
        raise ValueError("Task is no longer active")
    event(db, task.job_id, "task_cancel_requested", task_id=task.id)
    db.commit()
    return serialize(task)


@api.get("/tasks/{task_id}/logs")
def logs(task_id: UUID, db: DB, offset: int = 0, limit: int = 65536, tail: bool = False):
    task = get_task(db, task_id)
    path = contained(job_dir(get_settings().workspace_root, task.job_id), task.log_path)
    offset = max(0, offset)
    if not path.exists():
        return {"text": "", "offset": 0}
    if tail:
        offset = max(0, path.stat().st_size - max(1, min(limit, 262144)))
    with path.open("rb") as stream:
        stream.seek(offset)
        text = stream.read(max(1, min(limit, 262144)))
        return {"text": text.decode(errors="replace"), "offset": stream.tell()}


@api.get("/tasks/{task_id}/agent-events")
async def agent_events(task_id: UUID, request: Request, db: DB):
    task = get_task(db, task_id)
    job_id = task.job_id
    try:
        cursor = int(request.headers.get("last-event-id", "0"))
        if not 0 <= cursor <= 2**63 - 1:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Invalid Last-Event-ID") from None
    db.close()

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            with session() as event_db:
                rows = event_db.scalars(
                    select(Event)
                    .where(
                        Event.job_id == job_id,
                        Event.type == "agent_output",
                        Event.id > cursor,
                        Event.data["task_id"].as_string() == str(task_id),
                    )
                    .order_by(Event.id)
                    .limit(200)
                ).all()
                current = event_db.get(Task, str(task_id))
                active = current is not None and current.status in ("QUEUED", "RUNNING")
            for row in rows:
                cursor = row.id
                yield f"id: {row.id}\nevent: agent_output\ndata: {json.dumps(row.data)}\n\n"
            if not rows and not active:
                yield "event: terminal\ndata: {}\n\n"
                return
            if not rows:
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.05 if rows else 0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@api.get("/jobs/{job_id}/encoder-info")
def encoder_info(job_id: UUID, db: DB):
    from backend.app.encode_summary import summary

    return summary(db, get_job(db, str(job_id)))


@api.get("/artifacts/{artifact_id}/preview")
def artifact_preview(artifact_id: UUID, db: DB):
    item = db.get(Artifact, str(artifact_id))
    if not item:
        raise LookupError("Artifact not found")
    get_job(db, item.job_id)
    if item.artifact_type not in (
        "RELEASE_BBCODE",
        "RELEASE_NFO",
        "RELEASE_MD5",
        "RELEASE_ENCODER_INFO",
        "ENCODER_INFO",
    ):
        raise ValueError("This artifact is not a release text file")
    root = artifact_root(get_settings(), item.job_id, item.storage)
    path = contained(root, item.path, exists=True)
    limit = 1024 * 1024
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    encoding = "cp437" if item.artifact_type == "RELEASE_NFO" else "utf-8-sig"
    return {
        "filename": path.name,
        "text": data[:limit].decode(encoding, errors="replace"),
        "truncated": len(data) > limit,
    }


@api.get("/artifacts/{artifact_id}")
def artifact(artifact_id: UUID, db: DB):
    artifact = db.get(Artifact, str(artifact_id))
    if not artifact:
        raise LookupError("Artifact not found")
    get_job(db, artifact.job_id)
    root = artifact_root(get_settings(), artifact.job_id, artifact.storage)
    path = contained(root, artifact.path, exists=True)
    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="inline" if path.suffix.lower() in [".png", ".jpg"] else "attachment",
    )


@api.get("/jobs/{job_id}/events")
async def events(job_id: UUID, request: Request, db: DB):
    get_job(db, str(job_id))
    last_id = request.headers.get("last-event-id")
    if last_id is None:
        # The page loads its current state through GET requests. Replaying the
        # whole job history here causes thousands of redundant UI refreshes.
        cursor = db.scalar(select(func.max(Event.id)).where(Event.job_id == str(job_id))) or 0
    else:
        try:
            cursor = int(last_id)
            if not 0 <= cursor <= 2**63 - 1:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "Invalid Last-Event-ID") from None
    db.close()  # Do not hold a pooled connection for the lifetime of an SSE stream.

    async def stream():
        nonlocal cursor
        # Establish a reconnect cursor even when this job has no new events.
        # Refresh once to cover changes between loading the page and connecting.
        yield f"id: {cursor}\nevent: ready\ndata: {{}}\n\n"
        while not await request.is_disconnected():
            with session() as event_db:
                rows = event_db.scalars(
                    select(Event)
                    .where(Event.job_id == str(job_id), Event.id > cursor)
                    .order_by(Event.id)
                    .limit(200)
                ).all()
            for row in rows:
                cursor = row.id
                yield f"id: {row.id}\nevent: {row.type}\ndata: {json.dumps(jsonable_encoder(row.data))}\n\n"
            if not rows:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


api.include_router(media_preview_router)
api.include_router(agent_activity_router)
api.include_router(agent_prompts_router)
api.include_router(subtitle_guidance_router)
app.include_router(api)
