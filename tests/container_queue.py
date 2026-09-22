"""Real PostgreSQL/Redis/Celery queue check; replaces media work with a short wait.

Start the test worker with `celery -A container_queue:celery worker --autoscale=3,1`.
Run this file separately against the same isolated database and source fixture.
"""

import time

from fastapi.testclient import TestClient

import worker.tasks as task_worker
from backend.app.main import app
from shared.config import get_settings
from worker.pipeline.stages import HANDLERS
from worker.tasks import celery as celery


def fixture_work(ctx):
    time.sleep(6)


HANDLERS["analyze"] = fixture_work


def fixture_advance(db, job):
    # This harness tests admission only; don't start real media/agent work.
    job.state = "WAITING_FOR_ENCODE_SELECTION"


task_worker.advance = fixture_advance


def main():
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {get_settings().api_token}"

        def change(path, body):
            response = client.patch(path, json=body)
            assert response.status_code == 200, response.text
            return response.json()

        def status():
            return client.get("/api/queue").json()

        def wait_until(check, timeout=45):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                value = status()
                if check(value):
                    return value
                time.sleep(0.2)
            raise AssertionError(status())

        change("/api/queue", {"paused": True, "max_other_tasks": 2})
        jobs = []
        for index in range(4):
            response = client.post(
                "/api/jobs",
                json={
                    "source_path": "Fixture.mkv",
                    "title": f"Queue fixture {index + 1}",
                    "year": 2026,
                },
            )
            assert response.status_code == 201, response.text
            jobs.append(response.json())
        ids = [job["tasks"][0]["id"] for job in jobs]
        order = [ids[1], ids[0], ids[2], ids[3]]
        assert client.put("/api/queue/order", json={"task_ids": order}).status_code == 200
        change(f"/api/queue/tasks/{ids[3]}", {"held": True})
        assert not status()["running"]
        change("/api/queue", {"paused": False})
        running = wait_until(lambda q: len(q["running"]) == 2)
        assert {t["id"] for t in running["running"]} == set(ids[:2])
        print("PASS: two real Celery worker processes run jobs concurrently in queue order", flush=True)
        change("/api/queue", {"max_other_tasks": 1, "paused": True})
        wait_until(lambda q: not q["running"])
        assert len(status()["queued"]) == 2
        assert all(
            client.get(f"/api/jobs/{job['id']}").json()["state"] == "WAITING_FOR_ENCODE_SELECTION"
            for job in jobs[:2]
        )
        change("/api/queue", {"paused": False})
        running = wait_until(lambda q: len(q["running"]) == 1)
        assert running["running"][0]["id"] == ids[2]
        change(f"/api/queue/tasks/{ids[3]}", {"held": False})

        def complete(q):
            assert len(q["running"]) <= 1, q
            return not q["running"] and not q["queued"]

        wait_until(complete)
        details = [client.get(f"/api/jobs/{job['id']}").json() for job in jobs]
        assert all(job["state"] == "WAITING_FOR_ENCODE_SELECTION" for job in details)
        assert details[2]["tasks"][0]["finished_at"] <= details[3]["tasks"][0]["started_at"]
        print(
            "PASS: pause drains active stages, lowered limit prevents overlap, held work resumes", flush=True
        )
        change("/api/queue", {"paused": True})
        extra = client.post(
            "/api/jobs", json={"source_path": "Fixture.mkv", "title": "Remove fixture", "year": 2026}
        ).json()
        assert client.delete(f"/api/jobs/{extra['id']}?confirm={extra['id']}").json()["files_retained"]
        assert not status()["queued"]
        assert (get_settings().source_root / "Fixture.mkv").is_file()
        print(
            "PASS: queued removal retains source files; settings and jobs persist in PostgreSQL", flush=True
        )


if __name__ == "__main__":
    main()
