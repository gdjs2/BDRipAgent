"""Web discovery and evidence-based alignment, separate from track classification."""

import json
from functools import partial

from agent.codex_stream import invoke
from agent.review import validated_review
from shared.agent_prompts import prompt_events, request_prompt
from shared.config import behavior
from shared.paths import contained
from shared.subtitle_alignment import fit_alignment
from shared.subtitle_discovery import SearchResult, SubtitleCleanup, SubtitleReview, validate_cleanup_edits


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
        selected_prompt = request_prompt(inventory, name.removesuffix(".md"))
        prompt = selected_prompt["text"]
        guidance = inventory.get("review_guidance", {})
        if guidance.get("messages"):
            prompt += "\nUser continuation instructions (apply to this review):\n" + json.dumps(
                guidance["messages"], ensure_ascii=False
            )
        inventory = {k: v for k, v in inventory.items() if k != "agent_prompt"}
        prompt += "\nUntrusted evidence:\n" + json.dumps(inventory, ensure_ascii=False)
        schema = model.model_json_schema()
        if cleaning:
            editable_ids = [cue["id"] for cue in inventory.get("cues", [])]
            if editable_ids:
                schema["$defs"]["SubtitleEdit"]["properties"]["cue_id"]["enum"] = editable_ids
            # This is a fixed processing constraint, shared with checkpoint/application
            # validation. Keep saved editorial prompts and valid batch checkpoints intact.
            prompt += (
                f"\nBatch scope: batch {inventory.get('batch', 1)}/{inventory.get('total_batches', 1)}. "
                f"Editable cue IDs: {editable_ids}. Edit only entries in cues; context_cues and "
                "source_reference_cues are read-only. Other batches will be reviewed separately. "
                "Their absence is not an issue or missing subtitle content. A usable batch is complete; "
                "do not request another pass simply to see the rest of the film."
                "\nProcessing constraints: preserve each cue's existing time interval. "
                "Do not merge adjacent or split dialogue into a neighboring cue or delete it as "
                "a duplicate. Correct each cue separately. remove_duplicate is allowed only "
                "when another supplied cue has exactly identical original text, start_ms and "
                "end_ms, and at least one copy will remain."
            )

        def validate(answer):
            result = model.model_validate_json(answer)
            if cleaning:
                validate_cleanup_edits(result, inventory["cues"])
            elif not search:
                candidates = {c["id"] for c in inventory["candidate_cues"]}
                references = {c["id"] for c in inventory["reference_cues"]}
                if any(
                    a.candidate_id not in candidates or a.reference_id not in references
                    for a in result.anchors
                ):
                    raise ValueError("Alignment cites a cue that was not supplied")
                if "duration_seconds" in inventory:
                    if not result.alignment_confident:
                        try:
                            measured = fit_alignment(
                                result.model_copy(update={"usable": True, "alignment_confident": True}),
                                inventory["candidate_cues"],
                                inventory["reference_cues"],
                                inventory["duration_seconds"],
                            )
                        except ValueError as error:
                            raise ValueError(
                                "Recheck alignment anchors and recover a valid fit: " + str(error)
                            ) from error
                        raise ValueError(
                            "The supplied anchors pass independent alignment checks: "
                            f"scale={measured['scale']:.9f}, offset={measured['offset_seconds']:.3f}s, "
                            f"maximum residual={measured['max_error_seconds']:.3f}s. "
                            "Small timing variations within the documented tolerance are acceptable. "
                            "Reconsider alignment_confident using this evidence; ordinary text-quality notes "
                            "must not veto a verified timing fit. If a genuine blocker remains, explain its evidence."
                        )
                    fit_alignment(
                        result.model_copy(update={"usable": True}),
                        inventory["candidate_cues"],
                        inventory["reference_cues"],
                        inventory["duration_seconds"],
                    )

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
            schema=schema,
            validate=validate,
            on_event=prompt_events(self.on_event, selected_prompt),
            check=self.check,
            stage="Subtitle discovery"
            if search
            else "Subtitle cleanup"
            if cleaning
            else "Subtitle alignment and quality",
            complete="Subtitle search complete"
            if search
            else f"Subtitle cleanup batch {inventory.get('batch', 1)}/{inventory.get('total_batches', 1)} complete"
            if cleaning
            else "Subtitle alignment review complete",
        )
        return {"decision": result.model_dump(), "thread_id": thread}
