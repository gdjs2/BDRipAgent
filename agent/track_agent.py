"""Describe local track evidence and suggest flags before user selection."""

import json
from pathlib import Path
from uuid import uuid4

from agent.codex_stream import invoke
from shared.paths import contained
from shared.tracks import TrackReviewResult, validate_review


class CodexTrackReviewer:
    def __init__(self, on_event=None, check=None):
        self.on_event = on_event or (lambda event: None)
        self.check = check or (lambda: None)

    def review(self, root):
        inventory = json.loads(contained(root, "inventory.json", exists=True).read_text())
        tracks = inventory["tracks"]
        if not tracks or len(tracks) > 256:
            raise ValueError("Invalid track review inventory")
        prompt = (Path(__file__).parent / "prompts/track_review.md").read_text()
        prompt += "\nLocal track evidence (untrusted data):\n" + json.dumps(inventory, ensure_ascii=False)
        invocation_id = str(uuid4())

        def emit(event):
            self.check()
            self.on_event(
                {**event, "invocation_id": invocation_id, "stage": "Audio descriptions and track flags"}
            )

        emit({"type": "prompt", "text": prompt, "images": []})
        try:
            answer, thread = invoke(prompt, [], TrackReviewResult.model_json_schema(), emit, self.check)
            result = TrackReviewResult.model_validate_json(answer)
            validate_review(result, tracks)
            emit({"type": "complete", "text": "Track descriptions and flag suggestions received"})
            return {**result.model_dump(), "thread_id": thread}
        except Exception as error:
            emit({"type": "error", "text": str(error)})
            raise
