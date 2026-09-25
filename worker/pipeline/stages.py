import json
import re

import numpy as np
from sqlalchemy import delete, select

from shared.config import profiles
from shared.db import session
from shared.encoding import is_smoke_test
from shared.languages import language_tag
from shared.media_details import encoder_summary
from shared.models import CRFResult, EncodeConfig, MovieJob, MovieTrack, TrackSelection
from shared.naming import release_name, track_name
from shared.original_languages import is_original, movie_languages
from shared.paths import contained, job_dir, write_json
from shared.subtitles import SUBTITLE_ANALYSIS_VERSION, subtitle_is_resolved
from worker.adapters.handbrake import Crop, encode_command, parse_progress
from worker.adapters.integrations import CRFStudioAdapter, Sup2supAdapter
from worker.adapters.media import analyze, probe, video_metadata
from worker.adapters.mkvtoolnix import MKVToolNixProgress
from worker.adapters.source_tracks import extract_tracks, link_file
from worker.adapters.subtitles import classify as classify_subtitle
from worker.adapters.subtitles import prepare as prepare_subtitle
from worker.adapters.track_review import review_tracks
from worker.pipeline.subtitle_discovery import discover_subtitles
from worker.pipeline.subtitle_imports import review_uploaded_subtitle
from worker.pipeline.track_analysis import analyze_tracks
from worker.pipeline.validation import EncodeValidator, timeline
from worker.progress import plan as progress_plan


def update_analysis(extra):
    def save(db, job):
        job.analysis = {**job.analysis, **extra}

    return save


def analyze_source(ctx):
    result = (
        ctx.job.analysis if all(k in ctx.job.analysis for k in ("video", "crop", "tracks")) else analyze(ctx)
    )
    path = ctx.output("metadata", "source-scan.json")
    write_json(path, result)
    ctx.artifact(path, "SOURCE_METADATA")
    from worker.pipeline.track_analysis import save_analysis

    return lambda db, job: save_analysis(db, job, result)


def review_source_tracks(ctx):
    return analyze_tracks(
        ctx,
        scan=analyze,
        prepare_subtitle=prepare_subtitle,
        classify_subtitle=classify_subtitle,
        review_tracks=review_tracks,
    )


def prepare_tracks(ctx):
    with session() as db:
        selection = db.scalar(select(TrackSelection).where(TrackSelection.job_id == ctx.job.id))
    crop = Crop(**ctx.job.analysis["crop"])
    tracks = {
        t["track_id"]: t for t in [*ctx.job.analysis["tracks"], *ctx.job.analysis.get("uploaded_tracks", [])]
    }
    # These arrays are the user's ordered choices, not sets: retain their order through preparation and mux.
    chosen = [tracks[i] for i in [*selection.audio_track_ids, *selection.subtitle_track_ids]]
    steps = progress_plan(ctx, extraction=25, processing=75)
    native = [t for t in chosen if t.get("origin") not in ("upload", "discovery")]
    files, _ = extract_tracks(steps["extraction"], native) if native else ({}, None)
    for track in chosen:
        if track.get("origin") in ("upload", "discovery"):
            source = contained(ctx.settings.workspace_root, track["upload_path"], exists=True)
            path = ctx.output("uploaded-subtitles", f"{track['upload_id']}{source.suffix}")
            link_file(source, path)
            files[track["track_id"]] = {"path": path, "timestamps": None}
    steps["extraction"].done("Selected tracks available")
    track_steps = (
        progress_plan(
            steps["processing"], **{str(t["track_id"]): 1 if t["kind"] == "audio" else 10 for t in chosen}
        )
        if chosen
        else {}
    )
    extraction = [
        (track, files[track["track_id"]]["path"], files[track["track_id"]]["timestamps"]) for track in chosen
    ]

    prepared = []
    for track, path, timestamps in extraction:
        track_id = track["track_id"]
        audio = track["kind"] == "audio"
        step = track_steps[str(track_id)]
        relative = ctx.artifact(path, "AUDIO" if audio else "SUBTITLE_ORIGINAL", info={"track_id": track_id})
        item = {**track, "path": relative}
        if audio:
            item["timestamps"] = ctx.artifact(timestamps, "AUDIO_TIMESTAMPS")
        elif track.get("origin") != "upload" or track.get("discovery"):
            subtitle_steps = progress_plan(
                step, detection=40 if not subtitle_is_resolved(item) else 0, crop=60
            )
            subtitle_steps["crop"].progress(None, phase=f"Processing subtitle track {track_id}")
            if not subtitle_is_resolved(item):
                if item.get("subtitle_detection", {}).get("schema_version") == SUBTITLE_ANALYSIS_VERSION:
                    raise ValueError(
                        f"Track {track_id}: review unresolved language or SDH before preparation"
                    )
                item = classify_subtitle(subtitle_steps["detection"], item, path)
                subtitle_steps["detection"].done("Subtitle analysis complete")
            cached = ctx.job.analysis.get("prepared_track_cache", {}).get(str(track_id), {}).get("path")
            if cached and contained(ctx.workspace, cached).is_file():
                item["path"] = cached
                subtitle_steps["crop"].done(f"Reusing cropped subtitle track {track_id}")
            else:
                cropped = ctx.output("subtitles", f"track-{track_id}.cropped.sup")
                subtitle_steps["crop"].progress(None, phase=f"Cropping subtitle track {track_id}")
                provenance = track.get("discovery", {})
                shared_crop = (
                    contained(
                        ctx.settings.workspace_root, track["upload_path"].rsplit("/", 1)[0] + "/cropped.sup"
                    )
                    if track.get("discovery", {}).get("crop_checked")
                    else None
                )
                if (
                    shared_crop
                    and shared_crop.is_file()
                    and provenance.get("crop") == crop.model_dump()
                    and provenance.get("source_video")
                    == {key: ctx.job.analysis["video"][key] for key in ("width", "height")}
                ):
                    link_file(shared_crop, cropped)
                    ctx.log(f"Reusing Sup2sup-checked discovery crop for track {track_id}.")
                else:
                    Sup2supAdapter().crop(
                        subtitle_steps["crop"], path, cropped, crop, ctx.job.analysis["video"]
                    )
                item["path"] = ctx.artifact(cropped, "SUBTITLE_CROPPED", info={"track_id": track_id})
        prepared.append(item)
        step.done(f"Track {track_id} prepared", completed_tracks=len(prepared), total_tracks=len(chosen))
    name = release_name(
        ctx.job.title,
        ctx.job.year,
        profiles()[ctx.job.analysis_profile]["codec"],
        [t for t in prepared if t["kind"] == "audio"],
    )

    def save(db, job):
        save_prepared_tracks(db, job, prepared)
        job.release_name = ("SMOKE-TEST." if is_smoke_test(job) else "") + name

    return save


def save_prepared_tracks(db, job, prepared):
    detected = {t["track_id"]: t for t in prepared if t["kind"] == "subtitles"}
    tracks = [detected.get(t["track_id"], t) for t in job.analysis.get("tracks", [])]
    cache = {**job.analysis.get("prepared_track_cache", {}), **{str(t["track_id"]): t for t in prepared}}
    job.analysis = {
        **job.analysis,
        "prepared_tracks": prepared,
        "prepared_track_cache": cache,
        "tracks": tracks,
    }
    for row in db.scalars(
        select(MovieTrack).where(MovieTrack.job_id == job.id, MovieTrack.kind == "subtitles")
    ):
        if row.track_id in detected:
            row.info = detected[row.track_id]


def crf_analysis(ctx):
    result = CRFStudioAdapter().run_analysis(
        ctx, profiles()[ctx.job.analysis_profile], Crop(**ctx.job.analysis["crop"])
    )
    path = ctx.output("crf", "normalized.json")
    write_json(path, result)
    ctx.artifact(path, "CRF_RESULTS")

    def save(db, job):
        db.execute(delete(CRFResult).where(CRFResult.job_id == job.id))
        db.add(CRFResult(job_id=job.id, data=result))

    return save


def encode(ctx):
    with session() as db:
        config = db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == ctx.job.id)).data
    if is_smoke_test(ctx.job) != (config.get("execution_mode") == "smoke"):
        raise ValueError("Smoke test selection does not match the job")
    if is_smoke_test(ctx.job):
        source = ctx.source()
        ctx.log("SMOKE TEST: skipping HandBrake encoding; downstream stages will reuse the source video.")
        inspection_path = ctx.output("encode", "smoke-source.json")
        inspection = json.loads(ctx.run([ctx.settings.mkvmerge_bin, "-J", source], output=inspection_path))
        video_tracks = [t for t in inspection["tracks"] if t["type"] == "video"]
        if len(video_tracks) != 1:
            raise ValueError("Smoke test requires exactly one source video track")
        ctx.artifact(inspection_path, "SOURCE_METADATA")
        report = ctx.output("encode", "smoke-test.json")
        write_json(report, {"smoke_test": True, "encoding_skipped": True, "source_path": ctx.job.source_path})
        ctx.artifact(report, "SMOKE_TEST")
        return update_analysis(
            {
                "encoder_average_qp": {},
                "encoding_skipped": True,
                "smoke_video_track_id": video_tracks[0]["id"],
            }
        )
    ctx.progress(None, phase="Preparing video encoder")
    encode_dir = (
        "encode/revisions/" + ctx.job.analysis["encode_revision"]["id"]
        if ctx.job.analysis.get("encode_revision")
        else "encode"
    )
    output = ctx.output(encode_dir, "video.mkv")
    ctx.run(
        encode_command(
            ctx.settings.handbrake_bin,
            ctx.source(),
            output,
            ctx.job.analysis["video"],
            Crop(**ctx.job.analysis["crop"]),
            config["profile_snapshot"],
            config.get("crf"),
            rate_control=config.get("rate_control", "crf"),
            bitrate_kbps=config.get("bitrate_kbps"),
        ),
        progress_parser=parse_progress,
        pausable=True,
    )
    ctx.progress(None, phase="Saving encoded video and encoder statistics")
    path = ctx.artifact(output, "ENCODED_VIDEO")
    log_text = ctx.log_path.read_text(errors="replace")
    summary = encoder_summary(log_text, config["codec"])
    if summary:
        info_path = ctx.output(encode_dir, ctx.job.release_name + ".encoder.txt")
        info_path.write_text(summary + "\n", encoding="utf-8")
        ctx.artifact(info_path, "ENCODER_INFO")
    stats = {}
    for line in log_text.splitlines():
        match = re.search(r"frame\s+([IPB]):.*?(?:Avg QP:|QP:)\s*([\d.]+)", line, re.I)
        if match:
            stats[match[1].upper()] = float(match[2])
    return update_analysis({"encoded_path": path, "encoder_average_qp": stats})


def validate(ctx):
    frame_index = {}
    steps = progress_plan(ctx, metadata=2, source=44, encoded=44, checks=10)
    with session() as db:
        config = db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == ctx.job.id)).data
    smoke = is_smoke_test(ctx.job)
    if smoke:
        if config.get("execution_mode") != "smoke" or not ctx.job.analysis.get("encoding_skipped"):
            raise ValueError("Smoke test requires an explicitly skipped encoding task")
        from worker.pipeline.smoke import validation_report

        ctx.log("SMOKE TEST: checking source readability; encode validation is skipped, not passed.")
        result = validation_report(ctx)
        metadata = ctx.job.analysis["video"]
    else:
        encoded = contained(ctx.workspace, ctx.job.analysis["encoded_path"], exists=True)
        steps["metadata"].progress(None, phase="Reading encoded video metadata")
        metadata = video_metadata(probe(ctx, encoded, "encoded-probe.json"))
        steps["metadata"].done("Encoded metadata ready")
        ctx.log("Decoding source and encoded video to validate frame counts and timestamp correspondence")

        def decode(path, step, video, label):
            duration = video.get("duration", 0)
            step.progress(0 if duration else None, phase=label)

            def report(frames, seconds):
                fraction = seconds / duration if duration else None
                step.progress(
                    100 * fraction if fraction is not None else None,
                    phase=label,
                    decoded_frames=frames,
                    decoded_seconds=round(seconds, 2),
                    duration_seconds=duration,
                    indeterminate=fraction is None or fraction >= 1,
                )

            points = timeline(path, ctx.check, on_progress=report)
            step.done(label + " complete", decoded_frames=len(points))
            return points

        source_pts = decode(
            ctx.source(), steps["source"], ctx.job.analysis["video"], "Validating source frames"
        )
        index_path = ctx.output("encode", "source-frame-pts.npy")
        np.save(index_path, source_pts)
        frame_index["source_frame_index"] = ctx.artifact(index_path, "SOURCE_FRAME_INDEX")
        encoded_pts = decode(encoded, steps["encoded"], metadata, "Validating encoded frames")
        steps["checks"].progress(None, phase="Checking frame correspondence and encode settings")
        result = EncodeValidator().validate(
            ctx.job.analysis["video"],
            metadata,
            Crop(**ctx.job.analysis["crop"]),
            config["profile_snapshot"],
            source_pts,
            encoded_pts,
            encoded.stat().st_size,
        )
    result["metrics"]["average_qp"] = ctx.job.analysis.get("encoder_average_qp", {})
    path = ctx.output("encode", "validation.json")
    write_json(path, result)
    ctx.artifact(path, "VALIDATION")
    # Persist a failed report as well; never advance to remux after validation failure.
    with session() as db:
        job = db.get(MovieJob, ctx.job.id)
        job.validation = result
        db.commit()
    if not smoke and not result["valid"]:
        raise ValueError("Encode validation failed: " + "; ".join(result["errors"]))
    return update_analysis({"encoded_video": metadata, **frame_index})


def mux_command(ctx, output):
    source = ctx.source()
    smoke = is_smoke_test(ctx.job)
    video = source if smoke else contained(ctx.workspace, ctx.job.analysis["encoded_path"], exists=True)
    video_id = ctx.job.analysis["smoke_video_track_id"] if smoke else 0
    tracks = ctx.job.analysis["prepared_tracks"]
    originals = movie_languages(ctx.job.analysis, metadata=getattr(ctx.job, "imdb_metadata", None))
    metrics = ctx.job.validation["metrics"]
    video_offset_ms = (metrics["source_first_pts"] - metrics["encoded_first_pts"]) * 1000
    command = [
        ctx.settings.mkvmerge_bin,
        "--gui-mode",
        "-o",
        str(output),
        "--title",
        f"{ctx.job.title} ({ctx.job.year})",
        "--track-name",
        f"{video_id}:SMOKE TEST - Source reused" if smoke else f"{video_id}:",
        "--original-flag",
        f"{video_id}:1",
        "--video-tracks",
        str(video_id),
        "--no-audio",
        "--no-subtitles",
        "--no-chapters",
        "--no-global-tags",
        "--no-track-tags",
        "--no-attachments",
        "--sync",
        f"{video_id}:{round(video_offset_ms)}",
        str(video),
    ]
    for track in tracks:
        command += [
            "--language",
            f"0:{track['language']}",
            "--track-name",
            f"0:{track_name(track)}",
            "--default-track-flag",
            f"0:{int(track['default'])}",
            "--forced-display-flag",
            f"0:{int(track['forced'])}",
            "--hearing-impaired-flag",
            f"0:{int(track.get('hearing_impaired', False))}",
            "--visual-impaired-flag",
            f"0:{int(track.get('visual_impaired', False))}",
            "--commentary-flag",
            f"0:{int(track.get('commentary', False))}",
            "--original-flag",
            f"0:{int(is_original(track, originals))}",
        ]
        if track.get("timestamps"):
            command += ["--timestamps", f"0:{contained(ctx.workspace, track['timestamps'], exists=True)}"]
        command += [str(contained(ctx.workspace, track["path"], exists=True))]
    # Read chapters/global tags directly from immutable input without importing unselected tracks.
    command += [
        "--no-video",
        "--no-audio",
        "--no-subtitles",
        "--no-attachments",
        "--no-track-tags",
        str(source),
    ]
    command += ["--track-order", ",".join([f"0:{video_id}", *(f"{i}:0" for i in range(1, len(tracks) + 1))])]
    return command


def mux(ctx):
    # Legacy prepared jobs may predate the source-wide movie-language snapshot.
    if "original_languages" not in ctx.job.analysis:
        from backend.app.track_choices import original_languages

        with session() as db:
            ctx.job.analysis = {**ctx.job.analysis, "original_languages": original_languages(db, ctx.job)}
    smoke = is_smoke_test(ctx.job)
    smoke_ready = (
        ctx.job.analysis.get("encoding_skipped") is True
        and ctx.job.validation.get("smoke_test") is True
        and ctx.job.validation.get("skipped") is True
        and ctx.job.validation.get("source_readable") is True
        and ctx.job.validation.get("valid") is None
        and not ctx.job.validation.get("errors")
    )
    if smoke and not smoke_ready:
        raise ValueError("Smoke remux requires a readable source and explicitly skipped encode validation")
    if not smoke and not ctx.job.validation.get("valid"):
        raise ValueError("Remux requires a passing validation report")
    unresolved = [
        t
        for t in ctx.job.analysis["prepared_tracks"]
        if t["kind"] == "subtitles" and not subtitle_is_resolved(t)
    ]
    steps = progress_plan(ctx, detection=20 if unresolved else 0, merge=95, checks=5)
    detections = (
        progress_plan(steps["detection"], **{str(t["track_id"]): 1 for t in unresolved}) if unresolved else {}
    )
    # Older prepared jobs also need content detection before any inherited flags
    # reach a new output. Persist it with the successful mux result.
    prepared = []
    for track in ctx.job.analysis["prepared_tracks"]:
        if track["kind"] == "subtitles" and not subtitle_is_resolved(track):
            step = detections[str(track["track_id"])]
            track = classify_subtitle(step, track, contained(ctx.workspace, track["path"], exists=True))
            step.done("Subtitle analysis ready")
        prepared.append({**track, "original": is_original(track, movie_languages(ctx.job.analysis))})
    ctx.job.analysis = {**ctx.job.analysis, "prepared_tracks": prepared}
    directory = job_dir(ctx.settings.completed_root, ctx.job.id)
    revision = ctx.job.analysis.get("remux_revision")
    if revision:
        directory = contained(directory, f"remuxes/{revision['id']}")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"{ctx.task_id}.partial.mkv"
    output = directory / f"{ctx.job.release_name}.mkv"
    if output.name != f"{ctx.job.release_name}.mkv" or output.parent != directory:
        raise ValueError("Invalid release filename")
    command = mux_command(ctx, temporary)
    plan = ctx.output("mux", "plan.json")
    write_json(
        plan,
        {
            "video": ctx.job.source_path if smoke else ctx.job.analysis["encoded_path"],
            "smoke_test": smoke,
            "original_languages": movie_languages(ctx.job.analysis),
            "video_original": True,
            "tracks": ctx.job.analysis["prepared_tracks"],
            "chapters_and_global_tags_from": ctx.job.source_path,
            "argv": command,
        },
    )
    ctx.artifact(plan, "MUX_PLAN")
    steps["merge"].progress(None, phase="Merging final MKV", tool="mkvmerge")
    steps["merge"].run(
        command, allowed=(0, 1), progress_parser=MKVToolNixProgress("mkvmerge", "Merging final MKV")
    )
    steps["merge"].done("Final MKV merged")
    steps["checks"].progress(None, phase="Checking merged file")
    inspection = json.loads(
        ctx.run([ctx.settings.mkvmerge_bin, "-J", temporary], output=ctx.output("mux", "inspection.json"))
    )
    if len(inspection["tracks"]) != 1 + len(ctx.job.analysis["prepared_tracks"]):
        raise ValueError("Final mux track count does not match mux plan")
    if inspection["container"]["properties"].get("title") != f"{ctx.job.title} ({ctx.job.year})":
        raise ValueError("Final mux title does not match the movie title and year")
    if not inspection["tracks"][0]["properties"].get("flag_original"):
        raise ValueError("Final mux video is missing the original-language flag")
    for actual, expected in zip(inspection["tracks"][1:], ctx.job.analysis["prepared_tracks"], strict=True):
        p = actual["properties"]
        if p.get("track_name") != track_name(expected):
            raise ValueError("Final mux track name does not match the naming policy")
        actual_language = p.get("language_ietf") or p.get("language") or "und"
        if language_tag(actual_language, allow_unknown=True) != language_tag(
            expected["language"], allow_unknown=True
        ):
            raise ValueError("Final mux track language does not match the confirmed language code")
        for flag, key in (
            ("default_track", "default"),
            ("forced_track", "forced"),
            ("flag_hearing_impaired", "hearing_impaired"),
            ("flag_commentary", "commentary"),
            ("flag_visual_impaired", "visual_impaired"),
            ("flag_original", "original"),
        ):
            if bool(p.get(flag, False)) != bool(expected.get(key, False)):
                raise ValueError(f"Final mux did not preserve {key}")
    ctx.check()
    temporary.replace(output)
    ctx.artifact(output, "SMOKE_TEST_MKV" if smoke else "FINAL_MKV", storage="completed")

    def save(db, job):
        from worker.output_backups import retire_outputs

        save_prepared_tracks(db, job, prepared)
        final_path = str(output.relative_to(ctx.settings.completed_root))
        job.analysis = {
            **job.analysis,
            "final_path": final_path,
            "mux_original_languages": movie_languages(ctx.job.analysis),
        }
        retire_outputs(db, job, kinds={"FINAL_MKV", "SMOKE_TEST_MKV"}, keep_paths={("completed", final_path)})

    return save


def generate_candidates(ctx):
    from worker.pipeline.screenshots import generate

    return generate(ctx)


def select_screenshots(ctx):
    from worker.pipeline.screenshots import select_frames

    return select_frames(ctx)


def render_screenshots(ctx):
    from worker.pipeline.screenshots import render

    return render(ctx)


def generate_release(ctx):
    from worker.pipeline.release import generate

    return generate(ctx)


def sync_screenshots(ctx):
    from worker.pipeline.screenshots import sync_shared_pool

    return sync_shared_pool(ctx)


HANDLERS = {
    "sync_screenshots": sync_screenshots,
    "discover_subtitles": discover_subtitles,
    "review_uploaded_subtitle": review_uploaded_subtitle,
    "analyze": analyze_source,
    "review_tracks": review_source_tracks,
    "prepare_tracks": prepare_tracks,
    "crf_analysis": crf_analysis,
    "encode": encode,
    "validate": validate,
    "mux": mux,
    "generate_candidates": generate_candidates,
    "select_screenshots": select_screenshots,
    "render_screenshots": render_screenshots,
    "generate_release": generate_release,
}
