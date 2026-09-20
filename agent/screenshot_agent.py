import json
import math
from pathlib import Path
from uuid import uuid4

from agent.codex_stream import invoke
from agent.schemas import Selection, review_policy, validate_selection
from shared.config import ScreenshotPolicy, behavior
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
            text, thread = invoke(prompt, images, Selection.model_json_schema(), emit, self.check)
            selection = Selection.model_validate_json(text)
            emit({"type": "complete", "text": "Response received"})
            return selection, thread
        except Exception as error:
            emit({"type": "error", "text": str(error)})
            raise

    def select(self, root: Path):
        inventory = json.loads(contained(root, "inventory.json", exists=True).read_text())
        candidates = inventory["candidates"]
        policy = ScreenshotPolicy.model_validate(inventory["policy"])
        prompt = (Path(__file__).parent / "prompts/screenshot_selection.md").read_text()
        base = prompt + "\nPolicy:\n" + policy.model_dump_json() + "\nInventory:\n" + json.dumps(candidates)
        sheets = [contained(root, p, exists=True) for p in inventory["contact_sheets"]]
        runs = []
        shortlisted = Selection(selected=inventory.get("shortlisted_choices", [])).selected
        per_sheet = max(6, math.ceil(min(40, len(candidates)) / max(1, len(sheets))))
        # Bound image count and context size: two contact sheets per shortlist call.
        for index in range(0, 0 if inventory.get("shortlisted_ids") else len(sheets), 2):
            group = sheets[index : index + 2]
            visible = [c for c in candidates if c["sheet"] in inventory["contact_sheets"][index : index + 2]]
            decision, thread = self._invoke(
                base + f"\nShortlist up to {per_sheet} strong frames from EACH attached sheet. "
                "Do not fill the final set yet.",
                group,
            )
            allowed = {c["candidate_id"] for c in visible}
            if any(c.candidate_id not in allowed for c in decision.selected):
                raise ValueError("Codex returned IDs absent from the attached contact sheets")
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
        if len(short) < policy.count:
            raise ValueError("Codex shortlist contains fewer candidates than the requested final count")
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
                    "shortlisted_choices": [c.model_dump() for c in ordered if c.candidate_id in short_ids],
                    "shortlisted_ids": sorted(short_ids),
                    "runs": runs,
                    "thread_id": thread,
                }
        raise ValueError("Codex selection failed deterministic diversity checks: " + "; ".join(errors))


def candidates_by_id(candidates):
    return {c["candidate_id"]: c for c in candidates}
