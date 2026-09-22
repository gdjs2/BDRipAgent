"""Track labels, source descriptions and WiKi names (BDRip_Scripts 529552b7)."""

import re
import unicodedata

from langcodes import Language


def source_description(name: str) -> str:
    """Reuse release/pipeline.py's source_description formatting for an entered name.

    The upstream helper takes root.name; this API accepts that name directly so
    a manually entered description containing a slash is not treated as a path.
    Keep in sync with BDRip_Scripts revision 529552b7 (native parity test).
    """
    match = re.search(r"\.\d{4}\.(.+)$", name)
    value = match.group(1) if match else name
    value = value.replace("@", "-").replace(".", " ")
    value = re.sub(r"\b([257]) ([01])\b", r"\1.\2", value)
    return re.sub(r"\s+", " ", value).strip()


def language_name(code):
    code = code or "und"
    if code == "und":
        return "Undetermined"
    try:
        language = Language.get(code)
        return language.display_name("en") if language.is_valid() else code
    except ValueError:
        return code


def audio_description(track):
    return " ".join(
        str(track.get(k) or "")
        for k in (
            "codec",
            "codec_id",
            "commercial_name",
            "format",
            "format_profile",
            "format_features",
            "name",
        )
    ).casefold()


def audio_format(track):
    name = audio_description(track)
    if "atmos" in name:
        return "Dolby Atmos"
    if "dts:x" in name or "dts-x" in name:
        return "DTS:X"
    if "dts" in name:
        if any(s in name for s in ("master audio", "xll", "dts-ma", "dts-hd ma")):
            return "DTS-MA"
        return "DTS-HD HRA" if "high resolution" in name or "hra" in name else "DTS"
    if "truehd" in name or "true hd" in name or "a_truehd" in name:
        return "Dolby TrueHD"
    if any(s in name for s in ("e-ac-3", "eac3", "digital plus")):
        return "Dolby Digital Plus"
    if any(s in name for s in ("ac-3", "a_ac3", "dolby digital")):
        return "Dolby Digital"
    for token, display in (
        ("pcm", "LPCM"),
        ("flac", "FLAC"),
        ("aac", "AAC"),
        ("opus", "Opus"),
        ("vorbis", "Vorbis"),
        ("mpeg/l3", "MP3"),
        ("mpeg/l2", "MP2"),
    ):
        if token in name:
            return display
    return track.get("codec") or "Unknown"


def audio_channels(track):
    layout = str(track.get("channel_layout") or "")
    match = re.fullmatch(r"(\d+\.\d+(?:\.\d+)?)(?:\([^)]*\))?", layout)
    if match:
        return match[1]
    count = int(track.get("channels") or 0)
    if not count:
        return "Unknown"
    if layout:
        lfe = sum(token.upper().startswith("LFE") for token in re.split(r"[\s+]+", layout))
        if lfe:
            return f"{count - lfe}.{lfe}"
        if layout.lower() in ("mono", "stereo") or " " in layout:
            return f"{count}.0"
    return f"{count - 1}.1" if count in (6, 8) else f"{count}.0"


def track_name(track):
    return track.get("name_override") or automatic_track_name(track)


def automatic_track_name(track):
    code = (track.get("language") or "und").lower().split("-")
    label = language_name(track.get("language"))
    if track["kind"] == "subtitles":
        if code[0] == "yue":
            label = "Cantonese"
            if "hans" in code or "hant" in code:
                label += " (Simplified)" if "hans" in code else " (Traditional)"
        elif code[0] in ("zh", "zho", "chi", "cmn"):
            label = (
                "Simplified Chinese"
                if "hans" in code
                else ("Traditional Chinese" if "hant" in code else "Chinese")
            )
    parts = [label]
    if track["kind"] == "audio":
        parts += [audio_format(track), audio_channels(track)]
    else:
        parts.append(
            {
                "S_HDMV/PGS": "PGS",
                "S_TEXT/UTF8": "SRT",
                "S_TEXT/ASS": "ASS",
                "S_TEXT/SSA": "SSA",
                "S_VOBSUB": "VobSub",
            }.get(track.get("codec_id"), track.get("codec", "Unknown"))
        )
        if track.get("hearing_impaired"):
            parts.append("SDH")
        if track.get("forced"):
            parts.append("Forced")
    return " ".join(parts)


def release_audio_token(tracks):
    """Match upstream's core-first selection and omission of plain Dolby Digital."""
    if not tracks:
        return ""

    def core(t):
        name = audio_description(t)
        return (
            audio_format(t) in ("LPCM", "Dolby Digital", "Dolby Digital Plus", "AAC")
            or ("dts" in name and not any(s in name for s in ("dts-hd", "dts:x", "dts-x", "xll", "dts-ma")))
            or ("atmos" in name and any(s in name for s in ("e-ac-3", "eac3", "digital plus")))
        )

    cores = [t for t in tracks if core(t)]
    if cores:
        t = max(cores, key=lambda t: (int(t.get("channels") or 0), int(t.get("bitrate") or 0)))
        return {
            "LPCM": "LPCM",
            "Dolby Digital": "",
            "Dolby Digital Plus": "DDP",
            "Dolby Atmos": "DDP",
            "AAC": "AAC",
        }.get(audio_format(t), "DTS")

    def priority(t):
        name = audio_description(t)
        return (
            5 if "atmos" in name else 4 if "truehd" in name else 3 if "dts-hd" in name else 1,
            int(t.get("channels") or 0),
        )

    t = max(tracks, key=priority)
    fmt, layout = audio_format(t), audio_channels(t)
    layout = "" if layout == "Unknown" else layout
    if fmt == "DTS-MA":
        return f"DTS.MA{layout}"
    codec = {
        "Dolby Atmos": "Atmos.TrueHD",
        "Dolby TrueHD": "TrueHD",
        "DTS:X": "DTS-X.DTS-HD.MA",
        "DTS-HD HRA": "DTS-HD",
    }.get(fmt, fmt)
    return ".".join(p for p in (codec, layout) if p)


def release_name(title, year, codec, audio_tracks=()):
    if year is None:
        raise ValueError("A release year is required for WiKi naming")
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    title = re.sub(r"[^A-Za-z0-9]+", ".", ascii_title).strip(".")
    if not title:
        raise ValueError("Supply a romanized movie title for the WiKi release name")
    if codec not in ("x264", "x265"):
        raise ValueError("WiKi releases require x264 or x265")
    name = (
        ".".join(
            p
            for p in (
                title,
                str(year),
                "1080p",
                "BluRay",
                codec,
                "10bit" if codec == "x265" else "",
                release_audio_token(audio_tracks),
            )
            if p
        )
        + "-WiKi"
    )
    if len(name.encode("utf-8")) > 240:
        raise ValueError("Release name is too long for a portable filename")
    return name
