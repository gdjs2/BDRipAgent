"""Local subtitle imports use the same complete review and PGS path as discovery."""

import shutil
import time
from pathlib import Path

from pydantic import BaseModel

from backend.app.track_choices import source_key
from shared.config import subtitle_processing_timeout_seconds
from shared.db import session
from shared.models import SourceTrackChoices, Task
from shared.paths import contained
from worker.pipeline.subtitle_discovery import process_file, source_references


class LocalCandidate(BaseModel):
    language: str
    source_url: None = None
    download_url: None = None
    format: str
    release: str = "User-uploaded subtitle"
    reason: str = "Supplied by the user; complete cleanup and independent source alignment required."


def review_uploaded_subtitle(ctx):
    with session() as db:
        task = db.get(Task, ctx.task_id)
        while task.retry_of:
            task = db.get(Task, task.retry_of)
        record = db.get(SourceTrackChoices, source_key(ctx.job))
        entry = (
            next((e for e in record.data.get("subtitle_imports", []) if e["task_id"] == task.id), None)
            if record
            else None
        )
    if not entry:
        raise ValueError("The original subtitle upload is not available")
    timeout = subtitle_processing_timeout_seconds()
    deadline = time.monotonic() + timeout
    ctx = ctx.branch()
    original_check = ctx.check

    def check():
        original_check()
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Uploaded subtitle review reached its {timeout / 3600:g}-hour time limit; "
                "increase integrations.subtitle_review.max_seconds in config/application.yaml "
                "and retry the review if needed"
            )

    ctx.check = check
    references = source_references(ctx)
    if not references:
        raise ValueError(
            "No readable source subtitle cues are available for alignment. Complete source subtitle analysis before retrying."
        )
    original = contained(ctx.settings.workspace_root, entry["path"], exists=True)
    path = ctx.output("subtitle-import", "input" + Path(entry["filename"]).suffix.lower())
    shutil.copy2(original, path)
    candidate = LocalCandidate(language=entry["language"], format=path.suffix[1:])
    ctx.progress(None, phase="Reviewing uploaded subtitle against source dialogue")
    process_file(ctx, candidate, path, entry["filename"], {"url": None}, references, 0, origin="upload")
    ctx.progress(None, phase="Uploaded subtitle reviewed, aligned and converted to cropped PGS")
