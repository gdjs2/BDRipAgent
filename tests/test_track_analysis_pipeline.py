import copy
import json
import threading
from pathlib import Path

import pytest

from backend.app.services import reconcile
from shared.db import session
from shared.models import MovieJob
from tests.conftest import gate
from tests.test_track_review import agent_answer, fixture_tracks
from worker.adapters import track_review
from worker.pipeline import stages
from worker.runtime import TaskContext
from worker.tasks import execute


@pytest.fixture
def pipeline(client, new_job, monkeypatch):
    calls = {"scan": 0, "extract": 0, "classify": [], "prepare": [], "audio": 0}
    fail = {"track": None}
    prepared_second = threading.Event()
    reviewing_first = threading.Event()

    def scan(ctx):
        calls["scan"] += 1
        return dict(
            tracks=copy.deepcopy(fixture_tracks()),
            video=dict(width=1920, height=1080, duration=300, track_id=0),
            crop=dict(top=0, bottom=0, left=0, right=0),
        )

    def run(ctx, command, **kwargs):
        calls["extract"] += 1
        mode = None
        for arg in command[2:]:
            if arg == "--gui-mode":
                continue
            if arg in ("tracks", "timestamps_v2"):
                mode = arg
                continue
            _, path = arg.split(":", 1)
            Path(path).write_text("track data" if mode == "tracks" else "# timestamp format v2\n0\n40\n")

    def prepare(ctx, track, source):
        calls["prepare"].append(track["track_id"])
        if track["track_id"] == 12:
            assert reviewing_first.wait(5), "The agent did not begin while another track was being prepared"
            prepared_second.set()
        return track["track_id"]

    def classify(ctx, track, source, **options):
        track_id = track["track_id"]
        calls["classify"].append(track_id)
        assert options["prepared"] == track_id
        if track_id == 9:
            reviewing_first.set()
            assert prepared_second.wait(5), "Local preparation did not overlap agent review"
        if fail["track"] == track_id:
            raise ValueError("fixture review failure")
        return {
            **track,
            "language": "fr",
            "hearing_impaired": False,
            "subtitle_detection": dict(
                schema_version=2,
                language_code="fr",
                language_confident=True,
                sdh_confident=True,
                hearing_impaired=False,
                status="resolved",
            ),
        }

    def audio(ctx, tracks, duration):
        calls["audio"] += 1
        assert tracks[0]["source_track_path"].endswith(".ac3")
        return {t["track_id"]: {"samples": [{"id": 1}], "limitations": []} for t in tracks}

    def review(ctx):
        inventory = ctx.settings.cache_root / "agent" / ctx.job.id / ctx.task_id / "tracks/inventory.json"
        return agent_answer(json.loads(inventory.read_text())["tracks"])

    monkeypatch.setattr(stages, "analyze", scan)
    monkeypatch.setattr(TaskContext, "run", run)
    monkeypatch.setattr(stages, "prepare_subtitle", prepare)
    monkeypatch.setattr(stages, "classify_subtitle", classify)
    monkeypatch.setattr(track_review, "analyze_audio", audio)
    monkeypatch.setattr(track_review, "review", review)
    return new_job, calls, fail


def test_preparation_overlaps_review_and_completed_analysis_never_restarts(client, pipeline):
    job, calls, _ = pipeline
    execute(job["tasks"][0]["id"])
    review_task = next(
        t for t in client.get(f"/api/jobs/{job['id']}").json()["tasks"] if t["type"] == "review_tracks"
    )
    execute(review_task["id"])
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert detail["state"] == "RUNNING_CRF_ANALYSIS", detail["tasks"]
    assert detail["track_analysis_complete"] is True
    assert calls == {"scan": 1, "extract": 1, "classify": [9, 12], "prepare": [9, 12], "audio": 1}
    assert detail["analysis"]["source_video_timestamps"].endswith("track-0.timestamps.txt")
    for _ in range(2):
        response = client.post(f"/api/jobs/{job['id']}/tracks/analyze", json={})
        assert response.status_code == 202
        assert response.json()["state"] == "RUNNING_CRF_ANALYSIS"
    with session() as db:
        reconcile(db)
    assert len(client.get(f"/api/jobs/{job['id']}").json()["tasks"]) == 3


def test_failed_review_resumes_saved_tracks_without_rescanning_or_extracting(client, pipeline):
    job, calls, fail = pipeline
    fail["track"] = 12
    execute(job["tasks"][0]["id"])
    review_task = next(
        t for t in client.get(f"/api/jobs/{job['id']}").json()["tasks"] if t["type"] == "review_tracks"
    )
    execute(review_task["id"])
    partial = client.get(f"/api/jobs/{job['id']}").json()
    assert next(t for t in partial["tasks"] if t["type"] == "review_tracks")["status"] == "FAILED"
    assert partial["track_analysis_complete"] is False
    assert (
        next(t for t in partial["tracks"] if t["track_id"] == 9)["info"]["subtitle_detection"][
            "language_code"
        ]
        == "fr"
    )
    fail["track"] = None
    retry = client.post(f"/api/tasks/{review_task['id']}/retry", json={})
    assert retry.status_code == 202
    execute(retry.json()["id"])
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["state"] == "RUNNING_CRF_ANALYSIS", done["tasks"]
    assert calls["scan"] == calls["extract"] == 1
    assert calls["classify"] == [9, 12, 12]
    assert calls["prepare"] == [9, 12, 12]


def test_missing_review_is_scheduled_automatically_once(client, new_job):
    gate(new_job["id"], "WAITING_FOR_TRACK_SELECTION")
    with session() as db:
        reconcile(db)
        reconcile(db)
    detail = client.get(f"/api/jobs/{new_job['id']}").json()
    assert detail["state"] == "WAITING_FOR_TRACK_SELECTION"
    assert sum(t["status"] == "QUEUED" for t in detail["tasks"]) == 1


def test_inconclusive_completed_review_does_not_automatically_run_again(client, new_job):
    gate(new_job["id"], "WAITING_FOR_TRACK_SELECTION")
    with session() as db:
        db.get(MovieJob, new_job["id"]).analysis = {
            "track_review_version": 1,
            "tracks": [
                {
                    "track_id": 2,
                    "codec_id": "S_HDMV/PGS",
                    "track_review": {"schema_version": 1},
                    "subtitle_detection": {"schema_version": 1, "status": "inconclusive"},
                }
            ],
        }
        db.commit()
        reconcile(db)
    detail = client.get(f"/api/jobs/{new_job['id']}").json()
    assert detail["state"] == "WAITING_FOR_TRACK_SELECTION"
    assert detail["track_analysis_complete"] is True and len(detail["tasks"]) == 1


def test_failed_review_cancels_the_preparation_lane(client, pipeline, monkeypatch):
    job, _, _ = pipeline
    running = threading.Event()
    stopped = threading.Event()

    def prepare(ctx, track, source):
        if track["track_id"] == 9:
            return 9
        running.set()
        try:
            while not ctx.local_cancel.wait(0.01):
                ctx.check()
            ctx.check()
        finally:
            stopped.set()

    def fail(ctx, *args, **kwargs):
        assert running.wait(5)
        raise ValueError("agent failed while preparing the next track")

    monkeypatch.setattr(stages, "prepare_subtitle", prepare)
    monkeypatch.setattr(stages, "classify_subtitle", fail)
    execute(job["tasks"][0]["id"])
    review_task = next(
        t for t in client.get(f"/api/jobs/{job['id']}").json()["tasks"] if t["type"] == "review_tracks"
    )
    execute(review_task["id"])
    assert stopped.is_set()
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert next(t for t in detail["tasks"] if t["type"] == "review_tracks")["status"] == "FAILED"
    assert "agent failed" in next(t for t in detail["tasks"] if t["type"] == "review_tracks")["error_message"]


def same_source_job(client):
    response = client.post(
        "/api/jobs",
        json={"source_path": "Movie.mkv", "title": "Movie", "year": 2026, "analysis_profile": "x264-live"},
    )
    assert response.status_code == 201
    job = response.json()
    execute(job["tasks"][0]["id"])
    return client.get(f"/api/jobs/{job['id']}").json()


def test_same_source_codec_jobs_share_completed_analysis(client, pipeline, monkeypatch):
    job, calls, _ = pipeline
    execute(job["tasks"][0]["id"])
    first = client.get(f"/api/jobs/{job['id']}").json()
    execute(next(t["id"] for t in first["tasks"] if t["type"] == "review_tracks"))
    before = copy.deepcopy(calls)
    second = same_source_job(client)
    execute(next(t["id"] for t in second["tasks"] if t["type"] == "review_tracks"))
    reused = client.get(f"/api/jobs/{second['id']}").json()
    assert reused["track_analysis_complete"], reused["tasks"]
    assert reused["analysis"]["shared_track_analysis"]["reused"]
    assert calls == {**before, "scan": 2}
    assert not reused["track_selection"]
    assert all(t["info"].get("track_review") for t in reused["tracks"])
    assert any(a["artifact_type"] == "TRACK_REVIEW" for a in reused["artifacts"])


def test_concurrent_codec_jobs_do_not_duplicate_local_or_agent_review(client, pipeline, monkeypatch):
    job, calls, _ = pipeline
    execute(job["tasks"][0]["id"])
    first = client.get(f"/api/jobs/{job['id']}").json()
    second = same_source_job(client)
    entered, waiting, release = threading.Event(), threading.Event(), threading.Event()
    agent_calls = []
    original_review, original_progress = track_review.review, TaskContext.progress

    def review(ctx):
        agent_calls.append(ctx.job.id)
        entered.set()
        assert release.wait(10)
        return original_review(ctx)

    def progress(ctx, value, **detail):
        if detail.get("phase") == "Waiting for the shared audio/subtitle analysis":
            waiting.set()
        return original_progress(ctx, value, **detail)

    monkeypatch.setattr(track_review, "review", review)
    monkeypatch.setattr(TaskContext, "progress", progress)
    tasks = [next(t["id"] for t in item["tasks"] if t["type"] == "review_tracks") for item in (first, second)]
    threads = [threading.Thread(target=execute, args=(task,)) for task in tasks]
    try:
        threads[0].start()
        assert entered.wait(5)
        threads[1].start()
        assert waiting.wait(5)
        assert agent_calls == [first["id"]]
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert calls == {"scan": 2, "extract": 1, "classify": [9, 12], "prepare": [9, 12], "audio": 1}
    assert agent_calls == [first["id"]]
    for item in (first, second):
        current = client.get(f"/api/jobs/{item['id']}").json()
        assert current["track_analysis_complete"], current["tasks"]


def test_next_job_reuses_partial_subtitle_review_after_failure(client, pipeline):
    job, calls, fail = pipeline
    fail["track"] = 12
    execute(job["tasks"][0]["id"])
    current = client.get(f"/api/jobs/{job['id']}").json()
    execute(next(t["id"] for t in current["tasks"] if t["type"] == "review_tracks"))
    fail["track"] = None
    second = same_source_job(client)
    execute(next(t["id"] for t in second["tasks"] if t["type"] == "review_tracks"))
    current = client.get(f"/api/jobs/{second['id']}").json()
    assert current["track_analysis_complete"], current["tasks"]
    assert calls["classify"] == [9, 12, 12]
    assert calls["extract"] == 1
