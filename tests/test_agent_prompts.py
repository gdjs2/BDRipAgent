import importlib
import json
from types import SimpleNamespace

import pytest

from shared.agent_prompts import CATALOG, default_text, request_prompt, saved_prompts, task_prompts
from shared.db import session
from shared.models import AgentPrompt
from worker.adapters import subtitle_resume
from worker.adapters.track_cache import analysis_policy


def save(client, key, text, revision=0):
    response = client.put(f"/api/agent/prompts/{key}", json={"revision": revision, "text": text})
    assert response.status_code == 200, response.text
    return response.json()


def test_prompt_catalog_persistence_reset_and_concurrent_edits(client):
    values = client.get("/api/agent/prompts").json()
    assert {p["key"] for p in values} == set(CATALOG)
    assert all(p["text"] == p["default_text"] and p["revision"] == 0 for p in values)
    text = "Check EVERY cue.\n保留术语一致性。\nUse literal {braces} and $text.\n"
    edited = save(client, "subtitle_cleanup", text)
    assert edited["customized"] and edited["revision"] == 1 and edited["text"] == text
    with session() as db:
        assert db.get(AgentPrompt, "subtitle_cleanup").text == text
    stale = client.put("/api/agent/prompts/subtitle_cleanup", json={"revision": 0, "text": "Overwrite"})
    assert stale.status_code == 409
    reset = save(client, "subtitle_cleanup", None, 1)
    assert not reset["customized"] and reset["revision"] == 2
    assert reset["text"] == default_text("subtitle_cleanup")
    assert (
        client.put(
            "/api/agent/prompts/subtitle_cleanup", json={"revision": 1, "text": "Stale again"}
        ).status_code
        == 409
    )


def test_prompt_auth_and_validation(client):
    for text in ["", "  \n", "\x00", "x" * 50001]:
        assert (
            client.put("/api/agent/prompts/track_review", json={"revision": 0, "text": text}).status_code
            == 422
        )
    assert client.put("/api/agent/prompts/unknown", json={"revision": 0, "text": "Review"}).status_code == 404
    client.headers.clear()
    assert client.get("/api/agent/prompts").status_code == 401
    assert (
        client.put("/api/agent/prompts/track_review", json={"revision": 0, "text": "Review"}).status_code
        == 401
    )


def test_task_snapshot_and_cache_hashes_remain_consistent_after_edit(client):
    ctx = SimpleNamespace()
    frozen = task_prompts(ctx)
    inventory = {"mode": "clean", "cues": [], "agent_prompt": frozen["subtitle_cleanup"]}
    signature = subtitle_resume.signature(inventory)
    policy = analysis_policy({}, frozen)
    save(client, "subtitle_cleanup", "Repair more carefully.")
    save(client, "track_review", "Compare commentary against the main soundtrack.")
    assert task_prompts(ctx) is frozen
    assert subtitle_resume.signature(inventory) == signature
    assert analysis_policy({}, frozen) == policy
    fresh = task_prompts(SimpleNamespace())
    assert subtitle_resume.signature({**inventory, "agent_prompt": fresh["subtitle_cleanup"]}) != signature
    assert analysis_policy({}, fresh) != policy
    assert request_prompt({"agent_prompt": fresh["track_review"]}, "track_review")["text"].startswith(
        "Compare"
    )
    with pytest.raises(ValueError):
        request_prompt({"agent_prompt": {**fresh["track_review"], "text": "tampered"}}, "track_review")


@pytest.mark.parametrize(
    "key,module,method,inventory",
    [
        ("track_review", "agent.track_agent", "review", {"tracks": [{"track_id": 1, "kind": "audio"}]}),
        (
            "subtitle_classification",
            "agent.subtitle_agent",
            "classify",
            {"track_id": 1, "samples": [{"id": 1}], "contact_sheets": ["sheet.png"]},
        ),
        ("subtitle_discovery", "agent.subtitle_discovery", "run", {"mode": "search"}),
        ("subtitle_cleanup", "agent.subtitle_discovery", "run", {"mode": "clean"}),
        ("subtitle_alignment", "agent.subtitle_discovery", "run", {"mode": "align"}),
    ],
)
def test_each_reviewer_receives_saved_instructions_and_traces_revision(
    client, tmp_path, monkeypatch, key, module, method, inventory
):
    text = "My custom instructions: check details and explain uncertainty."
    save(client, key, text)
    selected = saved_prompts()[key]
    (tmp_path / "sheet.png").touch()
    (tmp_path / "inventory.json").write_text(json.dumps({**inventory, "agent_prompt": selected}))
    agent = importlib.import_module(module)
    seen = []

    def review(**args):
        assert args["prompt"].startswith(text + "\n")
        assert args["prompt"].count(text) == 1
        assert args["schema"]["type"] == "object"
        args["on_event"]({"type": "prompt", "text": args["prompt"]})
        return SimpleNamespace(model_dump=lambda: {"tracks": []}), "thread"

    monkeypatch.setattr(agent, "validated_review", review)
    cls = getattr(
        agent,
        {
            "review": "CodexTrackReviewer",
            "classify": "CodexSubtitleClassifier",
            "run": "CodexSubtitleDiscovery",
        }[method],
    )
    getattr(cls(on_event=seen.append), method)(tmp_path)
    assert seen[0]["prompt_key"] == key and seen[0]["prompt_revision"] == 1
    assert seen[0]["prompt_sha256"] == selected["sha256"]


def test_screenshot_calls_use_saved_prompt(client, tmp_path, monkeypatch):
    from agent.schemas import Selection
    from agent.screenshot_agent import CodexScreenshotSelector
    from shared.config import ScreenshotPolicy
    from tests.test_screenshot_review import review_candidates
    from tests.test_screenshot_strategy import choice

    save(client, "screenshot_selection", "Prefer detailed faces and natural lighting.")
    candidates = review_candidates()[:10]
    for item in candidates:
        item["sheet"] = "sheet.jpg"
    (tmp_path / "sheet.jpg").touch()
    (tmp_path / "inventory.json").write_text(
        json.dumps(
            {
                "candidates": candidates,
                "policy": ScreenshotPolicy(strategy="agent").model_dump(),
                "duration": 4000,
                "contact_sheets": ["sheet.jpg"],
                "allow_partial": False,
                "agent_prompt": saved_prompts()["screenshot_selection"],
            }
        )
    )
    calls = []

    def invoke(self, prompt, images):
        calls.append(prompt)
        return Selection(selected=[choice(1), choice(3)]), "test"

    monkeypatch.setattr(CodexScreenshotSelector, "_invoke", invoke)
    CodexScreenshotSelector().select(tmp_path)
    assert len(calls) == 1 and calls[0].startswith("Prefer detailed faces and natural lighting.")
    assert "Policy:" in calls[0] and "Inventory:" in calls[0]


def test_edited_prompts_do_not_reuse_legacy_or_previous_track_findings(client, new_job, environment):
    from tests.test_source_sharing import context, result
    from worker.adapters.track_cache import SharedTrackAnalysis

    donor = context(environment, new_job["id"])
    old = SharedTrackAnalysis(donor)
    value = {**result(donor), "track_analysis_signature": old.signature}
    assert not old.needs_review(value)
    save(client, "track_review", "New comparison instructions")
    current = SharedTrackAnalysis(context(environment, new_job["id"]))
    assert current.needs_review(value)
    assert current.needs_review({})
    assert not old.needs_review(value), "An active task must retain its frozen instructions"
