import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
import pytest

from worker.adapters.frame_index import frame_number, source_frame_index
from worker.adapters.screenshot_decoder import candidate_decoder, window_frames
from worker.pipeline.screenshots import generate, sampling_windows
from worker.pipeline.validation import timeline


@pytest.fixture(scope="module")
def sampling_source(tmp_path_factory):
    root = tmp_path_factory.mktemp("sparse-video")
    source = root / "source.mkv"
    # A nonzero video offset, audio as track zero, B-frames, and frequent seeks.
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=60",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x192:rate=24:duration=60",
            "-map",
            "0:a",
            "-map",
            "1:v",
            "-vf",
            "setpts=PTS+0.125/TB",
            "-c:a",
            "flac",
            "-c:v",
            "libx264",
            "-g",
            "24",
            "-bf",
            "3",
            "-threads",
            "1",
            str(source),
        ],
        check=True,
    )
    return source


def make_context(tmp_path, source, points):
    def output(category, name):
        path = tmp_path / category / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    path = output("metadata", "pts.npy")
    np.save(path, points)
    progress = []
    return SimpleNamespace(
        workspace=tmp_path,
        source=lambda: source,
        check=lambda: None,
        log=lambda message: None,
        output=output,
        artifact=lambda path, *args, **kwargs: str(path.relative_to(tmp_path)),
        progress=lambda value, **kwargs: progress.append({"value": value, **kwargs}),
        reports=progress,
        job=SimpleNamespace(
            screenshot_policy={"decoder": "cpu", "count": 7},
            validation={
                "metrics": {
                    "source_first_pts": points[0],
                    "encoded_first_pts": points[0],
                    "source_frames": len(points),
                }
            },
            analysis={
                "source_frame_index": str(path.relative_to(tmp_path)),
                "crop": {"top": 0, "left": 0, "right": 0, "bottom": 0},
                "video": {"height": 192},
                "smoke_test": True,
            },
        ),
    )


def test_sparse_scan_stops_at_target_and_preserves_exact_b_frame_numbers(
    tmp_path, sampling_source, monkeypatch
):
    from shared.config import behavior
    from worker.pipeline import screenshots

    points = timeline(sampling_source)
    ctx = make_context(tmp_path, sampling_source, points)
    config = {**behavior(), "candidate_count": 8, "duplicate_hash_distance": 0}
    monkeypatch.setattr(screenshots, "behavior", lambda: config)
    generate(ctx)
    index = json.loads((tmp_path / "screenshots/candidates.json").read_text())
    candidates, stats = index["candidates"], index["decoder"]
    assert len(candidates) == 8
    assert stats["sampled_windows"] == 8  # The refill budget is unused once the target is reached.
    assert stats["decoded_frames"] < len(points) / 2
    assert candidates[0]["timeline_seconds"] < 10 and candidates[-1]["timeline_seconds"] > 50
    with av.open(str(sampling_source)) as container:
        frames = list(container.decode(video=0))
    for candidate in candidates:
        index = candidate["source_frame_number"]
        assert candidate["source_total_frames"] == len(frames)
        assert candidate["source_pts_seconds"] == points[index]
        assert int(frames[index].pict_type) == 3
        assert candidate["picture_type"] == candidate["encoded_picture_type"] == "B"
        assert candidate["b_frames_verified"]
    values = [r["value"] for r in ctx.reports]
    assert values == sorted(values)


def test_seek_windows_reset_the_decoder_for_backward_and_forward_seeks(tmp_path, sampling_source):
    points = timeline(sampling_source)
    ctx = make_context(tmp_path, sampling_source, points)
    stats = {"decoded_frames": 0}
    with candidate_decoder(ctx) as (container, _, info):
        assert info["decoder"] == "cpu"
        for start in (45.125, 5.125, 30.125):
            frames = list(window_frames(ctx, container, start, start + 1, stats))
            assert frames
            assert all(start <= float(f.pts * f.time_base) <= start + 1 for f in frames)
            assert all(
                points[frame_number(points, float(f.pts * f.time_base))] == float(f.pts * f.time_base)
                for f in frames
            )
    assert stats["decoded_frames"] < 24 * 9


def test_index_uses_real_pts_including_variable_rate_and_rejects_mismatch(tmp_path):
    points = np.array([0.125, 0.167, 0.25, 0.375, 0.417])
    ctx = make_context(tmp_path, Path("unused.mkv"), points)
    loaded, _ = source_frame_index(ctx)
    assert [frame_number(loaded, t) for t in points] == list(range(len(points)))
    with pytest.raises(ValueError, match="No indexed"):
        frame_number(loaded, 0.3)
    ctx.job.validation["metrics"]["source_frames"] += 1
    with pytest.raises(ValueError, match="frame count"):
        source_frame_index(ctx)


def test_sampler_refills_each_region_with_bounded_different_windows():
    windows = list(sampling_windows(6000, 100, 4, 3))
    assert len(windows) == 400
    assert {b for b, *_ in windows[:100]} == set(range(100))
    assert all(0 <= start <= anchor < end <= 6000 for _, start, anchor, end in windows)
    assert len({anchor for _, _, anchor, _ in windows}) == 400
