"""Persist completed subtitle-agent requests and recover validated legacy responses."""

import hashlib
import json

from sqlalchemy import select

from shared.agent_prompts import saved_prompts, task_prompts
from shared.config import behavior
from shared.db import session
from shared.models import Event, Task
from shared.paths import contained, write_json
from shared.subtitle_discovery import (
    SearchResult,
    SubtitleCleanup,
    SubtitleReview,
    blocking_issues,
    covers,
    validate_cleanup_edits,
)
from shared.subtitle_guidance import task_guidance
from worker.adapters.subtitle_alignment import fit_alignment

PROMPTS = {
    "clean": "subtitle_cleanup.md",
    "align": "subtitle_alignment.md",
    "search": "subtitle_discovery.md",
}


def prompt_text(mode):
    return saved_prompts()[PROMPTS[mode].removesuffix(".md")]["text"]


def signature(inventory):
    # Exact input matching also preserves previous Chinese terminology choices.
    # Prompt/model changes invalidate old checkpoints instead of hiding new rules.
    options = behavior()["agent"]
    policy = options.get("subtitle_cleanup", {}) if inventory["mode"] == "clean" else options
    identity = {
        "version": 1,
        "inventory": {key: value for key, value in inventory.items() if key != "agent_prompt"},
        "prompt": inventory["agent_prompt"]["text"]
        if inventory.get("agent_prompt")
        else prompt_text(inventory["mode"]),
        "model": policy.get("model", options.get("model")),
        "effort": policy.get("reasoning_effort", options.get("reasoning_effort")),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validated_answer(inventory, answer):
    if inventory["mode"] == "search":
        review = SearchResult.model_validate(answer["decision"])
    elif inventory["mode"] == "clean":
        review = SubtitleCleanup.model_validate(answer["decision"])
        if not review.single_language or not covers(review.language, inventory["requested_language"]):
            raise ValueError("Checkpoint has the wrong subtitle language")
        validate_cleanup_edits(review, inventory["cues"])
    else:
        review = SubtitleReview.model_validate(answer["decision"])
        if not covers(review.language, inventory["candidate"]["language"]):
            raise ValueError("Checkpoint alignment has the wrong subtitle language")
        fit_alignment(
            review.model_copy(update={"usable": True}),
            inventory["candidate_cues"],
            inventory["reference_cues"],
            inventory["duration_seconds"],
        )
    return {**answer, "decision": review.model_dump()}


def checkpoint_path(ctx, key):
    return contained(ctx.workspace, f"subtitle-review-checkpoints/{key}.json")


def save(ctx, inventory, answer, *, source_task_id=None):
    answer = validated_answer(inventory, answer)
    if inventory["mode"] != "search" and blocking_issues(
        SubtitleCleanup.model_validate(answer["decision"])
        if inventory["mode"] == "clean"
        else SubtitleReview.model_validate(answer["decision"])
    ):
        return answer  # Keep blockers available to the repair pass, never as reusable completed work.
    key = signature(inventory)
    ctx.check()
    write_json(
        checkpoint_path(ctx, key),
        {
            "signature": key,
            "answer": answer,
            "source_task_id": source_task_id or ctx.task_id,
        },
    )
    return answer


def load(ctx, inventory):
    key = signature(inventory)
    path = checkpoint_path(ctx, key)
    try:
        record = json.loads(path.read_text())
        if record.get("signature") != key:
            return None
        answer = validated_answer(inventory, record["answer"])
        if any(
            issue.lstrip().upper().startswith("BLOCKING:") for issue in answer["decision"].get("issues", [])
        ):
            return None
        return answer
    except (OSError, ValueError, KeyError, TypeError):
        return None  # A partial/corrupt checkpoint is never treated as completed work.


def recover_previous_attempts(ctx):
    """Only replay completed, structured responses from this task's retry ancestry."""
    if getattr(ctx, "_subtitle_resume_seeded", False):
        return
    ctx._subtitle_resume_seeded = True
    guidance = task_guidance(ctx)
    preserve = bool(guidance and not guidance[-1]["recheck_completed"])
    with session() as db:
        current = db.get(Task, ctx.task_id)
        ancestors, seen = [], {ctx.task_id}
        while current and current.retry_of and current.retry_of not in seen:
            current = db.get(Task, current.retry_of)
            if not current or current.job_id != ctx.job.id:
                break
            seen.add(current.id)
            ancestors.append(current.id)
        if not current:
            return
        suffix = f"-{ctx.task_id}-{guidance[-1]['id']}" if guidance else ""
        marker = contained(ctx.workspace, f"subtitle-review-checkpoints/recovered-{current.id}{suffix}.json")
        if marker.exists():
            return
        if not ancestors:
            write_json(marker, {"attempts": []})
            return
        rows = db.scalars(
            select(Event)
            .where(
                Event.job_id == ctx.job.id,
                Event.type == "agent_output",
                Event.data["task_id"].as_string().in_(ancestors),
                Event.data["type"].as_string().in_(["prompt", "message", "complete", "error"]),
            )
            .order_by(Event.id)
        ).all()
    pending = {}
    recovered = 0
    for row in rows:
        ctx.check()
        value = row.data
        key = (value.get("task_id"), value.get("invocation_id"))
        if not key[1]:
            continue
        try:
            if value["type"] == "prompt":
                prefix, separator, evidence = value["text"].partition("\nUntrusted evidence:\n")
                if not separator:
                    continue
                inventory, _ = json.JSONDecoder().raw_decode(evidence)
                prefix = prefix.partition("\nUser continuation instructions")[0]
                if inventory.get("mode") not in PROMPTS or (
                    not preserve
                    and prefix != task_prompts(ctx)[PROMPTS[inventory["mode"]].removesuffix(".md")]["text"]
                ):
                    continue
                if preserve:
                    inventory.pop("review_guidance", None)
                inventory["agent_prompt"] = task_prompts(ctx)[PROMPTS[inventory["mode"]].removesuffix(".md")]
                pending[key] = {"inventory": inventory}
            elif value["type"] == "error":
                pending.pop(key, None)
            elif value["type"] == "message" and key in pending:
                decision = json.loads(value["text"])
                answer = validated_answer(pending[key]["inventory"], {"decision": decision})
                pending[key]["answer"] = answer
            elif value["type"] == "complete" and key in pending:
                item = pending.pop(key)
                if "answer" in item:
                    save(ctx, item["inventory"], item["answer"], source_task_id=key[0])
                    recovered += 1
        except (ValueError, KeyError, TypeError):
            continue  # Commentary, malformed responses and obsolete prompts cannot become checkpoints.
    write_json(marker, {"attempts": ancestors, "recovered": recovered})
    if recovered:
        ctx.log(f"Recovered {recovered} completed subtitle review responses from earlier attempts.")
