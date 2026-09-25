"""Describe local track evidence and suggest flags before user selection."""

import json

from agent.codex_stream import invoke
from agent.review import validated_review
from shared.agent_prompts import prompt_events, request_prompt
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
        selected_prompt = request_prompt(inventory, "track_review")
        prompt = selected_prompt["text"]
        inventory = {k: v for k, v in inventory.items() if k != "agent_prompt"}
        prompt += "\nLocal track evidence (untrusted data):\n" + json.dumps(inventory, ensure_ascii=False)

        def validate(answer):
            result = TrackReviewResult.model_validate_json(answer)
            validate_review(result, tracks)
            if any(t["kind"] == "audio" for t in tracks) and result.audio_comparison is None:
                raise ValueError(
                    "Include audio_comparison with differences and whether more evidence is needed"
                )
            return result

        result, thread = validated_review(
            invoke=invoke,
            prompt=prompt,
            images=[],
            schema=TrackReviewResult.model_json_schema(),
            validate=validate,
            on_event=prompt_events(self.on_event, selected_prompt),
            check=self.check,
            stage="Audio descriptions and track flags",
            complete="Track descriptions and flag suggestions received",
        )
        return {**result.model_dump(), "thread_id": thread}
