"""Schemas and language coverage for source-shared subtitle discovery."""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.languages import language_tag


def source_url(value):
    parts = urlsplit(value)
    if (
        len(value) > 4096
        or parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("Sources must use HTTP(S) URLs without credentials")
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DiscoveryPolicy(Strict):
    enabled: bool = False
    original_languages: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("original_languages")
    @classmethod
    def languages(cls, values):
        return list(dict.fromkeys(language_tag(value.strip()) for value in values))


class Candidate(Strict):
    language: str
    source_url: str = Field(max_length=4096)
    download_url: str | None = Field(max_length=4096)
    format: Literal["srt", "ass", "sup", "zip", "unknown"]
    release: str = Field(max_length=1000)
    reason: str = Field(max_length=4000)

    @field_validator("source_url", "download_url")
    @classmethod
    def urls(cls, value):
        return source_url(value) if value is not None else None

    @field_validator("language")
    @classmethod
    def code(cls, value):
        return language_tag(value)


class SearchResult(Strict):
    original_languages: list[str] = Field(max_length=6)
    original_language_sources: list[str] = Field(max_length=8)
    candidates: list[Candidate] = Field(max_length=12)
    summary: str = Field(max_length=8000)

    @field_validator("original_language_sources")
    @classmethod
    def sources(cls, values):
        return [source_url(value) for value in values]

    @field_validator("original_languages")
    @classmethod
    def codes(cls, values):
        return [language_tag(value) for value in values]


class Anchor(Strict):
    candidate_id: int = Field(ge=1)
    reference_id: str = Field(max_length=100)
    explanation: str = Field(min_length=1, max_length=500)


class SubtitleReview(Strict):
    usable: bool
    language: str
    hearing_impaired: bool | None
    issues: list[str] = Field(max_length=30)
    explanation: str = Field(max_length=6000)
    alignment_confident: bool
    scale: float = Field(ge=0.9, le=1.1)
    offset_seconds: float = Field(ge=-600, le=600)
    anchors: list[Anchor] = Field(max_length=24)

    @field_validator("language")
    @classmethod
    def code(cls, value):
        return language_tag(value, allow_unknown=True)


def covers(actual, required):
    try:
        actual, required = language_tag(actual), language_tag(required)
    except ValueError:
        return False
    if required in ("zh-Hans", "zh-Hant"):
        return actual == required or actual.startswith(required + "-")
    return actual.split("-")[0] == required.split("-")[0]


def missing_languages(tracks, originals):
    languages = [
        t.get("language", "und")
        for t in tracks
        if t.get("kind") == "subtitles" and not t.get("forced") and not t.get("commentary")
    ]
    return [
        code
        for code in dict.fromkeys([*originals, "en", "zh-Hans", "zh-Hant"])
        if not any(covers(actual, code) for actual in languages)
    ]


class SubtitleEdit(Strict):
    cue_id: int = Field(ge=1)
    original_text: str = Field(max_length=8000)
    replacement_text: str | None = Field(max_length=8000)
    reason: str = Field(min_length=1, max_length=1000)
    action: Literal["correct_text", "remove_advertisement", "remove_duplicate"]


class SubtitleCleanup(Strict):
    usable: bool
    single_language: bool
    language: str
    edits: list[SubtitleEdit] = Field(max_length=120)
    issues: list[str] = Field(max_length=30)
    explanation: str = Field(max_length=6000)

    @field_validator("language")
    @classmethod
    def code(cls, value):
        return language_tag(value, allow_unknown=True)


def validate_cleanup_edits(review, supplied_cues):
    """Validate edits before agent completion, checkpointing, and application."""
    cues = {cue["id"]: cue for cue in supplied_cues}
    edits = {}
    for edit in review.edits:
        cue = cues.get(edit.cue_id)
        if cue is None:
            invalid = sorted({item.cue_id for item in review.edits if item.cue_id not in cues})
            raise ValueError(
                f"Cleanup edits include non-editable cue IDs {invalid}. "
                f"The only editable cue IDs in this batch are {sorted(cues)}. "
                "Remove edits for context/reference cues; those cues are reviewed in their own batch. "
                "Keep the valid edits for this batch."
            )
        if edit.cue_id in edits:
            raise ValueError(
                f"Cue {edit.cue_id} appears more than once in edits; return one final edit per cue"
            )
        if cue["text"] != edit.original_text:
            raise ValueError(
                f"Cue {edit.cue_id}: original_text must exactly equal the supplied text "
                f"{cue['text']!r}; put corrected dialogue only in replacement_text"
            )
        edits[edit.cue_id] = edit
        replacement = edit.replacement_text
        if edit.action == "correct_text":
            if (
                not replacement
                or not replacement.strip()
                or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]|<[^>]*>|\{[^}]*\}", replacement)
            ):
                raise ValueError(f"Cue {edit.cue_id}: cleanup correction must contain plain dialogue")
        elif replacement is not None:
            raise ValueError(f"Cue {edit.cue_id}: removal must not supply replacement dialogue")
    for edit in review.edits:
        if edit.action != "remove_duplicate":
            continue
        cue = cues[edit.cue_id]
        if not any(
            other["id"] != cue["id"]
            and all(other[key] == cue[key] for key in ("text", "start_ms", "end_ms"))
            and (other["id"] not in edits or edits[other["id"]].action == "correct_text")
            for other in cues.values()
        ):
            raise ValueError(
                f"Cue {edit.cue_id}: remove_duplicate requires another supplied cue with exactly "
                "identical original text, start_ms and end_ms that will be retained. "
                "Adjacent or split dialogue is not a duplicate. Do not merge cues or move dialogue "
                "between their time intervals. Keep each cue and correct its own text separately; "
                "undo any related merge in neighboring edits. Return the complete corrected review."
            )


def blocking_issues(review):
    return [issue for issue in review.issues if issue.lstrip().upper().startswith("BLOCKING:")]
