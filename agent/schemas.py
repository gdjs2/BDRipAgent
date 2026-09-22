from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.config import ScreenshotPolicy
from shared.screenshot_rules import is_b_frame_pair


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    candidate_id: int
    score: float = Field(ge=0, le=1)
    category: Literal["representative", "encode_challenging"]
    reason: str = Field(min_length=1, max_length=2000)
    shot_type: Literal["close_up", "medium", "wide", "detail"]
    subject: str = Field(min_length=1, max_length=200)
    character_visible: bool


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected: list[Choice]


def review_policy(policy: ScreenshotPolicy, available: int) -> ScreenshotPolicy:
    """The agent proposes the requested review count; the user chooses the final set."""
    count = min(policy.best_count, available)
    if count < 2:
        raise ValueError("Not enough candidates for screenshot review")
    representative = round(count * policy.representative / policy.count)
    return policy.model_copy(
        update={
            "count": count,
            "representative": representative,
            "encode_challenging": count - representative,
            "min_timeline_bins": min(policy.min_timeline_bins, count),
        }
    )


def validate_selection(selection: Selection, candidates, policy: ScreenshotPolicy, duration, *, final=True):
    errors = []
    mapping = {c["candidate_id"]: c for c in candidates}
    ids = [s.candidate_id for s in selection.selected]
    if len(ids) != len(set(ids)):
        errors.append("Duplicate candidate IDs")
    if any(i not in mapping for i in ids):
        return ["Unknown candidate IDs"]
    if any(not is_b_frame_pair(mapping[i]) for i in ids):
        errors.append("Both source and encoded images must be verified B-frames")
    if not final:
        if len(ids) < policy.count:
            errors.append("Shortlist is too small")
        return errors
    if len(ids) != policy.count:
        errors.append(f"Select exactly {policy.count} candidates")
    counts = Counter(s.category for s in selection.selected)
    for category in ["representative", "encode_challenging"]:
        if counts[category] != getattr(policy, category):
            errors.append(f"Need exactly {getattr(policy, category)} {category} frames")
    points = sorted(mapping[i]["timeline_seconds"] for i in ids)
    if any(b - a < policy.min_spacing_seconds for a, b in zip(points, points[1:])):
        errors.append(f"Keep frames at least {policy.min_spacing_seconds} seconds apart")
    scenes = Counter(mapping[i]["scene_id"] for i in ids)
    if max(scenes.values(), default=0) > policy.max_per_scene:
        errors.append(f"At most {policy.max_per_scene} frames per detected scene")
    if duration <= 0 or len({min(3, int(t / duration * 4)) for t in points}) < policy.min_timeline_bins:
        errors.append(f"Cover at least {policy.min_timeline_bins} timeline quarters")
    shots = {s.shot_type for s in selection.selected}
    if policy.count >= 4 and "close_up" not in shots:
        errors.append("Include character close-ups")
    minimum_characters = (policy.count * 2 + 2) // 3
    if sum(s.character_visible for s in selection.selected) < minimum_characters:
        errors.append(f"At least {minimum_characters} frames must visibly feature characters")
    subjects = Counter(s.subject.strip().lower() for s in selection.selected)
    if policy.count >= 4 and max(subjects.values(), default=0) > (policy.count + 1) // 2:
        errors.append("No subject may dominate more than half the final set")
    return errors
