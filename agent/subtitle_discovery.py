"""Web discovery and evidence-based alignment, separate from track classification."""

import json
from functools import partial
from pathlib import Path

from agent.codex_stream import invoke
from agent.review import validated_review
from shared.config import behavior
from shared.paths import contained
from shared.subtitle_discovery import SearchResult, SubtitleCleanup, SubtitleReview


class CodexSubtitleDiscovery:
    def __init__(self, on_event=None, check=None):
        self.on_event = on_event or (lambda event: None)
        self.check = check or (lambda: None)

    def run(self, root):
        inventory = json.loads(contained(root, "inventory.json", exists=True).read_text())
        search = inventory["mode"] == "search"
        cleaning = inventory["mode"] == "clean"
        model = SearchResult if search else SubtitleCleanup if cleaning else SubtitleReview
        name = (
            "subtitle_discovery.md"
            if search
            else "subtitle_cleanup.md"
            if cleaning
            else "subtitle_alignment.md"
        )
        prompt = (Path(__file__).parent / "prompts" / name).read_text()
        prompt += "\nUntrusted evidence:\n" + json.dumps(inventory, ensure_ascii=False)

        def validate(answer):
            result = model.model_validate_json(answer)
            if cleaning:
                cues = {c["id"]: c["text"] for c in inventory["cues"]}
                if len({e.cue_id for e in result.edits}) != len(result.edits) or any(
                    cues.get(e.cue_id) != e.original_text for e in result.edits
                ):
                    raise ValueError("Cleanup edits must cite exact supplied cue text and unique IDs")
            elif not search:
                candidates = {c["id"] for c in inventory["candidate_cues"]}
                references = {c["id"] for c in inventory["reference_cues"]}
                if any(
                    a.candidate_id not in candidates or a.reference_id not in references
                    for a in result.anchors
                ):
                    raise ValueError("Alignment cites a cue that was not supplied")
            return result

        cleanup_options = behavior()["agent"].get("subtitle_cleanup", {}) if cleaning else {}
        result, thread = validated_review(
            invoke=partial(
                invoke,
                web_search=search or cleaning,
                **{
                    key: cleanup_options[key]
                    for key in ("model", "reasoning_effort")
                    if cleanup_options.get(key)
                },
                status="Finding missing subtitles"
                if search
                else "Cleaning subtitles"
                if cleaning
                else "Aligning subtitles",
            ),
            prompt=prompt,
            images=[],
            schema=model.model_json_schema(),
            validate=validate,
            on_event=self.on_event,
            check=self.check,
            stage="Subtitle discovery"
            if search
            else "Subtitle cleanup"
            if cleaning
            else "Subtitle alignment and quality",
            complete="Subtitle search complete"
            if search
            else "Subtitle cleanup and repair complete"
            if cleaning
            else "Subtitle alignment review complete",
        )
        return {"decision": result.model_dump(), "thread_id": thread}
