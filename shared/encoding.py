"""Final video rate-control settings shared by the API and encoder adapter."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def is_smoke_test(job):
    return job.analysis.get("smoke_test") is True


class EncodeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    # Missing mode in previously saved selections means constant quality.
    rate_control: Literal["crf", "bitrate"] = "crf"
    crf: float | None = Field(default=None, ge=0, le=51)
    bitrate_kbps: int | None = Field(default=None, ge=1, le=1_000_000, strict=True)

    @model_validator(mode="after")
    def one_target(self):
        if self.rate_control == "crf":
            if self.crf is None or self.bitrate_kbps is not None:
                raise ValueError("CRF mode requires crf and no bitrate_kbps")
        elif self.bitrate_kbps is None or self.crf is not None:
            raise ValueError("Bitrate (2-pass) mode requires bitrate_kbps and no crf")
        return self
