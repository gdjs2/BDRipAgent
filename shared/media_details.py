"""Track-specific metadata and the encoder summary used in release text files."""

import math
import re


def positive_number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def video_bitrate(probe=None, media_info=None, mkv=None):
    videos = [s for s in (probe or {}).get("streams", []) if s.get("codec_type") == "video"]
    stream = videos[0] if len(videos) == 1 else {}
    tags = {key.upper(): value for key, value in stream.get("tags", {}).items()}
    candidates = [stream.get("bit_rate"), tags.get("BPS"), tags.get("BPS-ENG")]
    mi = [t for t in (media_info or {}).get("media", {}).get("track", []) if t.get("@type") == "Video"]
    if len(mi) == 1:
        candidates.append(mi[0].get("BitRate"))
    tracks = [t for t in (mkv or {}).get("tracks", []) if t.get("type") == "video"]
    if len(tracks) == 1:
        candidates.append(tracks[0].get("properties", {}).get("tag_bps"))
    for value in candidates:
        if number := positive_number(value):
            return round(number)
    # Never substitute container bitrate: it includes all audio and subtitles.
    return None


def encoder_summary(text, codec):
    prefix = r"x265 \[info\]:" if codec == "x265" else r"(?:x264 \[info\]:|\[libx264[^]]*\])"
    patterns = (
        [
            r"Main .* profile, Level-",
            r"frame I:",
            r"frame P:",
            r"frame B:",
            r"Weighted P-Frames:",
            r"Weighted B-Frames:",
            r"consecutive B-frames:",
        ]
        if codec == "x265"
        else [r"profile ", r"frame I:", r"frame P:", r"frame B:", r"consecutive B-frames:"]
    )
    found = {}
    for line in text.splitlines():
        for pattern in patterns:
            if re.search(prefix + r"\s*" + pattern, line, re.I):
                found[pattern] = re.sub(r"^.*?(?=(?:x26[45]|\[libx264))", "", line).strip()
    return "\n".join(found[p] for p in patterns if p in found)
