import json
import re

from pydantic import BaseModel, Field

from shared.encoding import EncodeTarget


class Crop(BaseModel):
    top: int = Field(ge=0)
    bottom: int = Field(ge=0)
    left: int = Field(ge=0)
    right: int = Field(ge=0)

    def argument(self):
        return f"{self.top}:{self.bottom}:{self.left}:{self.right}"

    def dimensions(self, width, height):
        w, h = width - self.left - self.right, height - self.top - self.bottom
        if w <= 0 or h <= 0 or w % 2 or h % 2:
            raise ValueError("HandBrake crop must leave positive, even dimensions")
        return w, h


def parse_scan(text):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start() :])
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("TitleList"):
            title = next((t for t in obj["TitleList"] if t.get("Index") == 1), obj["TitleList"][0])
            crop = title.get("Crop")
            if isinstance(crop, list) and len(crop) == 4:
                return Crop(**dict(zip(["top", "bottom", "left", "right"], crop, strict=True)))
    # The title summary uses "autocrop:", while scan diagnostics use "autocrop =".
    matches = re.findall(r"\bautocrop\s*[:=]\s*(\d+)/(\d+)/(\d+)/(\d+)\b", text)
    if len(set(matches)) == 1:
        return Crop(**dict(zip(["top", "bottom", "left", "right"], map(int, matches[0]), strict=True)))
    raise ValueError("HandBrake scan did not return an unambiguous crop")


def encode_command(
    binary, source, output, video, crop, profile, crf=None, *, rate_control="crf", bitrate_kbps=None
):
    target = EncodeTarget(rate_control=rate_control, crf=crf, bitrate_kbps=bitrate_kbps)
    width, height = crop.dimensions(video["width"], video["height"])
    command = [
        binary,
        "-i",
        str(source),
        "-o",
        str(output),
        "-f",
        "av_mkv",
        "-e",
        profile["encoder"],
        "--encoder-preset",
        profile["preset"],
        "--crop",
        crop.argument(),
        "--width",
        str(width),
        "--height",
        str(height),
        "--non-anamorphic",
        "--modulus",
        "2",
        "--vfr",
        "--audio",
        "none",
        "--subtitle",
        "none",
        "--no-markers",
        "--no-comb-detect",
        "--no-deinterlace",
        "--no-decomb",
        "--no-detelecine",
        "--no-hqdn3d",
        "--no-nlmeans",
        "--no-unsharp",
        "--no-lapsharp",
    ]
    if target.rate_control == "bitrate":
        command += ["--vb", str(target.bitrate_kbps), "--two-pass", "--no-turbo"]
    else:
        command += ["-q", str(target.crf), "--no-two-pass"]
    if profile.get("tune"):
        command += ["--encoder-tune", profile["tune"]]
    if profile.get("video_profile"):
        command += ["--encoder-profile", profile["video_profile"]]
    if profile.get("level"):
        command += ["--encoder-level", str(profile["level"])]
    if profile.get("extra_options"):
        command += ["--encopts", profile["extra_options"]]
    return command


def parse_progress(text):
    matches = list(re.finditer(r"Encoding:.*?(\d+(?:\.\d+)?)\s*%[^\r\n]*", text))
    if not matches:
        return None
    match = matches[-1]
    fps = re.search(r"([\d.]+) fps", match[0])
    eta = re.search(r"ETA\s+(\d+)h(\d+)m(\d+)s", match[0])
    result = {"percentage": float(match[1])}
    task = re.search(r"task\s+(\d+)\s+of\s+(\d+)", match[0])
    if task:
        current, total = map(int, task.groups())
        if total > 1 and 1 <= current <= total:
            result.update(
                percentage=100 * ((current - 1) + float(match[1]) / 100) / total,
                pass_number=current,
                pass_count=total,
                pass_percentage=float(match[1]),
            )
    if fps:
        result["fps"] = float(fps[1])
    if eta:
        # HandBrake reports the current pass's ETA, not time remaining for both.
        result["pass_eta_seconds" if "pass_count" in result else "eta_seconds"] = (
            int(eta[1]) * 3600 + int(eta[2]) * 60 + int(eta[3])
        )
    if result["percentage"] >= 100:
        result.update(indeterminate=True, phase="Finalizing encoded file")
    return result
