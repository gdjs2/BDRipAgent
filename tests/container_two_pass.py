"""Actual x264/x265 two-pass encoding through the API and worker, in disposable storage.

Run in the worker image. All database/storage paths must be under /tmp. Source
analysis, encoding and validation are real; only the CRF sampling gate is seeded
with the actual profile snapshot, since this test targets final rate control.
"""

import json
import subprocess

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import app
from shared.config import get_settings, profiles
from shared.db import Base, engine, session
from shared.models import CRFResult, Event, MovieJob, Task
from worker.tasks import execute


def main():
    settings = get_settings()
    assert settings.database_url.startswith("sqlite:////tmp/")
    for folder in (settings.source_root, settings.workspace_root, settings.completed_root):
        assert str(folder).startswith("/tmp/")
        folder.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine())
    subprocess.run(
        [
            settings.ffmpeg_bin,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24:duration=12",
            "-c:v",
            "ffv1",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "2",
            str(settings.source_root / "Fixture.mkv"),
        ],
        check=True,
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {settings.api_token}"
        for codec in ("x264", "x265"):
            profile = f"{codec}-live"
            response = client.post(
                "/api/jobs",
                json={
                    "title": "Two pass test",
                    "year": 2026,
                    "source_path": "Fixture.mkv",
                    "analysis_profile": profile,
                },
            )
            assert response.status_code == 201, response.text
            job_id = response.json()["id"]
            execute(response.json()["tasks"][0]["id"])
            with session() as db:
                job = db.get(MovieJob, job_id)
                assert job.state == "WAITING_FOR_TRACK_SELECTION"
                job.state = "WAITING_FOR_ENCODE_SELECTION"
                db.add(CRFResult(job_id=job_id, data={"profile_snapshot": profiles()[profile]}))
                db.commit()
            response = client.post(
                f"/api/jobs/{job_id}/encode-selection",
                json={
                    "codec": codec,
                    "profile": profile,
                    "rate_control": "bitrate",
                    "bitrate_kbps": 750,
                },
            )
            assert response.status_code == 200, response.text
            task_id = next(t["id"] for t in response.json()["tasks"] if t["type"] == "encode")
            execute(task_id)
            with session() as db:
                task = db.get(Task, task_id)
                log = (settings.workspace_root / job_id / task.log_path).read_text()
                assert task.status == "SUCCEEDED", log[-6000:]
                assert "task 1 of 2" in log and "task 2 of 2" in log, log[-6000:]
                updates = [
                    e.data
                    for e in db.scalars(
                        select(Event)
                        .where(Event.job_id == job_id, Event.type == "task_progress")
                        .order_by(Event.id)
                    )
                    if e.data.get("task_id") == task_id
                ]
                assert {p.get("pass_number") for p in updates} == {1, 2}, updates
                assert all(p["progress"] <= 50 for p in updates if p.get("pass_number") == 1)
                assert all(p["progress"] >= 50 for p in updates if p.get("pass_number") == 2)
                validate = db.scalar(select(Task).where(Task.job_id == job_id, Task.type == "validate"))
                validation_id = validate.id
            execute(validation_id)
            job = client.get(f"/api/jobs/{job_id}").json()
            assert job["validation"]["valid"], job["validation"]
            assert job["validation"]["metrics"]["source_frames"] == 288
            assert job["validation"]["metrics"]["encoded_frames"] == 288
            assert job["encode_config"]["data"]["bitrate_kbps"] == 750
            print(
                json.dumps(
                    {
                        "codec": codec,
                        "passes": [1, 2],
                        "progress_updates": len(updates),
                        "target_kbps": 750,
                        "frames": 288,
                        "validation": "passed",
                    }
                ),
                flush=True,
            )
            # This harness ends at validation; do not run remux/screenshots.
            with session() as db:
                for pending in db.scalars(select(Task).where(Task.job_id == job_id, Task.status == "QUEUED")):
                    pending.status = "CANCELLED"
                db.commit()


if __name__ == "__main__":
    main()
