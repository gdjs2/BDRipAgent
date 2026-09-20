"""Agent track review and explicit user flag overrides."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

FLAG_NAMES = ("default", "forced", "hearing_impaired", "visual_impaired", "commentary")


class TrackFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default: StrictBool | None
    forced: StrictBool | None
    hearing_impaired: StrictBool | None
    visual_impaired: StrictBool | None
    commentary: StrictBool | None


class TrackReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    track_id: int = Field(ge=0, strict=True)
    description: str = Field(min_length=1, max_length=4000)
    flags: TrackFlags
    flag_explanation: str = Field(min_length=1, max_length=4000)
    evidence_sample_ids: list[int] = Field(max_length=12)
    confidence: Literal["high", "medium", "low"]


class TrackReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tracks: list[TrackReview] = Field(max_length=256)


def validate_review(result, tracks):
    supplied = {t["track_id"]: t for t in tracks}
    if len(result.tracks) != len(supplied) or {t.track_id for t in result.tracks} != set(supplied):
        raise ValueError("Agent review must cover each supplied track exactly once")
    for review in result.tracks:
        track = supplied[review.track_id]
        evidence = track.get("audio_analysis", {}) if track["kind"] == "audio" else {}
        ids = {s["id"] for s in evidence.get("samples", [])}
        if set(review.evidence_sample_ids) - ids:
            raise ValueError("Track review cites an audio sample that was not supplied")
        if track["kind"] == "subtitles":
            detected = track.get("subtitle_detection", {}).get("hearing_impaired")
            if review.flags.hearing_impaired is not detected:
                raise ValueError("Subtitle SDH must match the content review, including unknown findings")


def apply_flag_overrides(track):
    return {**track, **track.get("flag_overrides", {})}
