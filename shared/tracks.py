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
    evidence_sample_ids: list[int] = Field(
        max_length=64,
        description=(
            "IDs local to this track: audio_analysis.samples[].id for audio; "
            "subtitle_detection.evidence_cues[].id for subtitles. Never cite another "
            "track's IDs, timestamps, or sheet numbers. Use [] when no cues were supplied."
        ),
    )
    confidence: Literal["high", "medium", "low"]


class AudioDistinction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    track_id: int = Field(ge=0, strict=True)
    compared_with: list[int] = Field(max_length=256)
    difference: str = Field(min_length=1, max_length=2500)
    evidence_sample_ids: list[int] = Field(max_length=64)
    resolved: StrictBool


class AudioComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=4000)
    distinctions: list[AudioDistinction] = Field(max_length=256)
    needs_more: StrictBool
    next_track_ids: list[int] = Field(max_length=256)
    question: str = Field(max_length=2500)


class TrackReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tracks: list[TrackReview] = Field(max_length=256)
    # Strict structured outputs require every property, even when its value may be null.
    audio_comparison: AudioComparison | None


def validate_review(result, tracks):
    supplied = {t["track_id"]: t for t in tracks}
    if len(result.tracks) != len(supplied) or {t.track_id for t in result.tracks} != set(supplied):
        raise ValueError("Agent review must cover each supplied track exactly once")
    for review in result.tracks:
        track = supplied[review.track_id]
        if track["kind"] == "audio":
            evidence = track.get("audio_analysis", {}).get("samples", [])
            kind = "audio sample"
        else:
            evidence = track.get("subtitle_detection", {}).get("evidence_cues", [])
            kind = "subtitle cue"
        ids = {sample["id"] for sample in evidence}
        invalid = set(review.evidence_sample_ids) - ids
        if invalid:
            raise ValueError(
                f"Track {review.track_id} review cites {kind} IDs {sorted(invalid)} that were not supplied; "
                f"allowed IDs for this track: {sorted(ids)}"
            )
        if track["kind"] == "subtitles":
            detected = track.get("subtitle_detection", {}).get("hearing_impaired")
            if review.flags.hearing_impaired is not detected:
                raise ValueError("Subtitle SDH must match the content review, including unknown findings")

    comparison = result.audio_comparison
    audio = {t["track_id"]: t for t in tracks if t["kind"] == "audio"}
    if comparison is not None:
        if len(comparison.distinctions) != len(audio) or {d.track_id for d in comparison.distinctions} != set(
            audio
        ):
            raise ValueError("Audio comparison must cover every audio track exactly once")
        for distinction in comparison.distinctions:
            peers = set(distinction.compared_with)
            if peers - (set(audio) - {distinction.track_id}) or (len(audio) > 1 and not peers):
                raise ValueError("Audio distinctions must reference other supplied audio tracks")
            allowed = {
                s["id"] for s in audio[distinction.track_id].get("audio_analysis", {}).get("samples", [])
            }
            if set(distinction.evidence_sample_ids) - allowed:
                raise ValueError("Audio distinction cites a sample not supplied for that track")
        if set(comparison.next_track_ids) - set(audio):
            raise ValueError("Further sampling requested an unknown audio track")
        unresolved = {d.track_id for d in comparison.distinctions if not d.resolved}
        if unresolved and not comparison.needs_more:
            raise ValueError("Unresolved audio differences must request more evidence")
        if comparison.needs_more and (not comparison.next_track_ids or not comparison.question.strip()):
            raise ValueError("Further audio sampling needs track IDs and a concrete unresolved question")


def apply_flag_overrides(track):
    return {**track, **track.get("flag_overrides", {})}


def track_analysis_complete(analysis):
    """Completion is distinct from confidence and must not depend on frontend versions."""
    return (
        "tracks" in analysis
        and analysis.get("track_review_version") == 1
        and all(
            track.get("track_review")
            and (track.get("codec_id") != "S_HDMV/PGS" or track.get("subtitle_detection"))
            for track in analysis.get("tracks", [])
        )
    )
