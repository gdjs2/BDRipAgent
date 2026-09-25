"""Editable agent instructions and immutable per-task snapshots."""

import hashlib

from shared.config import ROOT

CATALOG = {
    "subtitle_cleanup": (
        "Subtitle cleanup & repair",
        "Repair uploaded and downloaded subtitle text, remove ads, and normalize language and terminology.",
    ),
    "subtitle_alignment": (
        "Subtitle alignment",
        "Match subtitle cues to the source, including timing offsets and frame-rate differences.",
    ),
    "subtitle_discovery": (
        "Subtitle search",
        "Find and compare missing subtitles online and explain which release is the best match.",
    ),
    "subtitle_classification": (
        "Subtitle language & SDH",
        "Review PGS images and OCR to identify language, script, and accessibility cues.",
    ),
    "track_review": (
        "Audio comparison & track flags",
        "Compare local audio evidence and transcripts, describe tracks, and suggest flags.",
    ),
    "screenshot_selection": (
        "Screenshot selection",
        "Choose and rank screenshot candidates in agent mode. Local selection does not use an agent.",
    ),
}
MAX_PROMPT_LENGTH = 50000


def default_text(key):
    if key not in CATALOG:
        raise LookupError("Unknown agent prompt")
    return (ROOT / "agent/prompts" / f"{key}.md").read_text(encoding="utf-8")


def snapshot(key, row=None):
    text = row.text if row is not None and row.text is not None else default_text(key)
    return {
        "key": key,
        "text": text,
        "revision": row.revision if row is not None else 0,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def saved_prompts():
    from sqlalchemy import select

    from shared.db import session
    from shared.models import AgentPrompt

    with session() as db:
        rows = {row.key: row for row in db.scalars(select(AgentPrompt))}
        return {key: snapshot(key, rows.get(key)) for key in CATALOG}


def task_prompts(ctx):
    if getattr(ctx, "agent_prompts", None) is None:
        ctx.agent_prompts = saved_prompts()
    return ctx.agent_prompts


def request_prompt(inventory, key):
    """The agent has no database access; the worker supplies the saved snapshot."""
    value = inventory.get("agent_prompt")
    if value is None:
        return snapshot(key)  # Compatibility with requests from an older worker.
    if (
        value.get("key") != key
        or not isinstance(value.get("text"), str)
        or not value["text"].strip()
        or len(value["text"]) > MAX_PROMPT_LENGTH
        or hashlib.sha256(value["text"].encode()).hexdigest() != value.get("sha256")
    ):
        raise ValueError("Invalid agent prompt snapshot")
    return value


def prompt_events(emit, value):
    def send(event):
        if event.get("type") == "prompt":
            event = {
                **event,
                "prompt_key": value["key"],
                "prompt_revision": value["revision"],
                "prompt_sha256": value["sha256"],
            }
        emit(event)

    return send
