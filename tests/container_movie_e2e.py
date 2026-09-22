"""Opt-in real movie E2E acceptance run in an isolated database/storage root.

Uses actual media tools, local transcription and the configured review agent.
Creates clearly marked test releases without uploading images or contacting a tracker.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import queue
from backend.app.main import app
from shared.config import get_settings
from shared.db import Base, engine, session
from shared.tracks import FLAG_NAMES
from worker.tasks import execute


def main():
    settings = get_settings()
    if "/review/" not in str(settings.workspace_root):
        raise RuntimeError("This harness requires isolated /review storage")
    Base.metadata.create_all(engine())
    results = Path("/review/results")
    results.mkdir(exist_ok=True)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=4) as executor:
        client.headers["Authorization"] = f"Bearer {settings.api_token}"

        def request(method, path, data=None):
            response = client.request(method, "/api" + path, json=data)
            assert response.is_success, f"{method} {path}: {response.status_code} {response.text}"
            return response.json()

        jobs = request("GET", "/jobs")
        if not jobs:
            jobs = request(
                "POST",
                "/jobs/pair",
                {
                    "source_path": "They.Will.Kill.You.E2E.excerpt.mkv",
                    "title": "E2E TEST They Will Kill You",
                    "year": 2026,
                    "analysis_profile": "x264-review",
                    "second_profile": "x265-review",
                    "audio_review_max_rounds": 2,
                    "screenshot_policy": {
                        "decoder": "cpu",
                        "count": 7,
                        "representative": 4,
                        "encode_challenging": 3,
                        "min_spacing_seconds": 1,
                        "min_timeline_bins": 1,
                        "max_per_scene": 20,
                        "policy": "Select sharp character and texture frames from this short test excerpt. Avoid fades and blur. Follow the spacing rules.",
                    },
                },
            )
        ids = [job["id"] for job in jobs]
        (results / "job-ids.json").write_text(json.dumps(ids))
        pending = {}
        last = {}
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            jobs = [request("GET", f"/jobs/{job_id}") for job_id in ids]
            for job in jobs:
                key = (job["state"], tuple((t["id"], t["status"]) for t in job["tasks"]))
                if last.get(job["id"]) != key:
                    print(
                        job["analysis_profile"],
                        job["state"],
                        [(t["type"], t["status"]) for t in job["tasks"]],
                        flush=True,
                    )
                    last[job["id"]] = key
                    (results / f"{job['analysis_profile']}.json").write_text(json.dumps(job, indent=2))
                failed = [
                    t
                    for t in job["tasks"]
                    if t["status"] == "FAILED" and not any(n.get("retry_of") == t["id"] for n in job["tasks"])
                ]
                if failed:
                    raise AssertionError(
                        json.dumps(
                            [
                                {"type": t["type"], "error": t.get("error_message"), "log": t["log_path"]}
                                for t in failed
                            ]
                        )
                    )
                path = f"/jobs/{job['id']}"
                if job["state"] != "ANALYZING_SOURCE" and not job["analysis"].get("release_details"):
                    request(
                        "PATCH",
                        path + "/release",
                        {
                            "chinese_name": "他们要杀你（流程测试片段）",
                            "source": "1080p Blu-ray AVC — local 45-second E2E excerpt",
                            "extra_description": "LOCAL ACCEPTANCE TEST ONLY — not a complete film release",
                            "tracker": "https://example.invalid/announce",
                            "upload_screenshots": False,
                        },
                    )
                if job["state"] == "WAITING_FOR_ENCODE_SELECTION":
                    request(
                        "POST",
                        path + "/encode-selection",
                        {
                            "codec": job["analysis_profile"].split("-")[0],
                            "profile": job["analysis_profile"],
                            "rate_control": "bitrate",
                            "bitrate_kbps": 6000,
                        },
                    )
                if job["track_analysis_complete"] and not job["track_selection"] and job["encode_config"]:
                    chosen = [t for t in job["tracks"] if t["info"].get("extractable")]
                    # Explicit test operator choices for uncertain flags; retain the agent's findings.
                    request(
                        "POST",
                        path + "/tracks/selection",
                        {
                            "audio_track_ids": [t["track_id"] for t in chosen if t["kind"] == "audio"],
                            "subtitle_track_ids": [t["track_id"] for t in chosen if t["kind"] == "subtitles"],
                            "track_flags": {
                                str(t["track_id"]): {f: bool(t["info"].get(f)) for f in FLAG_NAMES}
                                for t in chosen
                            },
                        },
                    )
                if job["state"] == "WAITING_FOR_SCREENSHOT_SELECTION":
                    shots = request("GET", path + "/screenshots")["items"]
                    best = sorted(
                        [
                            s
                            for s in shots
                            if s["info"].get("recommendation_rank") and not s.get("reservation")
                        ],
                        key=lambda s: s["info"]["recommendation_rank"],
                    )
                    assert best, "Agent must provide a usable verified screenshot"
                    request(
                        "POST", path + "/screenshots/selection", {"candidate_ids": [best[0]["candidate_id"]]}
                    )
                if job["state"] == "WAITING_FOR_RELEASE_DETAILS":
                    request("POST", path + "/release", job["analysis"]["release_details"])
            if all(job["state"] == "COMPLETE" for job in jobs):
                break
            for task_id, future in list(pending.items()):
                if future.done():
                    future.result()
                    del pending[task_id]
            with session() as db:
                config = queue.settings(db)
                ready = queue.available(db, config)
                for task in ready:
                    if task.job_id in ids and task.id not in pending:
                        pending[task.id] = executor.submit(execute.run, task.id)
            time.sleep(1)
        else:
            raise AssertionError("Acceptance run exceeded one hour; inspect isolated jobs")
        selected_frames = []
        for job in jobs:
            shots = request("GET", f"/jobs/{job['id']}/screenshots")["items"]
            chosen_shots = [shot for shot in shots if shot["selected"]]
            assert chosen_shots and all(shot["info"]["b_frames_verified"] for shot in chosen_shots)
            for shot in chosen_shots:
                assert all(
                    abs(shot["info"]["timeline_seconds"] - time)
                    >= job["screenshot_policy"]["min_spacing_seconds"]
                    for time in selected_frames
                )
            selected_frames.extend(shot["info"]["timeline_seconds"] for shot in chosen_shots)
            assert all(
                "mkvextract" not in str(command[0])
                for task in job["tasks"]
                if task["type"] == "prepare_tracks"
                for command in task["command_json"]
            )
            for artifact in job["artifacts"]:
                if artifact["artifact_type"] == "SCREENSHOT_ENCODE":
                    assert artifact["info"]["overlay_label"] == job["release_name"] + ".mkv"
                    assert artifact["info"]["picture_type"] == "B"
            assert job["validation"]["valid"], job["validation"]
            result = job["analysis"]["release_result"]
            assert not result["upload_screenshots"] and result["uploaded_images"] == 0
            types = {a["artifact_type"] for a in job["artifacts"]}
            assert {
                "FINAL_MKV",
                "SCREENSHOT_SOURCE",
                "SCREENSHOT_ENCODE",
                "RELEASE_BBCODE",
                "RELEASE_NFO",
                "RELEASE_MD5",
                "RELEASE_TORRENT",
            } <= types
            for artifact in job["artifacts"]:
                if artifact["artifact_type"] in (
                    "RELEASE_BBCODE",
                    "RELEASE_NFO",
                    "RELEASE_MD5",
                    "RELEASE_ENCODER_INFO",
                ):
                    request("GET", f"/artifacts/{artifact['id']}/preview")
            (results / f"{job['analysis_profile']}.json").write_text(json.dumps(job, indent=2))
        print(
            "PASS: both actual movie excerpt pipelines reached COMPLETE with validated encodes and local release artifacts",
            flush=True,
        )


if __name__ == "__main__":
    main()
