import json
import re

import numpy as np
from sqlalchemy import delete, select

from shared.config import profiles
from shared.db import session
from shared.encoding import is_smoke_test
from shared.models import CRFResult, EncodeConfig, MovieJob, MovieTrack, TrackSelection
from shared.naming import release_name, track_name
from shared.paths import contained, job_dir, write_json
from shared.subtitles import subtitle_is_resolved
from worker.adapters.handbrake import Crop, encode_command, parse_progress
from worker.adapters.integrations import CRFStudioAdapter, Sup2supAdapter
from worker.adapters.media import AUDIO_EXTENSIONS, analyze, probe, video_metadata
from worker.adapters.mkvtoolnix import MKVToolNixProgress
from worker.adapters.subtitles import classify as classify_subtitle
from worker.adapters.track_review import review_tracks
from worker.pipeline.validation import EncodeValidator, timeline


def update_analysis(extra):
    def save(db, job):
        job.analysis = {**job.analysis, **extra}

    return save


def analyze_source(ctx):
    result = analyze(ctx)
    subtitles = [
        t for t in result["tracks"] if t["kind"] == "subtitles" and t.get("codec_id") == "S_HDMV/PGS"
    ]
    paths = {t["track_id"]: ctx.output("subtitles", f"track-{t['track_id']}.sup") for t in subtitles}
    if subtitles:
        phase = "Extracting subtitles for track review"
        ctx.progress(0, phase=phase, tool="mkvextract")
        ctx.run(
            [
                ctx.settings.mkvextract_bin,
                ctx.source(),
                "tracks",
                *(f"{track_id}:{path}" for track_id, path in paths.items()),
                "--gui-mode",
            ],
            progress_parser=MKVToolNixProgress("mkvextract", phase),
        )
        for track_id, path in paths.items():
            if not path.is_file() or not path.stat().st_size:
                raise ValueError(f"Track {track_id} extraction produced no data")
    reviewed = {}
    for index, track in enumerate(subtitles, 1):
        track_id = track["track_id"]
        ctx.check()
        ctx.log(f"Analyzing subtitle {index}/{len(subtitles)} (track {track_id}) before track selection")
        original = ctx.artifact(paths[track_id], "SUBTITLE_ORIGINAL", info={"track_id": track_id})
        reviewed[track_id] = classify_subtitle(
            ctx,
            {**track, "subtitle_source_path": original},
            paths[track_id],
            require_confident=False,
            agent_review=True,
        )
    result["tracks"] = [reviewed.get(t["track_id"], t) for t in result["tracks"]]
    result["subtitle_analysis_version"] = 1
    result["tracks"] = review_tracks(ctx, result)
    result["track_review_version"] = 1
    path = ctx.output("metadata", "normalized.json")
    write_json(path, result)
    ctx.artifact(path, "SOURCE_METADATA")

    def save(db, job):
        job.analysis = result
        db.execute(delete(MovieTrack).where(MovieTrack.job_id == job.id))
        for t in result["tracks"]:
            db.add(MovieTrack(job_id=job.id, track_id=t["track_id"], kind=t["kind"], info=t))

    return save


def prepare_tracks(ctx):
    with session() as db:
        selection = db.scalar(select(TrackSelection).where(TrackSelection.job_id == ctx.job.id))
    source = ctx.source()
    crop = Crop(**ctx.job.analysis["crop"])
    tracks = {t["track_id"]: t for t in ctx.job.analysis["tracks"]}
    extraction = []
    pending = []
    for track_id in [*selection.audio_track_ids, *selection.subtitle_track_ids]:
        track = tracks[track_id]
        audio = track["kind"] == "audio"
        extension = AUDIO_EXTENSIONS[track["codec_id"]] if audio else "sup"
        cached = track.get("subtitle_source_path") if not audio else None
        path = contained(ctx.workspace, cached) if cached else None
        reuse = path is not None and path.is_file() and path.stat().st_size > 0
        if not reuse:
            path = ctx.output("audio" if audio else "subtitles", f"track-{track_id}.{extension}")
        timestamps = ctx.output("audio", f"track-{track_id}.timestamps.txt") if audio else None
        entry = (track, path, timestamps)
        extraction.append(entry)
        if not reuse:
            pending.append(entry)

    if pending:
        command = [ctx.settings.mkvextract_bin, source, "tracks"]
        command += [f"{track['track_id']}:{path}" for track, path, _ in pending]
        timestamp_specs = [f"{track['track_id']}:{ts}" for track, _, ts in pending if ts is not None]
        if timestamp_specs:
            command += ["timestamps_v2", *timestamp_specs]
        command.append("--gui-mode")
        ctx.progress(0, phase="Extracting selected tracks", tool="mkvextract")
        ctx.run(command, progress_parser=MKVToolNixProgress("mkvextract", "Extracting selected tracks"))
        ctx.progress(99, phase="Checking extracted tracks")

    # Check the whole batch before registering artifacts or processing subtitles.
    for track, path, timestamps in extraction:
        track_id = track["track_id"]
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f"Track {track_id} extraction produced no data")
        if timestamps is not None and (not timestamps.is_file() or not timestamps.stat().st_size):
            raise ValueError(f"Track {track_id} timestamp extraction produced no data")

    prepared = []
    for track, path, timestamps in extraction:
        track_id = track["track_id"]
        audio = track["kind"] == "audio"
        relative = ctx.artifact(path, "AUDIO" if audio else "SUBTITLE_ORIGINAL", info={"track_id": track_id})
        item = {**track, "path": relative}
        if audio:
            item["timestamps"] = ctx.artifact(timestamps, "AUDIO_TIMESTAMPS")
        else:
            ctx.progress(99, phase=f"Processing subtitle track {track_id}")
            if not subtitle_is_resolved(item):
                item = classify_subtitle(ctx, item, path)
            cropped = ctx.output("subtitles", f"track-{track_id}.cropped.sup")
            Sup2supAdapter().crop(ctx, path, cropped, crop, ctx.job.analysis["video"])
            item["path"] = ctx.artifact(cropped, "SUBTITLE_CROPPED", info={"track_id": track_id})
        prepared.append(item)
    name = release_name(
        ctx.job.title,
        ctx.job.year,
        profiles()[ctx.job.analysis_profile]["codec"],
        [t for t in prepared if t["kind"] == "audio"],
    )

    def save(db, job):
        save_prepared_tracks(db, job, prepared)
        job.release_name = name

    return save


def save_prepared_tracks(db, job, prepared):
    detected = {t["track_id"]: t for t in prepared if t["kind"] == "subtitles"}
    tracks = [detected.get(t["track_id"], t) for t in job.analysis.get("tracks", [])]
    job.analysis = {**job.analysis, "prepared_tracks": prepared, "tracks": tracks}
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
    output = ctx.output("encode", "video.mkv")
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
    path = ctx.artifact(output, "ENCODED_VIDEO")
    stats = {}
    for line in ctx.log_path.read_text(errors="replace").splitlines():
        match = re.search(r"frame\s+([IPB]):.*?(?:Avg QP:|QP:)\s*([\d.]+)", line, re.I)
        if match:
            stats[match[1].upper()] = float(match[2])
    return update_analysis({"encoded_path": path, "encoder_average_qp": stats})


def validate(ctx):
    frame_index = {}
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
        metadata = video_metadata(probe(ctx, encoded, "encoded-probe.json"))
        ctx.log("Decoding source and encoded video to validate frame counts and timestamp correspondence")
        source_pts = timeline(ctx.source(), ctx.check)
        index_path = ctx.output("encode", "source-frame-pts.npy")
        np.save(index_path, source_pts)
        frame_index["source_frame_index"] = ctx.artifact(index_path, "SOURCE_FRAME_INDEX")
        ctx.progress(45)
        encoded_pts = timeline(encoded, ctx.check)
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
    metrics = ctx.job.validation["metrics"]
    video_offset_ms = (metrics["source_first_pts"] - metrics["encoded_first_pts"]) * 1000
    command = [
        ctx.settings.mkvmerge_bin,
        "--gui-mode",
        "-o",
        str(output),
        "--title",
        ctx.job.release_name,
        "--track-name",
        f"{video_id}:SMOKE TEST - Source reused" if smoke else f"{video_id}:",
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
    # Older prepared jobs also need content detection before any inherited flags
    # reach a new output. Persist it with the successful mux result.
    prepared = []
    for track in ctx.job.analysis["prepared_tracks"]:
        if track["kind"] == "subtitles" and not subtitle_is_resolved(track):
            track = classify_subtitle(ctx, track, contained(ctx.workspace, track["path"], exists=True))
        prepared.append(track)
    ctx.job.analysis = {**ctx.job.analysis, "prepared_tracks": prepared}
    directory = job_dir(ctx.settings.completed_root, ctx.job.id)
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
            "tracks": ctx.job.analysis["prepared_tracks"],
            "chapters_and_global_tags_from": ctx.job.source_path,
            "argv": command,
        },
    )
    ctx.artifact(plan, "MUX_PLAN")
    ctx.progress(0, phase="Merging final MKV", tool="mkvmerge")
    ctx.run(command, allowed=(0, 1), progress_parser=MKVToolNixProgress("mkvmerge", "Merging final MKV"))
    ctx.progress(99, phase="Checking merged file")
    inspection = json.loads(
        ctx.run([ctx.settings.mkvmerge_bin, "-J", temporary], output=ctx.output("mux", "inspection.json"))
    )
    if len(inspection["tracks"]) != 1 + len(ctx.job.analysis["prepared_tracks"]):
        raise ValueError("Final mux track count does not match mux plan")
    if inspection["container"]["properties"].get("title") != ctx.job.release_name:
        raise ValueError("Final mux title does not match the release name")
    for actual, expected in zip(inspection["tracks"][1:], ctx.job.analysis["prepared_tracks"], strict=True):
        p = actual["properties"]
        if p.get("track_name") != track_name(expected):
            raise ValueError("Final mux track name does not match the naming policy")
        if (
            expected["kind"] == "subtitles"
            and expected.get("subtitle_detection", {}).get("language")
            in (
                "chinese",
                "cantonese",
            )
            and p.get("language_ietf", "").lower() != expected["language"].lower()
        ):
            raise ValueError("Final mux subtitle language does not match content detection")
        for flag, key in (
            ("default_track", "default"),
            ("forced_track", "forced"),
            ("flag_hearing_impaired", "hearing_impaired"),
            ("flag_commentary", "commentary"),
            ("flag_visual_impaired", "visual_impaired"),
        ):
            if bool(p.get(flag, False)) != bool(expected.get(key, False)):
                raise ValueError(f"Final mux did not preserve {key}")
    ctx.check()
    temporary.replace(output)
    ctx.artifact(output, "SMOKE_TEST_MKV" if smoke else "FINAL_MKV", storage="completed")

    def save(db, job):
        save_prepared_tracks(db, job, prepared)
        job.analysis = {**job.analysis, "final_path": str(output.relative_to(ctx.settings.completed_root))}

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


HANDLERS = {
    "analyze": analyze_source,
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
