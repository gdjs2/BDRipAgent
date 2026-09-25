"""Compare track evidence, expanding audio coverage until the agent can distinguish it."""

from shared.agent_prompts import task_prompts
from shared.config import behavior
from shared.naming import automatic_track_name, track_name
from shared.paths import contained, job_dir, write_json
from shared.tracks import TrackReviewResult, apply_flag_overrides, validate_review
from worker.adapters.audio_review import analyze_audio, diffusion_starts
from worker.adapters.subtitles import review
from worker.progress import plan


def inventory_tracks(tracks):
    inventory = []
    for track in tracks:
        item = {
            k: v
            for k, v in track.items()
            if k
            not in (
                "subtitle_source_path",
                "source_track_path",
                "source_timestamps_path",
                "path",
                "source_properties",
                "track_review",
            )
        }
        if "audio_analysis" in item:
            data = item["audio_analysis"]
            # Preserve initial anchors and recent evidence. Earlier conclusions remain
            # in previous_comparison, so arbitrarily long films don't grow every prompt.
            samples = data["samples"]
            visible = samples if len(samples) <= 15 else samples[:3] + samples[-12:]
            item["audio_analysis"] = {
                **data,
                "samples": [{k: v for k, v in s.items() if k != "path"} for s in visible],
                "total_samples": len(samples),
            }
        inventory.append(item)
    return inventory


def matching_signals(tracks):
    """Identical sampled mono PCM is useful evidence, never proof of identical mixes."""
    groups = {}
    for track in tracks:
        for sample in track.get("audio_analysis", {}).get("samples", []):
            if sample.get("pcm_sha256") and not sample.get("silent"):
                key = (sample["start_seconds"], sample["pcm_sha256"])
                groups.setdefault(key, []).append({"track_id": track["track_id"], "sample_id": sample["id"]})
    return [
        {"start_seconds": key[0], "matches": entries} for key, entries in groups.items() if len(entries) > 1
    ]


def review_tracks(ctx, result, *, on_progress=None):
    tracks = result["tracks"]
    if not tracks:
        return []
    if result.get("audio_comparison", {}).get("status") in ("resolved", "inconclusive") and all(
        t.get("track_review") for t in tracks
    ):
        return tracks
    audio = [t for t in tracks if t["kind"] == "audio" and "audio_analysis" not in t]
    steps = plan(ctx, evidence=20 if audio else 0, comparison=80)
    evidence = analyze_audio(steps["evidence"], audio, result["video"]["duration"]) if audio else {}
    if audio:
        steps["evidence"].done("Initial audio evidence ready")
    tracks = [
        {**t, **({"audio_analysis": evidence[t["track_id"]]} if t["track_id"] in evidence else {})}
        for t in tracks
    ]
    root = contained(job_dir(job_dir(ctx.settings.cache_root / "agent", ctx.job.id), ctx.task_id), "tracks")
    duration = float(result["video"]["duration"])
    seconds = max(
        5, min(60, float(behavior()["integrations"].get("audio_review", {}).get("sample_seconds", 30)))
    )
    max_rounds = max(
        1,
        min(
            30,
            int(
                result.get("audio_review_policy", {}).get(
                    "max_rounds", behavior()["integrations"].get("audio_review", {}).get("max_rounds", 6)
                )
            ),
        ),
    )
    round_number = result.get("audio_review_round", 0)
    pending = result.get("audio_review_next")
    rounds = plan(
        steps["comparison"], **{str(i): 1 for i in range(round_number, max(max_rounds, round_number + 1))}
    )

    def save():
        result["tracks"] = tracks
        if on_progress:
            on_progress(result)

    while True:
        ctx.check()
        stop_reason = None
        if round_number >= max_rounds and result.get("audio_comparison"):
            result["audio_comparison"] = {
                **result["audio_comparison"],
                "status": "inconclusive",
                "requires_human": True,
                "stop_reason": f"The configured limit of {max_rounds} comparison rounds was reached. Review the possible differences and choose tracks and flags manually.",
            }
            result["audio_review_next"] = None
            save()
            break
        current = plan(rounds[str(round_number)], sampling=60 if pending else 0, agent=40)
        if pending:
            ids = set(pending["track_ids"])
            requested = [t for t in tracks if t["track_id"] in ids]
            used = [
                (start, seconds)
                for t in requested
                for start in t.get("audio_analysis", {}).get("attempted_starts", [])
            ]
            used += [
                (s["start_seconds"], s.get("duration_seconds", seconds))
                for t in requested
                for s in t.get("audio_analysis", {}).get("samples", [])
            ]
            starts = diffusion_starts(duration, seconds, used)
            if not starts:
                stop_reason = "No unexamined intervals long enough for another usable sample remain."
            elif not any(
                "speech transcription" in t.get("audio_analysis", {}).get("method", "") for t in requested
            ):
                stop_reason = "Local transcription is unavailable; more samples cannot resolve the requested content differences."
            else:
                ctx.log(f"Audio comparison round {round_number + 1}: {pending['question']}")
                current["sampling"].progress(
                    None, phase=f"Comparing audio: sampling {', '.join(str(s) + 's' for s in starts)}"
                )
                evidence = analyze_audio(current["sampling"], requested, duration, starts=starts)
                current["sampling"].done("Additional audio evidence ready")
                tracks = [
                    {
                        **t,
                        **({"audio_analysis": evidence[t["track_id"]]} if t["track_id"] in evidence else {}),
                    }
                    for t in tracks
                ]
                if sum(len(t.get("audio_analysis", {}).get("samples", [])) for t in requested) == sum(
                    len(data["samples"]) for data in evidence.values()
                ):
                    stop_reason = (
                        "The additional intervals could not be decoded; no new evidence is available."
                    )
                result["audio_review_next"] = None
                save()  # A failed subsequent agent call never repeats these samples.
            if stop_reason:
                comparison = {
                    **result["audio_comparison"],
                    "status": "inconclusive",
                    "requires_human": True,
                    "stop_reason": stop_reason,
                }
                result["audio_comparison"] = comparison
                result["audio_review_next"] = None
                save()
                break
        inventory = inventory_tracks(tracks)
        write_json(
            root / "inventory.json",
            {
                "tracks": inventory,
                "agent_prompt": task_prompts(ctx)["track_review"],
                "comparison_round": round_number + 1,
                "previous_comparison": result.get("audio_comparison"),
                "matching_mono_samples": matching_signals(inventory),
                "analysis_budget": {
                    "max_rounds": max_rounds,
                    "remaining_rounds_after_this": max(0, max_rounds - round_number - 1),
                    "final_round": round_number + 1 >= max_rounds,
                },
                "sampling_policy": "Expand coverage until differences are resolved, the configured hard round limit is reached, or usable evidence is exhausted.",
            },
        )
        current["agent"].progress(
            None,
            phase=f"Agent comparing audio tracks and flags: round {round_number + 1} of at most {max_rounds}",
            comparison_round=round_number + 1,
            max_rounds=max_rounds,
        )
        answer = review(current["agent"])
        current["agent"].done("Track comparison round complete")
        decision = TrackReviewResult.model_validate(
            {k: answer[k] for k in ("tracks", "audio_comparison") if k in answer}
        )
        validate_review(decision, inventory)
        round_number += 1
        path = ctx.output("metadata", f"track-review-{round_number}.json")
        write_json(path, answer)
        relative = ctx.artifact(path, "TRACK_REVIEW")
        by_id = {t.track_id: t for t in decision.tracks}
        updated = []
        for track in tracks:
            reviewed = by_id[track["track_id"]]
            item = apply_flag_overrides(
                {
                    **track,
                    **reviewed.flags.model_dump(),
                    "track_review": {
                        "schema_version": 1,
                        **reviewed.model_dump(exclude={"track_id"}),
                        "report": relative,
                    },
                }
            )
            item["base_name"] = automatic_track_name({**item, "hearing_impaired": False, "forced": False})
            item["suggested_name"] = automatic_track_name(item)
            item["mux_name"] = track_name(item)
            updated.append(item)
        tracks = updated
        result["audio_review_round"] = round_number
        comparison = decision.audio_comparison
        result["audio_review_next"] = None
        if comparison:
            result["audio_comparison"] = {
                **comparison.model_dump(),
                "rounds": round_number,
                "max_rounds": max_rounds,
                "status": "sampling" if comparison.needs_more else "resolved",
            }
            if comparison.needs_more:
                # Include the uncertain track's comparison partners even if the model
                # accidentally requests only one side of a comparison.
                requested_ids = set(comparison.next_track_ids)
                for item in comparison.distinctions:
                    if not item.resolved or item.track_id in requested_ids:
                        requested_ids.add(item.track_id)
                        requested_ids.update(item.compared_with)
                result["audio_review_next"] = {
                    "track_ids": sorted(requested_ids),
                    "question": comparison.question,
                }
        save()
        pending = result["audio_review_next"]
        if not pending:
            break
    steps["comparison"].done("Track comparison complete")
    return tracks
