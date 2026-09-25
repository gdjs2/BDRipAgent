"""Review every cue in bounded batches before rendering editable subtitles to PGS."""

import pysubs2

from shared.subtitle_discovery import SubtitleCleanup, blocking_issues, covers, validate_cleanup_edits


def clean_subtitles(ctx, subtitles, candidate, request, *, references=()):
    events = [event for event in subtitles if not event.is_comment and event.plaintext.strip()]
    if not events:
        raise ValueError("No dialogue remains for subtitle cleanup")
    cues = []
    for index, event in enumerate(events, 1):
        text = event.plaintext.strip()
        if len(text) > 8000:
            raise ValueError("Subtitle cue is too long for a complete cleanup review")
        cues.append({"id": index, "start_ms": event.start, "end_ms": event.end, "text": text})
    batches, batch, size = [], [], 0
    for cue in cues:
        if batch and (len(batch) >= 100 or size + len(cue["text"]) > 24000):
            batches.append(batch)
            batch, size = [], 0
        batch.append(cue)
        size += len(cue["text"])
    if batch:
        batches.append(batch)
    edits, reviews, critical_errors = {}, [], []
    previous_corrections = []
    for index, batch in enumerate(batches, 1):
        ctx.check()
        ctx.progress(None, phase=f"Cleaning {candidate.language} subtitles: batch {index}/{len(batches)}")
        # Allow for unknown FPS/offset when selecting reference context. Semantic
        # matches, not this approximate window, establish the actual cue identity.
        lower = batch[0]["start_ms"] / 1000 * 0.9 - 600
        upper = batch[-1]["end_ms"] / 1000 * 1.1 + 600
        nearby = [cue for cue in references if lower <= cue["seconds"] <= upper]
        if len(nearby) > 192:
            nearby = [nearby[round(i * (len(nearby) - 1) / 191)] for i in range(192)]
        inventory = {
            "mode": "clean",
            "movie": {
                "title": ctx.job.title,
                "year": ctx.job.year,
                "imdb_id": getattr(ctx.job, "imdb_id", None),
            },
            "candidate": {
                "release": getattr(candidate, "release", ""),
                "source_url": getattr(candidate, "source_url", None),
            },
            "source_video": {
                k: getattr(ctx.job, "analysis", {}).get("video", {}).get(k) for k in ("fps", "duration")
            },
            "requested_language": candidate.language,
            "batch": index,
            "total_batches": len(batches),
            "cues": batch,
            "context_cues": cues[max(0, batch[0]["id"] - 3) : batch[0]["id"] - 1]
            + cues[batch[-1]["id"] : batch[-1]["id"] + 2],
            "source_reference_cues": nearby,
            "previous_corrections": previous_corrections,
        }
        answer = request(ctx, inventory)
        review = SubtitleCleanup.model_validate(answer["decision"])
        initial_review = None
        if (
            review.single_language
            and covers(review.language, candidate.language)
            and (not review.usable or blocking_issues(review))
        ):
            initial_review = review.model_dump()
            ctx.progress(
                None,
                phase=f"Repairing {candidate.language} subtitles with cross-language references: batch {index}/{len(batches)}",
            )
            answer = request(ctx, {**inventory, "previous_review": initial_review})
            review = SubtitleCleanup.model_validate(answer["decision"])
            # Do not lose already supported fixes if the follow-up only lists
            # additional repairs. New edits for the same cue take precedence.
            prior = SubtitleCleanup.model_validate(initial_review)
            merged = {edit.cue_id: edit for edit in prior.edits}
            merged.update({edit.cue_id: edit for edit in review.edits})
            review = review.model_copy(update={"edits": list(merged.values())})
        if not review.single_language or not covers(review.language, candidate.language):
            raise ValueError("Only confidently single-language subtitles are accepted: " + review.explanation)
        if blocking_issues(review):
            raise ValueError("Subtitle repair needs guidance: " + "; ".join(blocking_issues(review)))
        remaining = list(review.issues)
        if not review.usable:
            remaining.append(review.explanation)
        critical_errors.extend(
            f"Batch {index}: {issue}"
            for issue in remaining
            if issue.lstrip().upper().startswith(("CRITICAL", "BLOCKING:"))
        )
        validate_cleanup_edits(review, batch)
        for edit in review.edits:
            if edit.cue_id in edits:
                raise ValueError("Subtitle cleanup cites an already edited cue")
            edits[edit.cue_id] = edit
        reviews.append({**review.model_dump(), "initial_review": initial_review})
        if candidate.language.startswith(("zh-Hans", "zh-Hant")):
            # Carry recent editorial decisions across bounded batches without
            # exposing earlier cue IDs as editable evidence or unbounding prompts.
            context = previous_corrections + [
                {"original_text": e.original_text, "replacement_text": e.replacement_text}
                for e in review.edits
                if e.action == "correct_text"
            ]
            previous_corrections, context_size = [], 0
            for correction in reversed(context):
                length = sum(len(text) for text in correction.values())
                if context_size + length > 8000:
                    continue
                previous_corrections.insert(0, correction)
                context_size += length
                if len(previous_corrections) == 32:
                    break
    result = pysubs2.SSAFile()
    for cue in cues:
        edit = edits.get(cue["id"])
        if edit and edit.replacement_text is None:
            continue
        event = pysubs2.SSAEvent(start=cue["start_ms"], end=cue["end_ms"])
        event.plaintext = edit.replacement_text if edit else cue["text"]
        if "\ufffd" in event.plaintext:
            critical_errors.append(
                f"CRITICAL cue {cue['id']} at {cue['start_ms'] / 1000:g}s: damaged replacement characters remain after repair"
            )
        result.append(event)
    if not result:
        raise ValueError("Subtitle cleanup removed all dialogue")
    return result, {
        "single_language": True,
        "language": candidate.language,
        "reviewed_cues": len(cues),
        "retained_cues": len(result),
        "edited_cues": len(edits),
        "reviews": reviews,
        "critical_errors": critical_errors,
        "requires_attention": bool(critical_errors),
        "method": "complete cue review and reference-based repair; best-effort plain-text SRT before local PGS rendering",
    }
