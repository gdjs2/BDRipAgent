"""Run source analysis with real media tools in a disposable container database."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import app
from shared.config import get_settings
from shared.db import Base, engine, session
from shared.models import Task
from worker.adapters.handbrake import Crop, parse_scan
from worker.tasks import execute


def main():
    settings = get_settings()
    source = Path(sys.argv[1])
    Base.metadata.create_all(engine())
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {settings.api_token}"
        response = client.post(
            "/api/jobs/pair",
            json={
                "source_path": str(source.relative_to(settings.source_root)),
                "title": "Scan regression test",
                "year": 2026,
                "analysis_profile": "x264-live",
                "second_profile": "x265-live",
            },
        )
        assert response.status_code == 201, response.text
        for created in response.json():
            with session() as db:
                task = db.scalar(select(Task).where(Task.job_id == created["id"], Task.status == "QUEUED"))
                task_id = task.id
            execute(task_id)
            job = client.get(f"/api/jobs/{created['id']}").json()
            assert job["tasks"][0]["status"] == "SUCCEEDED", job["tasks"]
            assert job["state"] == "RUNNING_CRF_ANALYSIS", job["state"]
            video = job["analysis"]["video"]
            crop = Crop(**job["analysis"]["crop"])
            scan_path = settings.workspace_root / job["id"] / "metadata" / task_id / "handbrake-scan.txt"
            scan = scan_path.read_text()
            assert "HandBrake has exited." not in scan
            assert parse_scan(scan) == crop
            print(
                f"PASS: {job['analysis_profile']} source analysis, crop {crop.argument()}, "
                f"dimensions {crop.dimensions(video['width'], video['height'])}, "
                f"{len(job['tracks'])} tracks, {job['state']}",
                flush=True,
            )


if __name__ == "__main__":
    main()
