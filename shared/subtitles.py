"""Content-derived subtitle decisions shared by the worker and restricted agent."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.languages import language_tag

SUBTITLE_ANALYSIS_VERSION = 2


class SubtitleDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["chinese", "cantonese", "other", "unknown"]
    language_code: str = Field(min_length=2, max_length=64)
    script: Literal["simplified", "traditional", "mixed", "unknown", "not_applicable"]
    language_confident: bool
    hearing_impaired: bool | None
    sdh_confident: bool
    language_evidence: list[int] = Field(max_length=12)
    sdh_evidence: list[int] = Field(max_length=12)
    explanation: str = Field(min_length=1, max_length=4000)

    @field_validator("language_code")
    @classmethod
    def canonical_code(cls, value):
        return language_tag(value, allow_unknown=True)

    @model_validator(mode="after")
    def consistent(self):
        if self.language_confident and (
            self.language == "unknown"
            or (
                self.language in ("chinese", "cantonese") and self.script not in ("simplified", "traditional")
            )
            or (self.language == "other" and self.script != "not_applicable")
        ):
            raise ValueError("A confident language decision must identify a consistent language and script")
        if self.language_confident:
            if self.language_code == "und":
                raise ValueError("A confident language decision requires a specific language code")
            if self.language in ("chinese", "cantonese"):
                expected = ("yue" if self.language == "cantonese" else "zh") + (
                    "-Hans" if self.script == "simplified" else "-Hant"
                )
                if self.language_code != expected:
                    raise ValueError(f"Language code must match the detected Chinese variety: {expected}")
            elif self.language_code.split("-")[0] in ("zh", "yue"):
                raise ValueError("Chinese and Cantonese codes require their corresponding language category")
        elif self.language_code != "und":
            raise ValueError("Uncertain language decisions must use und, not a guessed language code")
        if self.sdh_confident and self.hearing_impaired is None:
            raise ValueError("A confident SDH decision cannot be unknown")
        return self


def validate_evidence(decision, cue_ids):
    for confident, evidence in (
        (decision.language_confident, decision.language_evidence),
        (decision.sdh_confident, decision.sdh_evidence),
    ):
        if any(cue_id not in cue_ids for cue_id in evidence):
            raise ValueError("Subtitle decision cites a cue outside the supplied sample")
        if confident and not evidence:
            raise ValueError("Confident subtitle decisions must cite visible cue evidence")


def detected_language(decision, original=None):
    # Original metadata is deliberately not a fallback for any language.
    return decision.language_code if decision.language_confident else "und"


def subtitle_is_resolved(track):
    if track.get("origin") == "upload" and not track.get("discovery"):
        return bool(track.get("upload_validated") and track.get("language_override")) and (
            language_tag(track["language_override"]) == track.get("language")
            and isinstance(track.get("hearing_impaired"), bool)
        )
    detection = track.get("subtitle_detection", {})
    manual_language = track.get("language_override")
    language_resolved = (
        bool(manual_language) and language_tag(manual_language) == track.get("language")
    ) or (
        detection.get("language_code") == track.get("language")
        and detection.get("language_code") not in (None, "und")
        and detection.get("language_confident", detection.get("status", "resolved") == "resolved")
    )
    return (
        detection.get("schema_version") == SUBTITLE_ANALYSIS_VERSION
        and language_resolved
        and (
            detection.get("sdh_confident", detection.get("status", "resolved") == "resolved")
            or isinstance(track.get("flag_overrides", {}).get("hearing_impaired"), bool)
        )
        and isinstance(track.get("hearing_impaired"), bool)
    )
