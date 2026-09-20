import secrets
import threading
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from agent.screenshot_agent import CodexScreenshotSelector
from shared.config import get_settings
from shared.paths import job_dir

app = FastAPI(title="Source screenshot selection service")
lock = threading.Lock()


class Request(BaseModel):
    job_id: UUID
    task_id: UUID


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/select")
def select(request: Request, authorization: str = Header(default="")):
    settings = get_settings()
    if not settings.agent_token or not secrets.compare_digest(
        authorization, f"Bearer {settings.agent_token}"
    ):
        raise HTTPException(401, "Invalid internal service credential")
    root = job_dir(job_dir(settings.cache_root / "agent", str(request.job_id)), str(request.task_id))
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Agent is busy; retry this task")
    try:
        return CodexScreenshotSelector().select(root)
    except (RuntimeError, ValueError, TimeoutError) as error:
        raise HTTPException(502, str(error)) from error
    finally:
        lock.release()
