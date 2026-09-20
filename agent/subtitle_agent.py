"""Visual fallback for content-based Chinese variant and SDH classification."""

import json
from pathlib import Path
from uuid import uuid4

from agent.codex_stream import invoke
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
        invocation_id = str(uuid4())

        def emit(event):
            self.check()
            self.on_event(
                {**event, "invocation_id": invocation_id, "stage": f"Subtitle track {inventory['track_id']}"}
            )

        emit({"type": "prompt", "text": prompt, "images": [p.name for p in images]})
        try:
            text, thread = invoke(prompt, images, SubtitleDecision.model_json_schema(), emit, self.check)
            decision = SubtitleDecision.model_validate_json(text)
            validate_evidence(decision, {s["id"] for s in inventory["samples"]})
            emit({"type": "complete", "text": "Subtitle review received"})
            return {"decision": decision.model_dump(), "thread_id": thread}
        except Exception as error:
            emit({"type": "error", "text": str(error)})
            raise
