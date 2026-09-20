"""Isolated PGS rendering and OCR process; cancelled with the task's process group."""

import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps


def script_forms(directory):
    tables = []
    for name in ("STCharacters", "TSCharacters"):
        output = directory / f"{name}.txt"
        subprocess.run(
            [
                "opencc_dict",
                "-i",
                f"/usr/share/opencc/{name}.ocd2",
                "-o",
                str(output),
                "-f",
                "ocd2",
                "-t",
                "text",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        tables.append(dict(line.split("\t", 1) for line in output.read_text().splitlines() if "\t" in line))
        output.unlink()
    simplified, traditional = tables
    # Exclude reciprocal/variant keys and ambiguous one-to-many mappings.
    return (
        {k for k, v in simplified.items() if len(k) == len(v) == 1 and k != v and k not in traditional},
        {k for k, v in traditional.items() if len(k) == 1 and k != v and k not in simplified},
    )


def ocr(path, languages):
    output = subprocess.run(
        ["tesseract", str(path), "stdout", "-l", languages, "--psm", "6", "tsv"],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},
    ).stdout
    lines, words = {}, []
    for row in csv.DictReader(io.StringIO(output), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row.get("level") != "5" or not row.get("text", "").strip():
            continue
        token = row["text"].strip()
        confidence = float(row["conf"])
        if confidence < 0:
            continue
        key = row["block_num"], row["par_num"], row["line_num"]
        lines.setdefault(key, []).append(token)
        words.append((token, confidence))
    weight = sum(len(w) for w, _ in words)
    return {
        "text": "\n".join(" ".join(line) for line in lines.values()),
        "confidence": round(sum(len(w) * c for w, c in words) / weight, 2) if weight else 0,
    }


def render(cue):
    from sup2sup.pgs.renderer import render_tiles

    tiles = render_tiles(cue)
    if not tiles:
        return None
    left, top = min(t.x for t in tiles), min(t.y for t in tiles)
    width = max(t.x + t.width for t in tiles) - left
    height = max(t.y + t.height for t in tiles) - top
    canvas = Image.new("RGBA", (width, height))
    for tile in tiles:
        canvas.alpha_composite(
            Image.frombytes("RGBA", (tile.width, tile.height), tile.rgba), (tile.x - left, tile.y - top)
        )
    bounds = canvas.getbbox()
    if not bounds:
        return None
    canvas = canvas.crop(bounds)
    background = Image.new("RGBA", canvas.size, "black")
    background.alpha_composite(canvas)
    return ImageOps.expand(background.convert("RGB"), border=16, fill="black")


def sheets(directory, samples):
    names = []
    for offset in range(0, len(samples), 8):
        rows = []
        for sample in samples[offset : offset + 8]:
            with Image.open(directory / sample["image"]) as raw:
                picture = raw.copy()
            picture.thumbnail((1500, 300))
            rows.append((sample, picture))
        sheet = Image.new("RGB", (1560, sum(p.height + 50 for _, p in rows)), "#202020")
        draw, y = ImageDraw.Draw(sheet), 0
        for sample, picture in rows:
            draw.text((20, y + 8), f"Cue {sample['id']} | {sample['seconds']:.2f}s", fill="white")
            sheet.paste(picture, (20, y + 30))
            y += picture.height + 50
        name = f"sheet-{offset // 8:02d}.jpg"
        sheet.save(directory / name, quality=95)
        names.append(name)
    return names


def scan(source, directory, limit, language):
    from sup2sup.pgs.parser import parse_sup

    document = parse_sup(source.read_bytes())
    # Animation/palette updates of the same text must not inflate evidence.
    unique, seen = [], set()
    for number, cue in enumerate(document.cues):
        digest = hashlib.sha256()
        for placement in cue.placements:
            bitmap = placement.bitmap
            digest.update(f"{bitmap.width},{bitmap.height},{placement.source_rect}".encode())
            digest.update(bitmap.indices)
        fingerprint = digest.digest()
        if fingerprint not in seen:
            unique.append((number + 1, cue))
            seen.add(fingerprint)
    if not unique:
        raise ValueError("Subtitle track has no visible cues to classify")
    count = min(limit, len(unique))
    indices = [round(i * (len(unique) - 1) / max(1, count - 1)) for i in range(count)]
    simple, traditional = script_forms(directory)
    base_language = language.lower().split("-")[0]
    primary = (
        "jpn+eng"
        if base_language in ("ja", "jpn")
        else ("kor+eng" if base_language in ("ko", "kor") else "chi_tra+chi_sim+eng")
    )
    samples = []
    for done, index in enumerate(indices, 1):
        number, cue = unique[index]
        picture = render(cue)
        if picture is None:
            continue
        path = directory / f"cue-{number:06d}.png"
        picture.save(path)
        # OCR expects dark text on a light background. Keep the original image
        # separately for visual review, avoiding OCR preprocessing artifacts.
        ocr_path = directory / "ocr-input.png"
        ImageOps.invert(picture.convert("RGB")).save(ocr_path)
        readings = [ocr(ocr_path, primary)]
        if any(c in simple or c in traditional for c in readings[0]["text"]):
            readings.append(ocr(ocr_path, "chi_sim+chi_tra+eng"))
        best = max(readings, key=lambda value: value["confidence"])

        def preference(reading):
            s, t = len(set(reading["text"]) & simple), len(set(reading["text"]) & traditional)
            return "simplified" if s > t else "traditional" if t > s else "unknown"

        preferences = {preference(r) for r in readings} - {"unknown"}
        samples.append(
            {
                "id": number,
                "seconds": cue.start_pts / 90000,
                "image": path.name,
                **best,
                "readings": readings,
                "simplified_chars": "".join(sorted(set(best["text"]) & simple)),
                "traditional_chars": "".join(sorted(set(best["text"]) & traditional)),
                "script_disagreement": len(preferences) > 1,
            }
        )
        print(f"SUBTITLE_OCR {done}/{count}", flush=True)
    (directory / "ocr-input.png").unlink(missing_ok=True)
    report = {
        "schema_version": 1,
        "total_cues": len(document.cues),
        "unique_cues": len(unique),
        "sampled_cues": len(samples),
        "sampling": "uniform across distinct bitmap cues",
        "samples": samples,
        "contact_sheets": sheets(directory, samples),
    }
    (directory / "ocr.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--limit", type=int, default=96)
    parser.add_argument("--language", default="und")
    parser.add_argument("--sup2sup-source", type=Path, required=True)
    args = parser.parse_args()
    if not 8 <= args.limit <= 192:
        parser.error("Subtitle sample limit must be between 8 and 192")
    args.directory.mkdir(parents=True, exist_ok=True)
    # Only import the pure-Python PGS decoder from the pinned Sup2Sup checkout;
    # its unrelated PyAV/GUI environment remains isolated.
    sys.path.insert(0, str(args.sup2sup_source))
    scan(args.source, args.directory, args.limit, args.language)
