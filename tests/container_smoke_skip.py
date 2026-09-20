"""Exercise source-reuse smoke mode end to end in an isolated worker container.

Real source analysis, audio extraction, remux, candidate generation and rendering.
CRF reports and visual choices are fixture data; HandBrake encoding is forbidden.
"""

import json
import os
import subprocess
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient
from PIL import Image, ImageChops

from backend.app.main import app
from shared.config import behavior, get_settings, profiles
from shared.db import Base, engine
from shared.models import CRFResult
from worker.pipeline.stages import HANDLERS
from worker.pipeline.validation import timeline
from worker.runtime import TaskContext
from worker.tasks import execute


def crf_fixture(ctx):
    return lambda db, job: db.add(
        CRFResult(
            job_id=job.id,
            data={
                "profile_snapshot": profiles()[job.analysis_profile],
                "samples": [],
                "predicted": [],
                "statistics": {},
            },
        )
    )


def selection_fixture(ctx):
    from worker.pipeline.screenshots import select_frames

    def agent_fixture(context, timeout):
        candidates = json.loads((context.workspace / context.job.analysis["candidate_index"]).read_text())[
            "candidates"
        ]
        assert len(candidates) >= 15
        best = [candidates[round(i * (len(candidates) - 1) / 14)] for i in range(15)]
        choices = [
            {
                "candidate_id": c["candidate_id"],
                "score": 1 - i * 0.01,
                "category": "representative" if i < 9 else "encode_challenging",
                "reason": "Explicit test fixture choice",
                "shot_type": "close_up",
                "subject": f"fixture {i}",
                "character_visible": True,
            }
            for i, c in enumerate(best)
        ]
        return {
            "selected": choices,
            "shortlisted_ids": [c["candidate_id"] for c in candidates],
            "shortlisted_choices": choices,
            "runs": [],
            "thread_id": "fixture",
        }

    with patch("worker.pipeline.screenshots.select_screenshots", agent_fixture):
        return select_frames(ctx)


def main():
    settings = get_settings()
    assert settings.database_url.startswith("sqlite:////tmp/")
    for folder in (
        settings.source_root,
        settings.workspace_root,
        settings.completed_root,
        settings.cache_root,
    ):
        assert str(folder).startswith("/tmp/")
        folder.mkdir(parents=True, exist_ok=True)
    config = behavior()
    config.update(candidate_count=20, duplicate_hash_distance=0)
    settings.config_path = settings.cache_root / "smoke-config.yaml"
    settings.config_path.write_text(yaml.safe_dump(config))
    Base.metadata.create_all(engine())
    source = settings.source_root / "SmokeFixture.mkv"
    # Audio comes first, video starts at 125 ms, and the image has 16-pixel margins.
    subprocess.run(
        [
            settings.ffmpeg_bin,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x328:rate=24:duration=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=12.2",
            "-map",
            "1:a",
            "-map",
            "0:v",
            "-vf",
            "pad=640:360:0:16,setpts=PTS+0.125/TB",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-bf",
            "3",
            "-crf",
            "15",
            "-c:a",
            "ac3",
            "-threads",
            "2",
            source,
        ],
        check=True,
    )
    original = (source.stat().st_size, source.stat().st_mtime_ns)
    HANDLERS["crf_analysis"] = crf_fixture
    HANDLERS["select_screenshots"] = selection_fixture
    run = TaskContext.run

    def no_encoder(ctx, command, **kwargs):
        if command[0] == settings.handbrake_bin:
            assert "--scan" in command, "Smoke mode must never run HandBrake encoding"
        assert command[0] != settings.crf_studio_bin
        return run(ctx, command, **kwargs)

    TaskContext.run = no_encoder
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {settings.api_token}"
        for profile in ("x264-live", "x265-live"):
            result = client.post(
                "/api/jobs",
                json={
                    "source_path": source.name,
                    "title": "Smoke fixture",
                    "year": 2026,
                    "analysis_profile": profile,
                    "screenshot_policy": {
                        "decoder": os.environ.get("SMOKE_SCREENSHOT_DECODER", "cpu"),
                        "count": 7,
                        "representative": 4,
                        "encode_challenging": 3,
                        "min_spacing_seconds": 0,
                        "min_timeline_bins": 1,
                        "max_per_scene": 20,
                    },
                },
            )
            assert result.status_code == 201, result.text
            job_id = result.json()["id"]
            for _ in range(20):
                job = client.get(f"/api/jobs/{job_id}").json()
                print(profile, job["state"], flush=True)
                if job["state"] == "WAITING_FOR_RELEASE_DETAILS":
                    break
                if job["state"] == "WAITING_FOR_TRACK_SELECTION":
                    response = client.post(
                        f"/api/jobs/{job_id}/tracks/selection",
                        json={
                            "audio_track_ids": [t["track_id"] for t in job["tracks"] if t["kind"] == "audio"],
                            "subtitle_track_ids": [],
                        },
                    )
                    assert response.status_code == 200, response.text
                elif job["state"] == "WAITING_FOR_ENCODE_SELECTION":
                    response = client.post(f"/api/jobs/{job_id}/smoke-test", json={})
                    assert response.status_code == 200, response.text
                elif job["state"] == "WAITING_FOR_SCREENSHOT_SELECTION":
                    shots = client.get(f"/api/jobs/{job_id}/screenshots").json()
                    assert shots["recommended"] == 15 and shots["final"] == 0
                    reviews = [
                        a for a in job["artifacts"] if a["artifact_type"].startswith("SCREENSHOT_REVIEW_")
                    ]
                    assert len(reviews) == 30
                    assert all(a["info"]["picture_type"] == "B" for a in reviews)
                    assert not any(t["status"] == "QUEUED" for t in job["tasks"])
                    for shot in shots["items"]:
                        if shot["shortlisted"]:
                            assert (settings.workspace_root / job_id / shot["info"]["thumbnail"]).is_file()
                    best = sorted(
                        [s for s in shots["items"] if s["info"].get("recommendation_rank")],
                        key=lambda s: s["info"]["recommendation_rank"],
                    )
                    count = int(os.environ.get("SMOKE_FINAL_COUNT", "7"))
                    response = client.post(
                        f"/api/jobs/{job_id}/screenshots/selection",
                        json={"candidate_ids": [s["candidate_id"] for s in best[:count]]},
                    )
                    assert response.status_code == 200, response.text
                else:
                    task = next((t for t in job["tasks"] if t["status"] == "QUEUED"), None)
                    if task is None:
                        failures = [t for t in job["tasks"] if t["status"] == "FAILED"]
                        for failed in failures:
                            print((settings.workspace_root / job_id / failed["log_path"]).read_text()[-6000:])
                        raise AssertionError(failures)
                    execute(task["id"])
            assert job["state"] == "WAITING_FOR_RELEASE_DETAILS"
            decoder = job["analysis"]["screenshot_scan_decoder"]
            expected_decoder = os.environ.get("EXPECT_SCREENSHOT_DECODER", "cpu")
            assert decoder["decoder"] == expected_decoder, decoder
            assert decoder["decoder_fallback"] == (
                os.environ.get("SMOKE_SCREENSHOT_DECODER", "cpu") != expected_decoder
            )
            assert job["analysis"]["smoke_video_track_id"] == 1
            assert job["validation"]["valid"] is None and job["validation"]["skipped"] is True
            assert "encoded_path" not in job["analysis"]
            assert not any(a["artifact_type"] in ("ENCODED_VIDEO", "FINAL_MKV") for a in job["artifacts"])
            final = next(a for a in job["artifacts"] if a["artifact_type"] == "SMOKE_TEST_MKV")
            final_path = settings.completed_root / final["path"]
            assert final_path.name.startswith("SMOKE-TEST.")
            assert len(timeline(final_path)) == 288
            assert abs(timeline(final_path)[0] - timeline(source)[0]) < 0.001
            images = [
                a
                for a in job["artifacts"]
                if a["artifact_type"] in ("SCREENSHOT_SOURCE", "SCREENSHOT_ENCODE")
            ]
            assert len(images) == 2 * int(os.environ.get("SMOKE_FINAL_COUNT", "7")) and all(
                a["info"]["smoke_test"] for a in images
            )
            assert all(a["info"]["picture_type"] == "B" for a in images)
            crop = job["analysis"]["crop"]
            assert crop["top"] > 0 and crop["bottom"] > 0
            expected_size = (640 - crop["left"] - crop["right"], 360 - crop["top"] - crop["bottom"])
            shots = client.get(f"/api/jobs/{job_id}/screenshots").json()["items"]
            for shot in (s for s in shots if s["selected"]):
                assert shot["info"]["b_frames_verified"]
                assert shot["info"]["picture_type"] == shot["info"]["encoded_picture_type"] == "B"
                paths = shot["info"]["comparisons"]
                src = Image.open(settings.workspace_root / job_id / paths["src"])
                comparison = Image.open(settings.workspace_root / job_id / paths["encode"])
                assert src.size == comparison.size == expected_size
                assert "SMOKE-TEST." in paths["encode"]
                assert (
                    ImageChops.difference(
                        src.crop((0, 65, *expected_size)), comparison.crop((0, 65, *expected_size))
                    ).getbbox()
                    is None
                )
            assert (source.stat().st_size, source.stat().st_mtime_ns) == original
            print(
                f"PASS {profile}: no encoding, real audio/remux, human review of 15 pairs, {len(images)} final marked PNGs",
                flush=True,
            )


if __name__ == "__main__":
    main()
