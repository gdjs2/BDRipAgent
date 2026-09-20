"""Attach agent descriptions and initial flags to locally analyzed tracks."""

from shared.naming import automatic_track_name, track_name
from shared.paths import contained, job_dir, write_json
from shared.tracks import TrackReviewResult, apply_flag_overrides, validate_review
from worker.adapters.audio_review import analyze_audio
from worker.adapters.subtitles import review


def review_tracks(ctx, result):
    tracks = result["tracks"]
    if not tracks:
        return []
    audio = [t for t in tracks if t["kind"] == "audio"]
    evidence = analyze_audio(ctx, audio, result["video"]["duration"]) if audio else {}
    tracks = [
        {**t, **({"audio_analysis": evidence[t["track_id"]]} if t["kind"] == "audio" else {})} for t in tracks
    ]
    root = contained(job_dir(job_dir(ctx.settings.cache_root / "agent", ctx.job.id), ctx.task_id), "tracks")
    # No workspace paths or raw audio are needed by the agent.
    inventory = [
        {k: v for k, v in t.items() if k not in ("subtitle_source_path", "path", "source_properties")}
        for t in tracks
    ]
    for track in inventory:
        if "audio_analysis" in track:
            data = track["audio_analysis"]
            track["audio_analysis"] = {
                **data,
                "samples": [{k: v for k, v in sample.items() if k != "path"} for sample in data["samples"]],
            }
    write_json(root / "inventory.json", {"tracks": inventory})
    ctx.progress(99, phase="Agent reviewing audio descriptions and track flags")
    answer = review(ctx)
    decision = TrackReviewResult.model_validate({"tracks": answer["tracks"]})
    validate_review(decision, inventory)
    path = ctx.output("metadata", "track-review.json")
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
    return updated
