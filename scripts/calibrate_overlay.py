"""Generate the specified overlay fixture; optionally compare to a supplied reference."""

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops

from worker.pipeline.screenshots import overlay


def yellow_mask(frame):
    rgb = np.asarray(frame.convert("RGB").crop((0, 0, 900, 64)))
    return (rgb[:, :, 0] > 190) & (rgb[:, :, 1] > 190) & (rgb[:, :, 2] < 100)


def compare(reference, rendered):
    expected, actual = yellow_mask(reference), yellow_mask(rendered)
    intersection = np.count_nonzero(expected & actual)
    union = np.count_nonzero(expected | actual)
    return {
        "differing_pixels": int(np.count_nonzero(expected != actual)),
        "yellow_mask_iou": float(intersection / union) if union else 1.0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/calibration/overlay.png"))
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--label", default="Source")
    args = parser.parse_args()
    image = overlay(Image.new("RGB", (1920, 804), "#555555"), 43631, 200484, "B", args.label)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)
    print(f"Rendered exact calibration text: {args.output}")
    if args.reference:
        reference = Image.open(args.reference).convert("RGB")
        if reference.size != image.size:
            raise ValueError("Reference must be 1920×804")

        diff = ImageChops.difference(
            Image.fromarray(yellow_mask(image)), Image.fromarray(yellow_mask(reference))
        )
        diff.save(args.output.with_name("overlay-mask-difference.png"))
        report = compare(reference, image)
        args.output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    else:
        print("No supplied reference: visual calibration remains pending.")


if __name__ == "__main__":
    main()
