import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from worker.adapters.audio_review import diffusion_starts

from shared.tracks import TrackReviewResult, validate_review
from tests.test_track_review import agent_answer
from worker.adapters import track_review
from worker.runtime import Interrupted


def test_diffusion_spreads_into_largest_unexamined_intervals():
    used = [(135, 30), (285, 30), (435, 30)]
    starts = diffusion_starts(600, 30, used)
    assert len(starts) == 2
    assert all(start + 30 <= left or start >= left + length for start in starts for left, length in used)
    assert max(starts) - min(starts) > 400
    assert diffusion_starts(20, 30, [(0, 20)]) == []


@pytest.fixture
def adaptive(tmp_path, environment, monkeypatch):
    calls = {"agent": 0, "sampling": []}
    result = {
        "video": {"duration": 7200},
        "tracks": [
            {
                "track_id": i,
                "kind": "audio",
                "language": "en",
                "codec": "AC3",
                "audio_analysis": {
                    "method": "local decoding and speech transcription",
                    "samples": [
                        {
                            "id": 1,
                            "start_seconds": 1000,
                            "duration_seconds": 30,
                            "segments": [{"text": "Same dialogue"}],
                        }
                    ],
                    "limitations": [],
                },
            }
            for i in (1, 2)
        ],
    }
    ctx = SimpleNamespace(
        settings=environment,
        workspace=tmp_path,
        job=SimpleNamespace(id=str(uuid4())),
        task_id=str(uuid4()),
        check=lambda: None,
        log=lambda text: None,
        progress=lambda *a, **kw: None,
        artifact=lambda path, *a, **kw: str(path),
    )

    def output(category, name):
        path = tmp_path / category / name
        path.parent.mkdir(exist_ok=True)
        return path

    ctx.output = output

    def sample(ctx, tracks, duration, *, starts):
        calls["sampling"].append(starts)
        evidence = {}
        for track in tracks:
            data = copy.deepcopy(track["audio_analysis"])
            for start in starts:
                assert all(abs(s["start_seconds"] - start) >= 30 for s in data["samples"])
                data["samples"].append(
                    {
                        "id": max(s["id"] for s in data["samples"]) + 1,
                        "start_seconds": start,
                        "duration_seconds": 30,
                        "segments": [{"text": "Additional content"}],
                    }
                )
            evidence[track["track_id"]] = data
        return evidence

    monkeypatch.setattr(track_review, "analyze_audio", sample)
    return ctx, result, calls


def test_agent_controls_sampling_beyond_fixed_rounds_and_retry_reuses_evidence(adaptive, monkeypatch):
    ctx, result, calls = adaptive
    checkpoints = []
    fail = {"once": True}

    def review(ctx):
        calls["agent"] += 1
        if calls["agent"] == 3 and fail["once"]:
            fail["once"] = False
            raise Interrupted("cancelled")
        inventory = json.loads(
            (
                ctx.settings.cache_root / "agent" / ctx.job.id / ctx.task_id / "tracks/inventory.json"
            ).read_text()
        )
        answer = agent_answer(inventory["tracks"])
        more = calls["agent"] < 7
        answer["audio_comparison"].update(
            needs_more=more,
            next_track_ids=[1] if more else [],
            question="Is track 2 commentary or ordinary dialogue?" if more else "",
        )
        for item in answer["audio_comparison"]["distinctions"]:
            item["resolved"] = not more
        return answer

    monkeypatch.setattr(track_review, "review", review)
    with pytest.raises(Interrupted):
        track_review.review_tracks(
            ctx, result, on_progress=lambda value: checkpoints.append(copy.deepcopy(value))
        )
    assert len(calls["sampling"]) == 2
    # Retry from a durable checkpoint; its prepared samples must not repeat.
    saved = checkpoints[-1]
    track_review.review_tracks(ctx, saved)
    assert saved["audio_comparison"]["status"] == "resolved"
    assert saved["audio_comparison"]["rounds"] == 6
    assert len(calls["sampling"]) == 5
    assert len({s for starts in calls["sampling"] for s in starts}) == 10


def test_exhausted_timeline_finishes_with_explicit_uncertainty(adaptive, monkeypatch):
    ctx, result, calls = adaptive
    result["video"]["duration"] = 30
    for track in result["tracks"]:
        track["audio_analysis"]["samples"][0]["start_seconds"] = 0
    answer = agent_answer(result["tracks"])
    answer["audio_comparison"].update(needs_more=True, next_track_ids=[1, 2], question="Content role unclear")
    for item in answer["audio_comparison"]["distinctions"]:
        item["resolved"] = False
    monkeypatch.setattr(track_review, "review", lambda ctx: answer)
    track_review.review_tracks(ctx, result)
    assert result["audio_comparison"]["status"] == "inconclusive"
    assert "No unexamined" in result["audio_comparison"]["stop_reason"]
    assert not calls["sampling"]


def test_comparison_cannot_invent_peer_tracks_or_evidence(adaptive):
    _, result, _ = adaptive
    answer = agent_answer(result["tracks"])
    answer["audio_comparison"]["distinctions"][0]["compared_with"] = [99]
    with pytest.raises(ValueError, match="other supplied"):
        validate_review(TrackReviewResult.model_validate(answer), result["tracks"])
    answer = agent_answer(result["tracks"])
    answer["audio_comparison"]["distinctions"][0]["evidence_sample_ids"] = [99]
    with pytest.raises(ValueError, match="sample not supplied"):
        validate_review(TrackReviewResult.model_validate(answer), result["tracks"])


def test_configured_hard_limit_stops_sampling_and_hands_off_to_user(adaptive, monkeypatch):
    ctx, result, calls = adaptive
    result["audio_review_policy"] = {"max_rounds": 2}
    budgets = []

    def review(ctx):
        inventory = json.loads(
            (
                ctx.settings.cache_root / "agent" / ctx.job.id / ctx.task_id / "tracks/inventory.json"
            ).read_text()
        )
        budgets.append(inventory["analysis_budget"])
        answer = agent_answer(inventory["tracks"])
        answer["audio_comparison"].update(
            needs_more=True,
            next_track_ids=[1, 2],
            question="Possibly a different dub; manual listening is needed.",
        )
        for item in answer["audio_comparison"]["distinctions"]:
            item["resolved"] = False
            item["difference"] = "Dialogue overlaps; this may be an alternate mix. Listen to distinguish it."
        return answer

    monkeypatch.setattr(track_review, "review", review)
    tracks = track_review.review_tracks(ctx, result)
    assert len(budgets) == 2 and len(calls["sampling"]) == 1
    assert budgets[-1]["final_round"] and budgets[-1]["remaining_rounds_after_this"] == 0
    assert result["audio_comparison"]["requires_human"]
    assert "limit of 2" in result["audio_comparison"]["stop_reason"]
    assert result["audio_comparison"]["status"] == "inconclusive"
    assert all(t["track_review"] for t in tracks)
    assert result["audio_review_next"] is None

    result["tracks"] = tracks
    track_review.review_tracks(ctx, result)
    assert len(budgets) == 2, "Retry must not exceed the saved hard limit"
