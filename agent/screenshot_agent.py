import json
import math
import time
from pathlib import Path
from uuid import uuid4

from agent.codex_stream import invoke
from agent.schemas import Selection, review_policy, validate_selection
from shared.agent_prompts import prompt_events, request_prompt
from shared.config import ScreenshotPolicy, behavior, screenshot_selection_limits
from shared.paths import contained


class CodexScreenshotSelector:
    """Screenshot selection with streamed Codex app-server responses."""

    def __init__(self, on_event=None, check=None):
        self.on_event = on_event or (lambda event: None)
        self.check = check or (lambda: None)
        self.stage = "shortlist"

    def _invoke(self, prompt, images):
        invocation_id = str(uuid4())

        def emit(event):
            self.check()
            self.on_event({**event, "invocation_id": invocation_id, "stage": self.stage})

        emit({"type": "prompt", "text": prompt, "images": [image.name for image in images]})
        try:
            text, thread = invoke(
                prompt,
                images,
                Selection.model_json_schema(),
                emit,
                self.check,
                timeout_seconds=screenshot_selection_limits()["request_timeout_seconds"],
            )
            selection = Selection.model_validate_json(text)
            emit({"type": "complete", "text": "Response received"})
            return selection, thread
        except Exception as error:
            emit({"type": "error", "text": str(error)})
            raise

    def select(self, root: Path):
        inventory = json.loads(contained(root, "inventory.json", exists=True).read_text())
        original_check = self.check
        deadline = time.monotonic() + min(
            screenshot_selection_limits()["max_seconds"],
            inventory.get("remaining_seconds", screenshot_selection_limits()["max_seconds"]),
        )

        def check():
            original_check()
            if time.monotonic() >= deadline:
                raise TimeoutError("Screenshot selection reached its configured time limit")

        self.check = check
        candidates = inventory["candidates"]
        policy = ScreenshotPolicy.model_validate(inventory["policy"])
        selected_prompt = request_prompt(inventory, "screenshot_selection")
        self.on_event = prompt_events(self.on_event, selected_prompt)
        prompt = selected_prompt["text"]
        base = prompt + "\nPolicy:\n" + policy.model_dump_json() + "\nInventory:\n" + json.dumps(candidates)
        sheets = [contained(root, p, exists=True) for p in inventory["contact_sheets"]]
        runs = []
        shortlisted = Selection(selected=inventory.get("shortlisted_choices", [])).selected
        reviewed = set(inventory.get("reviewed_candidate_ids", []))
        per_sheet = max(6, math.ceil(min(40, len(candidates)) / max(1, len(sheets))))
        # Bound image count and context size: two contact sheets per shortlist call.
        for index in range(0, 0 if inventory.get("shortlisted_ids") else len(sheets), 2):
            group = sheets[index : index + 2]
            visible = [
                c
                for c in candidates
                if c["sheet"] in inventory["contact_sheets"][index : index + 2]
                and c["candidate_id"] not in reviewed
            ]
            if not visible:
                continue
            decision, thread = self._invoke(
                base + f"\nShortlist up to {per_sheet} strong frames from EACH attached sheet. "
                "Do not fill the final set yet. Never pad the shortlist with poor frames. "
                + "Only review these unreviewed IDs: "
                + json.dumps([c["candidate_id"] for c in visible]),
                group,
            )
            allowed = {c["candidate_id"] for c in visible}
            if any(c.candidate_id not in allowed for c in decision.selected):
                raise ValueError("Codex returned IDs absent from the attached contact sheets")
            reviewed.update(allowed)
            shortlisted.extend(decision.selected)
            runs.append({"stage": "shortlist", "thread_id": thread, "output": decision.model_dump()})
        deduplicated = {c.candidate_id: c for c in shortlisted}
        # Keep at most 40 images while spreading them through the movie's timeline.
        ordered = sorted(
            deduplicated.values(),
            key=lambda c: candidates_by_id(candidates)[c.candidate_id]["timeline_seconds"],
        )
        if len(ordered) > 40:
            ordered = [ordered[round(i * (len(ordered) - 1) / 39)] for i in range(40)]
        short_ids = set(inventory.get("shortlisted_ids") or [c.candidate_id for c in ordered])
        if len(short_ids) > 40:
            raise ValueError("Screenshot shortlist cannot exceed 40 frames")
        short = [c for c in candidates if c["candidate_id"] in short_ids]
        if len(short) < policy.best_count and not inventory.get("allow_partial", True):
            return {
                "selected": [],
                "shortlisted_choices": [c.model_dump() for c in ordered],
                "shortlisted_ids": sorted(short_ids),
                "reviewed_candidate_ids": sorted(reviewed),
                "needs_more_candidates": True,
                "runs": runs,
            }
        if len(short) < 2:
            raise ValueError(
                "Too few suitable frames after sampling; use local selection or increase the sampling limit"
            )
        policy = review_policy(policy, len(short))
        images = [contained(root, c["agent_image"], exists=True) for c in short]
        final_prompt = (
            base + "\nChoose the BEST review options for the user, ranked strongest first. "
            f"Return exactly {policy.count} choices, following this review policy instead of the "
            "original final-count policy: "
            + policy.model_dump_json()
            + "\nThe user will later choose 1–15 final pairs and may edit the best list from the shortlist. "
            "These are recommendations, not final exports. "
            "Choose only from these higher-quality image IDs in attachment order: "
        )
        final_prompt += json.dumps([c["candidate_id"] for c in short])
        errors = []
        self.stage = "review"
        for attempt in range(behavior()["agent"]["selection_attempts"]):
            decision, thread = self._invoke(
                final_prompt + "\nRequired corrections: " + json.dumps(errors), images
            )
            errors = validate_selection(decision, short, policy, inventory["duration"])
            runs.append(
                {
                    "stage": "review",
                    "attempt": attempt + 1,
                    "thread_id": thread,
                    "output": decision.model_dump(),
                    "errors": errors,
                }
            )
            if not errors:
                return {
                    "selected": decision.model_dump()["selected"],
                    "needs_more_candidates": len(decision.selected)
                    < inventory["policy"].get("best_count", 30),
                    "reviewed_candidate_ids": sorted(reviewed),
                    "shortlisted_choices": [c.model_dump() for c in ordered if c.candidate_id in short_ids],
                    "shortlisted_ids": sorted(short_ids),
                    "runs": runs,
                    "thread_id": thread,
                }
        if not inventory.get("allow_partial", True):
            return {
                "selected": [],
                "shortlisted_choices": [c.model_dump() for c in ordered],
                "shortlisted_ids": sorted(short_ids),
                "reviewed_candidate_ids": sorted(reviewed),
                "needs_more_candidates": True,
                "runs": runs,
                "errors": errors,
            }
        raise ValueError("Codex selection failed deterministic diversity checks: " + "; ".join(errors))


def candidates_by_id(candidates):
    return {c["candidate_id"]: c for c in candidates}
