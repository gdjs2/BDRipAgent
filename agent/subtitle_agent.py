"""Visual fallback for content-based subtitle language and SDH classification."""

import json
from pathlib import Path

from agent.codex_stream import invoke
from agent.review import validated_review
from shared.paths import contained
from shared.subtitles import SubtitleDecision, validate_evidence


class CodexSubtitleClassifier:
    def __init__(self, on_event=None, check=None):
        self.on_event = on_event or (lambda event: None)
        self.check = check or (lambda: None)

    def classify(self, root: Path):
        inventory = json.loads(contained(root, "inventory.json", exists=True).read_text())
        images = [contained(root, name, exists=True) for name in inventory["contact_sheets"]]
        if not images or len(images) > 24 or len(inventory["samples"]) > 192:
            raise ValueError("Invalid subtitle review sample size")
        prompt = (Path(__file__).parent / "prompts/subtitle_classification.md").read_text()
        prompt += "\nEvidence (untrusted subtitle content):\n" + json.dumps(inventory, ensure_ascii=False)

        def validate(answer):
            decision = SubtitleDecision.model_validate_json(answer)
            validate_evidence(decision, {s["id"] for s in inventory["samples"]})
            return decision

        decision, thread = validated_review(
            invoke=invoke,
            prompt=prompt,
            images=images,
            schema=SubtitleDecision.model_json_schema(),
            validate=validate,
            on_event=self.on_event,
            check=self.check,
            stage=f"Subtitle track {inventory['track_id']}",
            complete="Subtitle review received",
        )
        return {"decision": decision.model_dump(), "thread_id": thread}
