import json

import pytest
from sqlalchemy import select

from agent.schemas import Selection
from agent.screenshot_agent import CodexScreenshotSelector
from backend.app.services import advance
from shared.config import ScreenshotPolicy
from shared.db import session
from shared.models import MovieJob, Screenshot, Task
from tests.conftest import gate


def review_candidates():
    return [
        {
            "candidate_id": i,
            "source_frame_number": i * 2400,
            "source_pts_seconds": i * 100,
            "timeline_seconds": i * 100,
            "scene_id": i,
            "picture_type": "B",
            "encoded_picture_type": "B",
            "b_frames_verified": True,
            "agent_image": f"{i}.png",
            "recommendation_rank": i if i <= 15 else None,
        }
        for i in range(1, 41)
    ]


@pytest.fixture
def review_job(client, new_job):
    gate(new_job["id"], "WAITING_FOR_SCREENSHOT_SELECTION")
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        job.validation = {"metrics": {"source_duration": 4000}}
        for candidate in review_candidates():
            db.add(
                Screenshot(
                    job_id=job.id, candidate_id=candidate["candidate_id"], shortlisted=True, info=candidate
                )
            )
        db.commit()
    return new_job["id"]


def test_review_is_a_human_gate_without_automatic_render(client, new_job):
    gate(new_job["id"], "SCREENSHOT_AGENT_SELECTION")
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        advance(db, job)
        db.commit()
        assert job.state == "WAITING_FOR_SCREENSHOT_SELECTION"
        assert not db.scalar(select(Task).where(Task.job_id == job.id, Task.status == "QUEUED"))
    assert client.post(f"/api/jobs/{new_job['id']}/screenshots/render", json={}).status_code == 409


@pytest.mark.parametrize("count", [1, 3, 10, 15])
def test_user_can_confirm_any_subset_and_render_only_that_count(client, review_job, count):
    ids = list(range(1, count + 1))
    response = client.post(f"/api/jobs/{review_job}/screenshots/selection", json={"candidate_ids": ids})
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["state"] == "SCREENSHOT_RENDERING"
    assert job["analysis"]["screenshot_selection"] == {
        "candidate_ids": ids,
        "count": count,
        "selected_by": "user",
    }
    assert job["screenshot_policy"]["count"] == 7
    assert [t["type"] for t in job["tasks"] if t["status"] == "QUEUED"] == ["render_screenshots"]
    shots = client.get(f"/api/jobs/{review_job}/screenshots").json()
    assert (shots["recommended"], shots["shortlisted"], shots["final"]) == (15, 40, count)
    assert [s["candidate_id"] for s in shots["items"] if s["selected"]] == ids
    assert all(s["info"]["selected_by"] == "user" for s in shots["items"] if s["selected"])
    assert (
        client.post(f"/api/jobs/{review_job}/screenshots/selection", json={"candidate_ids": ids}).status_code
        == 409
    )


@pytest.mark.parametrize(
    "ids,status",
    [
        ([], 422),
        (list(range(1, 17)), 422),
        ([1, 1], 422),
        ([True], 422),
        (["1"], 422),
        ([0], 422),
        ([16], 409),
        ([999], 409),
    ],
)
def test_invalid_choices_do_not_change_selection_or_queue_work(client, review_job, ids, status):
    response = client.post(f"/api/jobs/{review_job}/screenshots/selection", json={"candidate_ids": ids})
    assert response.status_code == status, response.text
    assert client.get(f"/api/jobs/{review_job}/screenshots").json()["final"] == 0
    assert client.get(f"/api/jobs/{review_job}").json()["state"] == "WAITING_FOR_SCREENSHOT_SELECTION"


@pytest.mark.parametrize(
    "change", [{"b_frames_verified": False}, {"encoded_picture_type": "P"}, {"picture_type": "I"}]
)
def test_confirmation_rechecks_b_frame_eligibility(client, review_job, change):
    with session() as db:
        row = db.scalar(
            select(Screenshot).where(Screenshot.job_id == review_job, Screenshot.candidate_id == 1)
        )
        row.info = {**row.info, **change}
        db.commit()
    response = client.post(f"/api/jobs/{review_job}/screenshots/selection", json={"candidate_ids": [1]})
    assert response.status_code == 409
    assert "B-frames" in response.text


def test_confirmation_retains_spacing_checks(client, review_job):
    with session() as db:
        row = db.scalar(
            select(Screenshot).where(Screenshot.job_id == review_job, Screenshot.candidate_id == 2)
        )
        row.info = {**row.info, "timeline_seconds": 101}
        db.commit()
    response = client.post(f"/api/jobs/{review_job}/screenshots/selection", json={"candidate_ids": [1, 2]})
    assert response.status_code == 409 and "too close" in response.text


def test_best_list_can_remove_add_and_clear_without_changing_finals(client, review_job):
    with session() as db:
        row = db.scalar(
            select(Screenshot).where(Screenshot.job_id == review_job, Screenshot.candidate_id == 1)
        )
        row.selected = True
        row.info = {**row.info, "comparisons": {"src": "old-src.png", "encode": "old-encode.png"}}
        db.commit()
    url = f"/api/jobs/{review_job}/screenshots/best"
    ids = [*range(2, 16), 40]
    result = client.patch(url, json={"candidate_ids": ids})
    assert result.status_code == 200, result.text
    assert result.json()["shortlisted"] == 40
    assert result.json()["recommended"] == 15
    assert result.json()["final"] == 1
    saved = client.get(f"/api/jobs/{review_job}/screenshots").json()
    best = sorted(
        (s for s in saved["items"] if s["info"]["recommendation_rank"]),
        key=lambda s: s["info"]["recommendation_rank"],
    )
    assert [s["candidate_id"] for s in best] == ids
    assert saved["items"][0]["info"]["comparisons"]["src"] == "old-src.png"
    assert client.get(f"/api/jobs/{review_job}").json()["state"] == "WAITING_FOR_SCREENSHOT_SELECTION"
    assert client.patch(url, json={"candidate_ids": []}).json()["recommended"] == 0
    assert client.patch(url, json={"candidate_ids": [40]}).json()["recommended"] == 1
    # A human-added shortlist frame can become the only final pair.
    response = client.post(f"/api/jobs/{review_job}/screenshots/selection", json={"candidate_ids": [40]})
    assert response.status_code == 200, response.text
    assert response.json()["analysis"]["screenshot_selection"]["candidate_ids"] == [40]
    assert client.patch(url, json={"candidate_ids": []}).status_code == 409


@pytest.mark.parametrize(
    "ids,status", [(list(range(1, 17)), 422), ([1, 1], 422), ([False], 422), ([999], 409)]
)
def test_best_curation_rejects_invalid_ids(client, review_job, ids, status):
    result = client.patch(f"/api/jobs/{review_job}/screenshots/best", json={"candidate_ids": ids})
    assert result.status_code == status
    assert client.get(f"/api/jobs/{review_job}/screenshots").json()["recommended"] == 15


@pytest.mark.parametrize("shortlisted,verified", [(False, True), (True, False)])
def test_best_curation_rechecks_shortlist_and_b_frames(client, review_job, shortlisted, verified):
    with session() as db:
        row = db.scalar(
            select(Screenshot).where(Screenshot.job_id == review_job, Screenshot.candidate_id == 40)
        )
        row.shortlisted = shortlisted
        row.info = {**row.info, "b_frames_verified": verified}
        db.commit()
    result = client.patch(f"/api/jobs/{review_job}/screenshots/best", json={"candidate_ids": [40]})
    assert result.status_code == 409


def test_best_curation_requires_authentication(client, review_job):
    client.headers.clear()
    assert (
        client.patch(f"/api/jobs/{review_job}/screenshots/best", json={"candidate_ids": []}).status_code
        == 401
    )


def test_confirmation_retains_cross_codec_reservations(client, review_job):
    peer = client.post(
        "/api/jobs",
        json={"source_path": "Movie.mkv", "title": "Movie", "year": 2026, "analysis_profile": "x264-live"},
    )
    assert peer.status_code == 201, peer.text
    with session() as db:
        db.add(
            Screenshot(job_id=peer.json()["id"], candidate_id=1, selected=True, info=review_candidates()[0])
        )
        db.commit()
    response = client.post(f"/api/jobs/{review_job}/screenshots/selection", json={"candidate_ids": [1]})
    assert response.status_code == 409 and "other codec" in response.text


def test_single_confirmed_pair_can_still_be_replaced(client, review_job):
    assert (
        client.post(f"/api/jobs/{review_job}/screenshots/selection", json={"candidate_ids": [1]}).status_code
        == 200
    )
    gate(review_job, "COMPLETE")
    shot = client.get(f"/api/jobs/{review_job}/screenshots").json()["items"][0]
    response = client.post(
        f"/api/jobs/{review_job}/screenshots/{shot['id']}/replace", json={"candidate_id": 2}
    )
    assert response.status_code == 200, response.text
    job = client.get(f"/api/jobs/{review_job}").json()
    assert job["analysis"]["screenshot_selection"]["candidate_ids"] == [2]
    assert job["analysis"]["screenshot_selection"]["count"] == 1


def test_shortlist_review_of_completed_job_preserves_existing_exports(client, review_job):
    gate(review_job, "COMPLETE")
    with session() as db:
        rows = db.scalars(select(Screenshot).where(Screenshot.job_id == review_job)).all()
        for row in rows:
            row.selected = row.candidate_id <= 7
            row.info = {
                **row.info,
                "recommendation_rank": None,
                "comparisons": {"src": "old-src.png", "encode": "old-encode.png"},
            }
        db.commit()
    response = client.post(f"/api/jobs/{review_job}/screenshots/review", json={})
    assert response.status_code == 202, response.text
    assert response.json()["type"] == "select_screenshots"
    job = client.get(f"/api/jobs/{review_job}").json()
    assert job["state"] == "SCREENSHOT_AGENT_SELECTION"
    assert sorted(job["analysis"]["review_shortlisted_ids"]) == list(range(1, 41))
    shots = client.get(f"/api/jobs/{review_job}/screenshots").json()
    assert shots["final"] == 7 and shots["shortlisted"] == 40
    assert all(s["info"]["comparisons"]["src"] == "old-src.png" for s in shots["items"] if s["selected"])
    assert client.post(f"/api/jobs/{review_job}/screenshots/review", json={}).status_code == 409


def test_waiting_review_can_refresh_after_other_codec_reserves_frames(client, review_job):
    response = client.post(f"/api/jobs/{review_job}/screenshots/review", json={})
    assert response.status_code == 202, response.text
    assert response.json()["type"] == "select_screenshots"
    job = client.get(f"/api/jobs/{review_job}").json()
    assert job["state"] == "SCREENSHOT_AGENT_SELECTION"
    assert len(job["analysis"]["review_shortlisted_ids"]) == 40
    assert client.get(f"/api/jobs/{review_job}/screenshots").json()["final"] == 0


def test_agent_reuses_all_40_shortlist_images_and_ranks_fifteen(environment, tmp_path, monkeypatch):
    candidates = review_candidates()
    ids = [1 + round(i * 39 / 14) for i in range(15)]
    choices = [
        {
            "candidate_id": candidate_id,
            "score": 1 - rank / 100,
            "category": "representative" if rank < 9 else "encode_challenging",
            "reason": "Fine texture and character detail",
            "shot_type": "close_up",
            "subject": f"subject {rank}",
            "character_visible": True,
        }
        for rank, candidate_id in enumerate(ids)
    ]
    shortlist_choices = [{**choices[0], "candidate_id": i} for i in range(1, 41)]
    inventory = {
        "candidates": candidates,
        "policy": ScreenshotPolicy().model_dump(),
        "contact_sheets": [],
        "shortlisted_ids": list(range(1, 41)),
        "shortlisted_choices": shortlist_choices,
        "duration": 4000,
    }
    (tmp_path / "inventory.json").write_text(json.dumps(inventory))
    for candidate in candidates:
        (tmp_path / candidate["agent_image"]).write_bytes(b"fixture")
    calls = []

    def invoke(self, prompt, images):
        calls.append((prompt, images))
        return Selection(selected=choices), "review-thread"

    monkeypatch.setattr(CodexScreenshotSelector, "_invoke", invoke)
    result = CodexScreenshotSelector().select(tmp_path)
    assert len(calls) == 1 and len(calls[0][1]) == 40
    assert "ranked strongest first" in calls[0][0]
    assert [s["candidate_id"] for s in result["selected"]] == ids
    assert len(result["shortlisted_ids"]) == len(result["shortlisted_choices"]) == 40
    assert result["runs"][0]["stage"] == "review"


def test_shortlist_can_keep_40_from_95_sparse_candidates(environment, tmp_path, monkeypatch):
    candidates = []
    for i in range(1, 96):
        candidate = {
            **review_candidates()[0],
            "candidate_id": i,
            "scene_id": i,
            "timeline_seconds": i * 100,
            "agent_image": f"{i}.png",
            "sheet": f"sheet-{(i - 1) // 16}.jpg",
        }
        candidates.append(candidate)
        (tmp_path / candidate["agent_image"]).touch()
    sheets = [f"sheet-{i}.jpg" for i in range(6)]
    for sheet in sheets:
        (tmp_path / sheet).touch()
    (tmp_path / "inventory.json").write_text(
        json.dumps(
            {
                "candidates": candidates,
                "contact_sheets": sheets,
                "policy": ScreenshotPolicy().model_dump(),
                "duration": 9600,
            }
        )
    )

    def invoke(self, prompt, images):
        if images[0].suffix == ".jpg":
            assert "up to 7 strong frames" in prompt
            ids = [
                c["candidate_id"]
                for image in images
                for c in [c for c in candidates if c["sheet"] == image.name][:7]
            ]
        else:
            assert len(images) == 40
            ids = [int(images[round(i * 39 / 14)].stem) for i in range(15)]
        return Selection(
            selected=[
                {
                    "candidate_id": candidate_id,
                    "score": 1,
                    "category": "representative" if rank < 9 else "encode_challenging",
                    "reason": "Detailed character frame",
                    "shot_type": "close_up",
                    "subject": f"subject {rank}",
                    "character_visible": True,
                }
                for rank, candidate_id in enumerate(ids)
            ]
        ), "thread"

    monkeypatch.setattr(CodexScreenshotSelector, "_invoke", invoke)
    result = CodexScreenshotSelector().select(tmp_path)
    assert len(result["shortlisted_ids"]) == 40
    assert len(result["selected"]) == 15


@pytest.mark.parametrize(
    "seconds,frame,blocked", [(100, 2400, True), (129.9, 9999, True), (130, 9999, False), (900, 2400, True)]
)
def test_gallery_disables_same_or_nearby_cross_codec_frames(client, review_job, seconds, frame, blocked):
    peer = client.post(
        "/api/jobs",
        json={"source_path": "Movie.mkv", "title": "Movie", "year": 2026, "analysis_profile": "x264-live"},
    ).json()
    with session() as db:
        db.add(Screenshot(job_id=peer["id"], candidate_id=1, selected=True, info=review_candidates()[0]))
        row = db.scalar(
            select(Screenshot).where(Screenshot.job_id == review_job, Screenshot.candidate_id == 1)
        )
        row.info = {**row.info, "timeline_seconds": seconds, "source_frame_number": frame}
        db.commit()
    item = client.get(f"/api/jobs/{review_job}/screenshots").json()["items"][0]
    assert bool(item["reservation"]) is blocked
    if blocked:
        assert item["reservation"] == {
            "job_id": peer["id"],
            "codec": "x264",
            "frame_number": 2400,
            "spacing_seconds": 30,
        }
        response = client.post(f"/api/jobs/{review_job}/screenshots/selection", json={"candidate_ids": [1]})
        assert response.status_code == 409
    # Soft deletion releases those reservations immediately.
    with session() as db:
        from shared.models import now

        db.get(MovieJob, peer["id"]).deleted_at = now()
        db.commit()
    assert client.get(f"/api/jobs/{review_job}/screenshots").json()["items"][0]["reservation"] is None
