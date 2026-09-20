"""Content-derived subtitle decisions shared by the worker and restricted agent."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SubtitleDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["chinese", "cantonese", "other", "unknown"]
    script: Literal["simplified", "traditional", "mixed", "unknown", "not_applicable"]
    language_confident: bool
    hearing_impaired: bool | None
    sdh_confident: bool
    language_evidence: list[int] = Field(max_length=12)
    sdh_evidence: list[int] = Field(max_length=12)
    explanation: str = Field(min_length=1, max_length=4000)

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


def detected_language(decision, original):
    if decision.language in ("chinese", "cantonese"):
        return ("yue" if decision.language == "cantonese" else "zh") + (
            "-Hans" if decision.script == "simplified" else "-Hant"
        )
    # This classifier distinguishes Chinese variants; other language tags are retained.
    if original.lower().split("-")[0] in ("zh", "zho", "chi", "cmn", "yue"):
        raise ValueError("Subtitle content does not support the source Chinese language tag")
    return original


def subtitle_is_resolved(track):
    detection = track.get("subtitle_detection", {})
    return (
        detection.get("schema_version") == 1
        and detection.get("language_confident", detection.get("status", "resolved") == "resolved")
        and (
            detection.get("sdh_confident", detection.get("status", "resolved") == "resolved")
            or isinstance(track.get("flag_overrides", {}).get("hearing_impaired"), bool)
        )
        and isinstance(track.get("hearing_impaired"), bool)
    )
