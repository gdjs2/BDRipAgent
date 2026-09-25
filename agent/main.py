import json
import queue
import secrets
import threading
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.request_queue import AgentQueue
from agent.screenshot_agent import CodexScreenshotSelector
from agent.subtitle_agent import CodexSubtitleClassifier
from agent.subtitle_discovery import CodexSubtitleDiscovery
from agent.track_agent import CodexTrackReviewer
from shared.config import behavior, get_settings, screenshot_selection_limits
from shared.paths import contained, job_dir

app = FastAPI(title="Screenshot and track review service")
agent_queue = AgentQueue()


class Request(BaseModel):
    job_id: UUID
    task_id: UUID


@app.get("/health")
def health():
    return {"status": "ok", "queue": agent_queue.snapshot()}


@app.post("/select")
def select(request: Request, authorization: str = Header(default=""), accept: str = Header(default="")):
    settings = get_settings()
    if not settings.agent_token or not secrets.compare_digest(
        authorization, f"Bearer {settings.agent_token}"
    ):
        raise HTTPException(401, "Invalid internal service credential")
    root = job_dir(job_dir(settings.cache_root / "agent", str(request.job_id)), str(request.task_id))
    if "application/x-ndjson" in accept:
        return selection_stream(root)
    try:
        with agent_queue.slot(agent_queue.enqueue(), timeout=screenshot_selection_limits()["max_seconds"]):
            return CodexScreenshotSelector().select(root)
    except (RuntimeError, ValueError, TimeoutError) as error:
        raise HTTPException(502, str(error)) from error


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
    return selection_stream(root, tracks=True)


@app.post("/discover-subtitles")
def discover_subtitles(request: Request, authorization: str = Header(default="")):
    settings = get_settings()
    if not settings.agent_token or not secrets.compare_digest(
        authorization, f"Bearer {settings.agent_token}"
    ):
        raise HTTPException(401, "Invalid internal service credential")
    root = contained(
        job_dir(job_dir(settings.cache_root / "agent", str(request.job_id)), str(request.task_id)),
        "discovery",
    )
    return selection_stream(root, discovery=True)


def queue_timeout():
    return max(1, float(behavior()["agent"].get("busy_timeout_seconds", 1800)))


def selection_stream(root, subtitle=False, tracks=False, discovery=False):
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

    def position_changed(position):
        emit(
            {
                "type": "status",
                "stage": "Agent queue",
                "agent_queue_state": "waiting" if position else "running",
                "queue_position": position,
                "text": f"Waiting for agent · queue position {position}"
                if position
                else "Agent processing started",
            }
        )

    def work(ticket):
        try:
            with agent_queue.slot(
                ticket,
                timeout=queue_timeout()
                if subtitle or tracks or discovery
                else screenshot_selection_limits()["max_seconds"],
                check=check,
                on_position=position_changed,
            ):
                result = (
                    CodexSubtitleDiscovery(on_event=emit, check=check).run(root)
                    if discovery
                    else CodexTrackReviewer(on_event=emit, check=check).review(root)
                    if tracks
                    else CodexSubtitleClassifier(on_event=emit, check=check).classify(root)
                    if subtitle
                    else CodexScreenshotSelector(on_event=emit, check=check).select(root)
                )
                emit({"type": "result", "result": result})
        except Exception as error:
            if not cancelled.is_set():
                emit({"type": "error", "text": str(error)})

    async def stream():
        # Start inside the iterator so its finally block always owns cancellation.
        import asyncio

        ticket = agent_queue.enqueue()
        thread = threading.Thread(target=work, args=(ticket,), daemon=True)
        try:
            thread.start()
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
            # The worker owns an active slot until its subprocess has stopped.
            if thread.ident is None:
                agent_queue.discard(ticket)

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"})
