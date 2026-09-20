import pytest

from agent.schemas import Selection, validate_selection
from shared.config import ScreenshotPolicy


def fixture_selection():
    candidates = [
        {
            "candidate_id": i + 1,
            "scene_id": i,
            "timeline_seconds": t,
            "picture_type": "B",
            "encoded_picture_type": "B",
            "b_frames_verified": True,
        }
        for i, t in enumerate([100, 300, 600, 900])
    ]
    selected = [
        {
            "candidate_id": i + 1,
            "score": 0.9,
            "category": "representative" if i < 2 else "encode_challenging",
            "reason": "Fine texture and representative composition",
            "shot_type": "wide" if i % 2 else "close_up",
            "subject": f"subject {i}",
            "character_visible": True,
        }
        for i in range(4)
    ]
    return candidates, selected


def test_diversity_enforces_unknown_ids_spacing_categories_and_timeline():
    candidates, selected = fixture_selection()
    policy = ScreenshotPolicy(count=4, representative=2, encode_challenging=2)
    assert validate_selection(Selection(selected=selected), candidates, policy, 1000) == []
    selected[0]["candidate_id"] = 999
    assert validate_selection(Selection(selected=selected), candidates, policy, 1000) == [
        "Unknown candidate IDs"
    ]
    selected[0]["candidate_id"] = 1
    candidates[1]["timeline_seconds"] = 101
    candidates[2]["timeline_seconds"] = 102
    candidates[3]["timeline_seconds"] = 103
    errors = validate_selection(Selection(selected=selected), candidates, policy, 1000)
    assert any("seconds apart" in e for e in errors)
    assert any("timeline quarters" in e for e in errors)


def test_agent_schema_rejects_commands_and_invalid_category():
    _, selected = fixture_selection()
    with pytest.raises(ValueError):
        Selection.model_validate({"selected": selected, "command": "HandBrakeCLI --quality 0"})
    selected[0]["category"] = "encode_settings"
    with pytest.raises(ValueError):
        Selection(selected=selected)


def test_policy_category_totals_must_match():
    with pytest.raises(ValueError):
        ScreenshotPolicy(count=5, representative=6, encode_challenging=4)
