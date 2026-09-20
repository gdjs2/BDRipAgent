"""Container smoke test with real CRF Studio/media tools and fixture screenshot choices."""

import json
import os

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import app
from shared.config import get_settings, profiles
from shared.db import Base, engine, session
from shared.models import Screenshot, Task
from shared.paths import contained
from worker.pipeline.stages import HANDLERS
from worker.tasks import execute


def fixture_selection(ctx):
    def save(db, job):
        shots = db.scalars(
            select(Screenshot).where(Screenshot.job_id == job.id).order_by(Screenshot.candidate_id)
        ).all()
        assert len(shots) >= 7
        for shot in [shots[round(i * (len(shots) - 1) / 6)] for i in range(7)]:
            shot.shortlisted = True
            shot.info = {**shot.info, "recommendation_rank": 1}
            shot.info = {**shot.info, "reason": "Explicit smoke-test fixture choice"}

    return save


def main():
    Base.metadata.create_all(engine())
    HANDLERS["select_screenshots"] = fixture_selection
    settings = get_settings()
    profile = os.environ.get("SMOKE_PROFILE", "x264-live")
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {settings.api_token}"
        response = client.post(
            "/api/jobs",
            json={
                "source_path": "Fixture.mkv",
                "title": "Container pipeline test",
                "year": 2026,
                "analysis_profile": profile,
                "screenshot_policy": {
                    "count": 7,
                    "representative": 4,
                    "encode_challenging": 3,
                    "min_spacing_seconds": 0,
                    "min_timeline_bins": 1,
                    "max_per_scene": 20,
                },
            },
        )
        assert response.status_code == 201, response.text
        job_id = response.json()["id"]
        for _ in range(20):
            job = client.get(f"/api/jobs/{job_id}").json()
            print("STATE", job["state"], flush=True)
            if job["state"] == "WAITING_FOR_RELEASE_DETAILS":
                break
            if job["state"] == "WAITING_FOR_TRACK_SELECTION":
                ids = [t["track_id"] for t in job["tracks"] if t["kind"] == "audio"]
                result = client.post(
                    f"/api/jobs/{job_id}/tracks/selection",
                    json={"audio_track_ids": ids, "subtitle_track_ids": []},
                )
                assert result.status_code == 200, result.text
            elif job["state"] == "WAITING_FOR_ENCODE_SELECTION":
                result = client.post(
                    f"/api/jobs/{job_id}/encode-selection",
                    json={"codec": profiles()[profile]["codec"], "profile": profile, "crf": 18},
                )
                assert result.status_code == 200, result.text
            elif job["state"] == "WAITING_FOR_SCREENSHOT_SELECTION":
                shots = client.get(f"/api/jobs/{job_id}/screenshots").json()["items"]
                result = client.post(
                    f"/api/jobs/{job_id}/screenshots/selection",
                    json={"candidate_ids": [s["candidate_id"] for s in shots if s["shortlisted"]]},
                )
                assert result.status_code == 200, result.text
            else:
                with session() as db:
                    task = db.scalar(select(Task).where(Task.job_id == job_id, Task.status == "QUEUED"))
                    if task is None:
                        failures = [t for t in job["tasks"] if t["status"] == "FAILED"]
                        for failed in failures:
                            print((settings.workspace_root / job_id / failed["log_path"]).read_text()[-6000:])
                        raise AssertionError(json.dumps(failures, indent=2))
                execute(task.id)
        assert job["state"] == "WAITING_FOR_RELEASE_DETAILS", job
        assert job["validation"]["valid"], job["validation"]
        assert job["validation"]["metrics"]["source_frames"] == 288
        assert any(a["artifact_type"] == "FINAL_MKV" for a in job["artifacts"])
        from worker.pipeline.validation import timeline

        final_pts = timeline(settings.completed_root / job_id / f"{job['release_name']}.mkv")
        assert abs(final_pts[0] - job["validation"]["metrics"]["source_first_pts"]) <= 0.001
        images = [
            a for a in job["artifacts"] if a["artifact_type"] in ["SCREENSHOT_SOURCE", "SCREENSHOT_ENCODE"]
        ]
        assert len(images) == 14
        for artifact in images:
            from PIL import Image

            path = contained(settings.workspace_root / job_id, artifact["path"], exists=True)
            assert Image.open(path).size == (640, 360)
        print(
            "PASS: real analysis, audio extraction, HandBrake encode, validation, remux, candidates, PNG pairs"
        )
        print("Live Codex is replaced by explicit fixture selections. Sup2Sup is tested separately.")


if __name__ == "__main__":
    main()
