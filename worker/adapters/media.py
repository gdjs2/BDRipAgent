import json
import re

from shared.naming import track_name
from worker.adapters.handbrake import parse_scan

AUDIO_EXTENSIONS = {
    "A_DTS": "dts",
    "A_AC3": "ac3",
    "A_EAC3": "eac3",
    "A_TRUEHD": "thd",
    "A_FLAC": "flac",
    "A_AAC": "aac",
    "A_PCM/INT/LIT": "wav",
    "A_PCM/INT/BIG": "wav",
    "A_OPUS": "opus",
    "A_VORBIS": "ogg",
    "A_MPEG/L3": "mp3",
    "A_MPEG/L2": "mp2",
}


def probe(ctx, path, filename="ffprobe.json"):
    output = ctx.output("metadata", filename)
    text = ctx.run(
        [
            ctx.settings.ffprobe_bin,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-show_chapters",
            "-of",
            "json",
            path,
        ],
        output=output,
    )
    ctx.artifact(output, "SOURCE_METADATA")
    return json.loads(text)


def video_metadata(probe):
    videos = [s for s in probe["streams"] if s["codec_type"] == "video"]
    if len(videos) != 1:
        raise ValueError("V1 requires exactly one video stream")
    v = videos[0]
    depth = v.get("bits_per_raw_sample")
    if not depth or depth == "0":
        import re

        match = re.search(r"(?:p|rgb)(10|12|14|16)", v.get("pix_fmt", ""))
        depth = int(match[1]) if match else 8
    return {
        "codec": v.get("codec_name"),
        "width": v["width"],
        "height": v["height"],
        "fps": v.get("avg_frame_rate", v.get("r_frame_rate")),
        "bit_depth": int(depth),
        "duration": float(v.get("duration") or probe["format"]["duration"]),
        "start_time": float(v.get("start_time", 0)),
        "sample_aspect_ratio": v.get("sample_aspect_ratio", "1:1"),
        "color_primaries": v.get("color_primaries"),
        "color_transfer": v.get("color_transfer"),
        "color_space": v.get("color_space"),
        "color_range": v.get("color_range"),
        "side_data_list": v.get("side_data_list", []),
        "pix_fmt": v.get("pix_fmt"),
        "field_order": v.get("field_order"),
        "frame_count": int(v["nb_frames"]) if str(v.get("nb_frames", "")).isdigit() else None,
        "bit_rate": int(v.get("bit_rate") or probe["format"].get("bit_rate", 0)),
    }


def analyze(ctx):
    source = ctx.source()
    mkv_path = ctx.output("metadata", "mkvmerge.json")
    mkv = json.loads(ctx.run([ctx.settings.mkvmerge_bin, "-J", source], output=mkv_path))
    ctx.artifact(mkv_path, "SOURCE_METADATA")
    info_path = ctx.output("metadata", "mediainfo.json")
    media_info = json.loads(ctx.run([ctx.settings.mediainfo_bin, "--Output=JSON", source], output=info_path))
    ctx.artifact(info_path, "SOURCE_METADATA")
    ff = probe(ctx, source)
    scan_path = ctx.output("metadata", "handbrake-scan.txt")
    # HandBrake writes JSON to stdout and diagnostics to stderr. Merging them
    # can insert a diagnostic into a JSON string, especially with many tracks.
    scan = ctx.run(
        [
            ctx.settings.handbrake_bin,
            "-i",
            source,
            "--scan",
            "--json",
            "--min-duration",
            "0",
            "--previews",
            "20:0",
        ],
        output=scan_path,
    )
    ctx.artifact(scan_path, "SOURCE_METADATA")
    crop = parse_scan(scan)
    video = video_metadata(ff)
    crop.dimensions(video["width"], video["height"])
    if video["sample_aspect_ratio"] not in ["1:1", "N/A"]:
        raise ValueError("Anamorphic source needs an explicit pixel-aspect policy; V1 accepts square pixels")
    validate_sdr_progressive(video)
    if video.get("color_space") in ["bt2020nc", "bt2020c", "ictcp"]:
        raise ValueError(
            "Wide-gamut sources need an explicit RGB comparison policy; V1 currently accepts SDR BT.709/601"
        )
    from shared.config import profiles

    if profiles()[ctx.job.analysis_profile]["bit_depth"] < video["bit_depth"]:
        raise ValueError(
            "Selected profile would reduce source bit depth; create a job with a suitable profile"
        )
    tracks = []
    for t in mkv["tracks"]:
        if t["type"] not in ["audio", "subtitles"]:
            continue
        p = t.get("properties", {})
        mi = next(
            (
                m
                for m in media_info.get("media", {}).get("track", [])
                if str(m.get("ID")) == str(p.get("number"))
            ),
            {},
        )
        # mkvmerge IDs and ffprobe indexes are different domains; match Matroska track numbers when present.
        streams = [
            s for s in ff["streams"] if str(s.get("id")) in [str(p.get("number")), hex(p.get("number", -1))]
        ]
        s = streams[0] if len(streams) == 1 else {}
        codec_id = p.get("codec_id", "")
        tracks.append(
            {
                "track_id": t["id"],
                "kind": t["type"],
                "codec": t["codec"],
                "codec_id": codec_id,
                "language": p.get("language_ietf", p.get("language", "und")),
                "name": p.get("track_name", ""),
                "default": p.get("default_track", False),
                "forced": p.get("forced_track", False),
                "hearing_impaired": bool(
                    p.get("flag_hearing_impaired", False)
                    or re.search(r"\b(SDH|CC|hearing impaired)\b", p.get("track_name", ""), re.I)
                ),
                "visual_impaired": p.get("flag_visual_impaired", False),
                "commercial_name": mi.get("Format_Commercial_IfAny", ""),
                "format": mi.get("Format", ""),
                "format_profile": mi.get("Format_Profile", ""),
                "format_features": mi.get("Format_AdditionalFeatures", ""),
                "channels": p.get("audio_channels"),
                "channel_layout": s.get("channel_layout") or mi.get("ChannelLayout"),
                "sample_rate": p.get("audio_sampling_frequency"),
                "bit_depth": p.get("audio_bits_per_sample")
                if codec_id
                not in ["A_AC3", "A_EAC3", "A_AAC", "A_OPUS", "A_VORBIS", "A_MPEG/L3", "A_MPEG/L2"]
                else None,
                "bitrate": s.get("bit_rate") or mi.get("BitRate") or p.get("tag_bps"),
                "commentary": bool(
                    p.get("flag_commentary", False) or "commentary" in p.get("track_name", "").lower()
                ),
                "extractable": codec_id in AUDIO_EXTENSIONS or codec_id == "S_HDMV/PGS",
                "source_properties": p,
            }
        )
        tracks[-1]["mux_name"] = track_name(tracks[-1])
    return {
        "video": video,
        "crop": crop.model_dump(),
        "crop_source": "handbrake",
        "tracks": tracks,
        "chapters": ff.get("chapters", []),
        "container": mkv.get("container", {}),
    }


def validate_sdr_progressive(video):
    side_data = json.dumps(video.get("side_data_list", [])).lower()
    if video.get("color_transfer") in ("smpte2084", "arib-std-b67") or any(
        marker in side_data
        for marker in ("dovi", "dolby vision", "hdr10", "mastering display", "content light level")
    ):
        raise ValueError("HDR and Dolby Vision sources are not supported; supply an SDR source")
    if video.get("field_order") not in (None, "unknown", "progressive"):
        raise ValueError("Interlaced sources are not supported; supply a progressive source")
