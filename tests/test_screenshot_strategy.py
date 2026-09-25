import json
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

from agent.schemas import Selection
from agent.screenshot_agent import CodexScreenshotSelector
from backend.app.services import enqueue
from shared.config import ScreenshotPolicy, screenshot_selection_limits
from shared.db import session
from shared.models import AgentRun, MovieJob, Screenshot, Task
from shared.paths import write_json
from tests.conftest import gate
from tests.test_screenshot_review import review_candidates
from worker.pipeline import screenshots
from worker.tasks import execute


def test_local_review_never_prefills_best_even_for_high_quality_candidates():
    candidates = [{**c, "quality": 100 - c["candidate_id"]} for c in review_candidates()]
    result = screenshots.local_selection(candidates, ScreenshotPolicy().model_dump())
    assert result["selected"] == []
    assert len(result["shortlisted_ids"]) == 40
    assert result["warning"] is None


def setup_review(new_job, environment, strategy="local"):
    job_id = new_job["id"]
    gate(job_id, "SCREENSHOT_AGENT_SELECTION")
    root = environment.workspace_root / job_id
    candidates = review_candidates()
    for c in candidates:
        c["path"] = f"{c['candidate_id']}.png"
        c["quality"] = c["candidate_id"]
        Image.new("RGB", (32, 32), "gray").save(root / c["path"])
    write_json(root / "candidates.json", {"candidates": candidates, "decoder": {"sampling_round": 0}})
    with session() as db:
        job = db.get(MovieJob, job_id)
        job.analysis = {"candidate_index": "candidates.json"}
        job.validation = {"metrics": {"source_duration": 4000}}
        job.screenshot_policy = ScreenshotPolicy(strategy=strategy).model_dump()
        for c in candidates:
            db.add(Screenshot(job_id=job_id, candidate_id=c["candidate_id"], info=c))
        task = enqueue(db, job)
        task_id = task.id
        db.commit()
    return task_id, candidates


def test_local_stage_never_calls_agent_and_allows_fourteen_final_pairs(
    client, new_job, environment, monkeypatch
):
    task_id, _ = setup_review(new_job, environment)
    monkeypatch.setattr(screenshots, "select_screenshots", lambda *a: pytest.fail("Local mode called agent"))
    monkeypatch.setattr(
        screenshots, "render_pairs", lambda *a, **kw: pytest.fail("Local review rendered unchosen pairs")
    )
    execute(task_id)
    job = client.get(f"/api/jobs/{new_job['id']}").json()
    assert job["state"] == "WAITING_FOR_SCREENSHOT_SELECTION", job["tasks"]
    assert job["analysis"]["screenshot_review"]["available"] == 0
    with session() as db:
        assert not db.scalar(select(AgentRun).where(AgentRun.job_id == new_job["id"]))
    gallery = client.get(f"/api/jobs/{new_job['id']}/screenshots").json()
    assert gallery["shortlisted"] == 40 and gallery["recommended"] == 0
    ids = [s["candidate_id"] for s in gallery["items"]][:14]
    assert (
        client.post(
            f"/api/jobs/{new_job['id']}/screenshots/selection", json={"candidate_ids": ids}
        ).status_code
        == 409
    )
    added = client.patch(f"/api/jobs/{new_job['id']}/screenshots/best", json={"candidate_ids": ids})
    assert added.status_code == 200, added.text
    assert added.json()["recommended"] == 14
    response = client.post(f"/api/jobs/{new_job['id']}/screenshots/selection", json={"candidate_ids": ids})
    assert response.status_code == 200, response.text


def test_strategy_change_and_resample_are_fenced(client, new_job, environment):
    task_id, _ = setup_review(new_job, environment)
    url = f"/api/jobs/{new_job['id']}/screenshots"
    assert client.patch(url + "/strategy", json={"strategy": "agent"}).status_code == 200
    with session() as db:
        db.get(Task, task_id).status = "RUNNING"
        db.commit()
    assert client.patch(url + "/strategy", json={"strategy": "local"}).status_code == 409
    assert client.post(url + "/review", json={"strategy": "local"}).status_code == 409
    with session() as db:
        db.get(Task, task_id).status = "CANCELLED"
        db.commit()
    response = client.post(url + "/review", json={"strategy": "local", "resample": True, "best_count": 35})
    assert response.status_code == 202, response.text
    assert response.json()["type"] == "generate_candidates"
    job = client.get(f"/api/jobs/{new_job['id']}").json()
    assert job["screenshot_policy"]["strategy"] == "local"
    assert job["screenshot_policy"]["best_count"] == 35


def choice(i, rank=0):
    return {
        "candidate_id": i,
        "score": 0.9,
        "category": "representative" if rank < 17 else "encode_challenging",
        "reason": "Detailed character",
        "shot_type": "close_up",
        "subject": str(i),
        "character_visible": True,
    }


def test_agent_requests_more_samples_instead_of_padding_shortlist(tmp_path, monkeypatch):
    candidates = review_candidates()[:10]
    for c in candidates:
        c["sheet"] = "sheet.jpg"
    (tmp_path / "sheet.jpg").touch()
    write_json(
        tmp_path / "inventory.json",
        {
            "candidates": candidates,
            "policy": ScreenshotPolicy(strategy="agent").model_dump(),
            "duration": 4000,
            "contact_sheets": ["sheet.jpg"],
            "allow_partial": False,
        },
    )
    calls = []

    def invoke(self, prompt, images):
        calls.append(prompt)
        return Selection(selected=[choice(1), choice(3)]), "test"

    monkeypatch.setattr(CodexScreenshotSelector, "_invoke", invoke)
    result = CodexScreenshotSelector().select(tmp_path)
    assert result["needs_more_candidates"] and result["selected"] == []
    assert result["reviewed_candidate_ids"] == list(range(1, 11))
    assert len(calls) == 1


def test_worker_resamples_and_checkpoints_until_agent_has_thirty(client, new_job, environment, monkeypatch):
    task_id, candidates = setup_review(new_job, environment, "agent")
    monkeypatch.setattr(screenshots, "render_pairs", lambda *a, **kw: {})
    monkeypatch.setattr(screenshots, "contact_sheets", lambda *a: [])
    calls, passes = [], []

    def select_frames(ctx, timeout):
        inventory = json.loads(
            (environment.cache_root / "agent" / new_job["id"] / task_id / "inventory.json").read_text()
        )
        calls.append(inventory)
        if len(calls) == 1:
            return {
                "needs_more_candidates": True,
                "selected": [],
                "shortlisted_choices": [choice(1)],
                "reviewed_candidate_ids": list(range(1, 41)),
            }
        return {
            "shortlisted_ids": list(range(1, 41)),
            "selected": [choice(i, rank) for rank, i in enumerate(range(1, 31))],
            "thread_id": "test",
        }

    def sample(ctx, *, existing, sampling_round, reserved):
        passes.append(sampling_round)
        assert len(existing) == 40
        extra = {**candidates[-1], "candidate_id": 101, "source_frame_number": 101000}
        index = {"candidates": [*existing, extra], "decoder": {"sampling_round": sampling_round}}
        path = ctx.output("screenshots", "expanded.json")
        write_json(path, index)
        return index, str(path.relative_to(ctx.workspace)), "index.npy"

    monkeypatch.setattr(screenshots, "sample_candidates", sample)
    monkeypatch.setattr(screenshots, "select_screenshots", select_frames)
    execute(task_id)
    job = client.get(f"/api/jobs/{new_job['id']}").json()
    assert job["state"] == "WAITING_FOR_SCREENSHOT_SELECTION", job["tasks"]
    assert passes == [1] and calls[1]["reviewed_candidate_ids"] == list(range(1, 41))
    assert job["analysis"]["candidate_index"].endswith("expanded.json")
    assert job["analysis"]["screenshot_review"]["available"] == 30
    with session() as db:
        assert db.scalar(
            select(Screenshot).where(Screenshot.job_id == new_job["id"], Screenshot.candidate_id == 101)
        )


def test_additional_windows_are_new_and_screenshot_budget_is_separate(monkeypatch):
    first = screenshots.sampling_windows(7200, 100, 4, 3)
    second = screenshots.sampling_windows(7200, 100, 4, 3, pass_offset=4)
    assert not {a for _, _, a, _ in first} & {a for _, _, a, _ in second}
    limits = screenshot_selection_limits()
    assert limits == {"max_seconds": 7200, "request_timeout_seconds": 1800, "max_sampling_rounds": 4}
    ctx = screenshots.ScreenshotDeadline(SimpleNamespace(check=lambda: None), 0)
    with pytest.raises(TimeoutError, match="time limit"):
        ctx.check()


def test_resampling_keeps_final_reservations_until_replacement(client, new_job, environment, monkeypatch):
    task_id, candidates = setup_review(new_job, environment)
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        row = db.scalar(select(Screenshot).where(Screenshot.job_id == job.id, Screenshot.candidate_id == 1))
        row.selected = True
        db.commit()
    new = {**candidates[1], "candidate_id": 102}
    seen = []

    def sample(ctx, **kwargs):
        seen.append(kwargs["sampling_round"])
        return {"candidates": [new], "decoder": {"sampling_round": 1}}, "next.json", "pts.npy"

    monkeypatch.setattr(screenshots, "sample_candidates", sample)
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        save = screenshots.generate(SimpleNamespace(job=job))
        save(db, job)
        db.commit()
        final = db.scalar(select(Screenshot).where(Screenshot.job_id == job.id, Screenshot.candidate_id == 1))
        assert final.selected and not final.shortlisted
        assert seen == [1]
        assert len(db.scalars(select(Screenshot).where(Screenshot.job_id == job.id)).all()) == 2


def test_more_appends_fifteen_without_changing_best_finals_or_using_agent(
    client, new_job, environment, monkeypatch
):
    old_task, _ = setup_review(new_job, environment)
    job_id = new_job["id"]
    with session() as db:
        db.get(Task, old_task).status = "SUCCEEDED"
        job = db.get(MovieJob, job_id)
        job.state = "WAITING_FOR_SCREENSHOT_SELECTION"
        first = db.scalar(select(Screenshot).where(Screenshot.job_id == job_id, Screenshot.candidate_id == 1))
        first.selected = True
        for row in db.scalars(select(Screenshot).where(Screenshot.job_id == job_id)):
            row.shortlisted = True
        db.commit()
    before = client.get(f"/api/jobs/{job_id}/screenshots").json()
    url = f"/api/jobs/{job_id}/screenshots/more"
    for count in [0, 101, True]:
        assert client.post(url, json={"count": count}).status_code == 422
    response = client.post(url, json={"count": 15})
    assert response.status_code == 202, response.text
    assert response.json()["type"] == "generate_candidates"
    assert client.post(url, json={"count": 15}).status_code == 409

    def sample(ctx, *, existing, target, sampling_round, reserved):
        assert target == 15 and sampling_round == 1 and len(existing) == 40
        additions = [
            {**existing[0], "candidate_id": i, "source_frame_number": i * 3000, "recommendation_rank": None}
            for i in range(41, 56)
        ]
        index = {"candidates": existing + additions, "decoder": {"sampling_round": 1}}
        path = ctx.output("screenshots", "more.json")
        write_json(path, index)
        return index, str(path.relative_to(ctx.workspace)), "pts.npy"

    monkeypatch.setattr(screenshots, "sample_candidates", sample)
    monkeypatch.setattr(
        screenshots, "select_screenshots", lambda *a: pytest.fail("More screenshots called agent")
    )
    monkeypatch.setattr(
        screenshots,
        "render_pairs",
        lambda *a, **kw: pytest.fail("More screenshots changed final comparisons"),
    )
    execute(response.json()["id"])
    with session() as db:
        task = db.scalar(select(Task).where(Task.job_id == job_id, Task.status == "QUEUED"))
        assert task.type == "select_screenshots"
        task_id = task.id
    execute(task_id)
    current = client.get(f"/api/jobs/{job_id}/screenshots").json()
    assert current["shortlisted"] == 55
    assert current["recommended"] == before["recommended"]
    assert current["final"] == before["final"] == 1
    old = {s["candidate_id"]: s for s in before["items"]}
    for item in current["items"]:
        if item["candidate_id"] in old:
            assert item == old[item["candidate_id"]]
        else:
            assert not item["info"].get("recommendation_rank") and not item["selected"]
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["analysis"]["screenshot_more"]["added"] == 15
    assert "screenshot_append" not in job["analysis"]
