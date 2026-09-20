import json
import queue
import secrets
import threading
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.screenshot_agent import CodexScreenshotSelector
from agent.subtitle_agent import CodexSubtitleClassifier
from agent.track_agent import CodexTrackReviewer
from shared.config import get_settings
from shared.paths import contained, job_dir

app = FastAPI(title="Screenshot and track review service")
lock = threading.Lock()


class Request(BaseModel):
    job_id: UUID
    task_id: UUID


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/select")
def select(request: Request, authorization: str = Header(default=""), accept: str = Header(default="")):
    settings = get_settings()
    if not settings.agent_token or not secrets.compare_digest(
        authorization, f"Bearer {settings.agent_token}"
    ):
        raise HTTPException(401, "Invalid internal service credential")
    root = job_dir(job_dir(settings.cache_root / "agent", str(request.job_id)), str(request.task_id))
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Agent is busy; retry this task")
    if "application/x-ndjson" in accept:
        return selection_stream(root)
    try:
        return CodexScreenshotSelector().select(root)
    except (RuntimeError, ValueError, TimeoutError) as error:
        raise HTTPException(502, str(error)) from error
    finally:
        lock.release()


class SubtitleRequest(Request):
    track_id: int = Field(ge=0, strict=True)


@app.post("/classify-subtitles")
def classify_subtitles(request: SubtitleRequest, authorization: str = Header(default="")):
    settings = get_settings()
    if not settings.agent_token or not secrets.compare_digest(
        authorization, f"Bearer {settings.agent_token}"
    ):
        raise HTTPException(401, "Invalid internal service credential")
    root = contained(
        job_dir(job_dir(settings.cache_root / "agent", str(request.job_id)), str(request.task_id)),
        f"subtitle-{request.track_id}",
    )
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Agent is busy; retry this task")
    return selection_stream(root, subtitle=True)


@app.post("/review-tracks")
def review_tracks(request: Request, authorization: str = Header(default="")):
    settings = get_settings()
    if not settings.agent_token or not secrets.compare_digest(
        authorization, f"Bearer {settings.agent_token}"
    ):
        raise HTTPException(401, "Invalid internal service credential")
    root = contained(
        job_dir(job_dir(settings.cache_root / "agent", str(request.job_id)), str(request.task_id)), "tracks"
    )
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Agent is busy; retry this task")
    return selection_stream(root, tracks=True)


def selection_stream(root, subtitle=False, tracks=False):
    messages = queue.Queue(maxsize=128)
    cancelled = threading.Event()

    def check():
        if cancelled.is_set():
            raise RuntimeError("Agent request cancelled")

    def emit(event):
        while not cancelled.is_set():
            try:
                messages.put(event, timeout=0.2)
                return
            except queue.Full:
                continue
        check()

    def work():
        try:
            result = (
                CodexTrackReviewer(on_event=emit, check=check).review(root)
                if tracks
                else CodexSubtitleClassifier(on_event=emit, check=check).classify(root)
                if subtitle
                else CodexScreenshotSelector(on_event=emit, check=check).select(root)
            )
            emit({"type": "result", "result": result})
        except Exception as error:
            if not cancelled.is_set():
                emit({"type": "error", "text": str(error)})
        finally:
            lock.release()

    async def stream():
        # Start inside the iterator so its finally block always owns cancellation.
        import asyncio

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        try:
            while True:
                try:
                    message = messages.get_nowait()
                except queue.Empty:
                    yield json.dumps({"type": "heartbeat"}) + "\n"
                    await asyncio.sleep(0.5)
                    continue
                yield json.dumps(message) + "\n"
                if message["type"] == "result" or (
                    message["type"] == "error" and "invocation_id" not in message
                ):
                    return
        finally:
            cancelled.set()

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"})
