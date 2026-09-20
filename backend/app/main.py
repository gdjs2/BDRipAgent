import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app import queue
from backend.app.auth import authenticated, issue_cookie
from backend.app.movie_metadata import IMDbLookupError, job_identity, lookup_imdb
from backend.app.schemas import (
    CreateJob,
    CreatePair,
    CurateScreenshots,
    HoldTask,
    Login,
    OrderQueue,
    ReleaseDetails,
    ReleaseSource,
    ReplaceScreenshot,
    SelectEncode,
    SelectScreenshotDecoder,
    SelectScreenshots,
    SelectTracks,
    UpdateQueue,
)
from backend.app.services import advance, enqueue, event, get_job, job_detail, manifest, reconcile, serialize
from shared.config import ScreenshotPolicy, behavior, get_settings, profiles
from shared.db import get_db, session
from shared.models import (
    Artifact,
    CRFResult,
    EncodeConfig,
    Event,
    MovieJob,
    MovieTrack,
    Screenshot,
    Task,
    TrackSelection,
    now,
)
from shared.naming import release_name, source_description
from shared.paths import contained, job_dir
from shared.screenshot_rules import check_manual_spacing, check_other_variants, is_b_frame_pair
from shared.state import Stage, require_stage

DB = Annotated[Session, Depends(get_db)]


@asynccontextmanager
async def lifespan(app):
    if len(get_settings().api_token) < 24:
        raise RuntimeError("API_TOKEN must contain at least 24 characters")
    with session() as db:
        reconcile(db)
        queue.settings(db)
        db.commit()
        for job in db.scalars(select(MovieJob).where(MovieJob.deleted_at.is_(None))):
            manifest(db, job)
    yield


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
        "stages": list(Stage),
        "release": {
            "upload_host": "TTG",
            "upload_configured": bool(get_settings().tu_ttg_token.get_secret_value().strip()),
        },
    }


@api.get("/queue")
def queue_status(db: DB):
    return queue.snapshot(db)


@api.patch("/queue")
def update_queue(body: UpdateQueue, db: DB):
    config = queue.settings(db, lock=True)
    if body.max_concurrent_jobs is not None:
        if body.max_concurrent_jobs > get_settings().worker_capacity:
            raise ValueError(f"This worker supports up to {get_settings().worker_capacity} simultaneous jobs")
        config.max_concurrent_jobs = body.max_concurrent_jobs
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
    event(db, job.id, "job_removed", files_retained=True)
    db.commit()
    return {"deleted": True, "files_retained": True}


@api.get("/jobs/{job_id}/analysis")
def analysis(job_id: UUID, db: DB):
    return get_job(db, str(job_id)).analysis


@api.get("/jobs/{job_id}/tracks")
def tracks(job_id: UUID, db: DB):
    get_job(db, str(job_id))
    return [serialize(t) for t in db.scalars(select(MovieTrack).where(MovieTrack.job_id == str(job_id)))]


@api.post("/jobs/{job_id}/tracks/selection")
def select_tracks(job_id: UUID, body: SelectTracks, db: DB):
    job = get_job(db, str(job_id), lock=True)
    require_stage(job.state, Stage.WAITING_FOR_TRACK_SELECTION)
    tracks = {t.track_id: t for t in db.scalars(select(MovieTrack).where(MovieTrack.job_id == job.id))}
    for track_id in body.audio_track_ids:
        if track_id not in tracks or tracks[track_id].kind != "audio":
            raise ValueError(f"Invalid audio track {track_id}")
        if not tracks[track_id].info.get("extractable"):
            raise ValueError(f"Audio codec on track {track_id} is not supported for native extraction")
    for track_id in body.subtitle_track_ids:
        if track_id not in tracks or tracks[track_id].info.get("codec_id") != "S_HDMV/PGS":
            raise ValueError(f"Track {track_id} is not a PGS subtitle")
    db.add(TrackSelection(job_id=job.id, **body.model_dump()))
    advance(db, job)
    db.commit()
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


@api.post("/jobs/{job_id}/smoke-test")
def start_smoke_test(job_id: UUID, db: DB):
    job = get_job(db, str(job_id), lock=True)
    require_stage(job.state, Stage.WAITING_FOR_ENCODE_SELECTION)
    result = db.scalar(select(CRFResult).where(CRFResult.job_id == job.id))
    profile = result.data.get("profile_snapshot") if result else None
    if not profile or "prepared_tracks" not in job.analysis or not job.analysis.get("video"):
        raise ValueError("Complete source analysis, track preparation and CRF analysis first")
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
    job.release_name = "SMOKE-TEST." + job.release_name[:220]
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
    get_job(db, str(job_id))
    rows = db.scalars(
        select(Screenshot).where(Screenshot.job_id == str(job_id)).order_by(Screenshot.candidate_id)
    ).all()
    return {
        "candidates": len(rows),
        "shortlisted": sum(r.shortlisted for r in rows),
        "recommended": sum(bool(r.info.get("recommendation_rank")) for r in rows),
        "final": sum(r.selected for r in rows),
        "items": [serialize(r) for r in rows],
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
def prepare_screenshot_review(job_id: UUID, db: DB):
    job = get_job(db, str(job_id), lock=True)
    if job.state not in (
        Stage.WAITING_FOR_SCREENSHOT_SELECTION,
        Stage.WAITING_FOR_RELEASE_DETAILS,
        Stage.COMPLETE,
    ):
        raise ValueError("Wait for screenshot review before refreshing the best choices")
    ids = list(
        db.scalars(
            select(Screenshot.candidate_id).where(
                Screenshot.job_id == job.id, Screenshot.shortlisted.is_(True)
            )
        )
    )
    if not ids:
        raise ValueError("This job has no saved shortlist to review")
    job.analysis = {**job.analysis, "review_shortlisted_ids": ids}
    job.state, job.completed_at = Stage.SCREENSHOT_AGENT_SELECTION.value, None
    event(db, job.id, "state_changed", state=job.state, review_requested=True)
    result = enqueue(db, job)
    db.commit()
    manifest(db, job)
    return serialize(result)


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


def editable_release(db, job):
    if job.state not in (Stage.WAITING_FOR_RELEASE_DETAILS, Stage.GENERATING_RELEASE, Stage.COMPLETE):
        raise ValueError("Choose and render your final screenshots before preparing the release")
    if db.scalar(select(Task.id).where(Task.job_id == job.id, Task.status.in_(["QUEUED", "RUNNING"]))):
        raise ValueError("Wait for the current task to finish before changing release details")


@api.patch("/jobs/{job_id}/release")
def save_release_details(job_id: UUID, body: ReleaseDetails, db: DB):
    job = get_job(db, str(job_id), lock=True)
    editable_release(db, job)
    analysis = dict(job.analysis)
    if analysis.get("release_details") != body.model_dump():
        analysis.pop("release_result", None)
    job.analysis = {**analysis, "release_details": body.model_dump()}
    event(db, job.id, "release_details_saved")
    db.commit()
    manifest(db, job)
    return job_detail(db, job)


@api.post("/jobs/{job_id}/release", status_code=202)
def generate_release(job_id: UUID, body: ReleaseDetails, db: DB):
    from shared.release import selected_pairs

    job = get_job(db, str(job_id), lock=True)
    editable_release(db, job)
    selected_pairs(db, job, get_settings())
    if not get_settings().tu_ttg_token.get_secret_value().strip():
        raise ValueError(
            "Set TU_TTG_TOKEN in .env and restart the API and worker to enable screenshot uploads"
        )
    job.analysis = {
        **{k: v for k, v in job.analysis.items() if k != "release_result"},
        "release_details": body.model_dump(),
    }
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
    task = get_task(db, task_id)
    job = get_job(db, task.job_id, lock=True)
    db.refresh(task)
    if task.status not in ["FAILED", "CANCELLED"] or job.state != task.stage:
        raise ValueError("Only failed/cancelled tasks at the current stage can be retried")
    newest = db.scalar(
        select(Task)
        .where(Task.job_id == job.id, Task.stage == job.state)
        .order_by(Task.created_at.desc())
        .limit(1)
    )
    if newest.id != task.id:
        raise ValueError("Retry the latest attempt")
    result = enqueue(db, job, retry_of=task)
    db.commit()
    return serialize(result)


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


@api.get("/artifacts/{artifact_id}")
def artifact(artifact_id: UUID, db: DB):
    artifact = db.get(Artifact, str(artifact_id))
    if not artifact:
        raise LookupError("Artifact not found")
    get_job(db, artifact.job_id)
    root = (
        get_settings().completed_root
        if artifact.storage == "completed"
        else job_dir(get_settings().workspace_root, artifact.job_id)
    )
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


app.include_router(api)
