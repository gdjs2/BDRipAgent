import unicodedata
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from backend.app.movie_metadata import normalize_imdb_id
from shared.config import ScreenshotDecoder, ScreenshotPolicy
from shared.encoding import EncodeTarget
from shared.languages import language_tag
from shared.subtitle_discovery import DiscoveryPolicy
from shared.tracks import FLAG_NAMES


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CreateJob(StrictModel):
    source_path: str
    title: str | None = Field(default=None, min_length=1, max_length=300)
    year: int | None = Field(default=None, ge=1880, le=2200)
    imdb_id: str | None = None
    analysis_profile: str = "x265-live"
    screenshot_policy: ScreenshotPolicy | None = None
    subtitle_discovery: DiscoveryPolicy = Field(default_factory=DiscoveryPolicy)
    audio_review_max_rounds: int | None = Field(default=None, ge=1, le=30, strict=True)

    @field_validator("title")
    @classmethod
    def printable(cls, value):
        if value is None:
            return value
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Text must be nonempty and contain no control characters")
        return value.strip()

    @field_validator("imdb_id")
    @classmethod
    def valid_imdb_id(cls, value):
        return normalize_imdb_id(value) if value and value.strip() else None

    @model_validator(mode="after")
    def identity_required(self):
        if not self.imdb_id and (self.title is None or self.year is None):
            raise ValueError("Provide an IMDb ID, or enter both the movie title and release year")
        return self


class CreatePair(CreateJob):
    second_profile: str = "x264-live"


class SelectTracks(StrictModel):
    shared_revision: int | None = Field(default=None, ge=0, strict=True)
    audio_track_ids: list[int] = Field(
        description="Selected audio track IDs in final output order, after video"
    )
    subtitle_track_ids: list[int] = Field(
        description="Selected subtitle track IDs in final output order, after audio"
    )
    track_names: dict[int, str] = Field(default_factory=dict)
    track_flags: dict[int, dict[str, bool]] = Field(default_factory=dict)
    track_languages: dict[int, str] = Field(default_factory=dict)

    @field_validator("track_languages")
    @classmethod
    def valid_languages(cls, value):
        return {track_id: language_tag(code.strip()) for track_id, code in value.items()}

    @field_validator("track_flags", mode="before")
    @classmethod
    def valid_flags(cls, value):
        if not isinstance(value, dict):
            raise ValueError("Track flags must map track IDs to flag choices")
        for flags in value.values():
            if (
                not isinstance(flags, dict)
                or set(flags) - set(FLAG_NAMES)
                or any(type(v) is not bool for v in flags.values())
            ):
                raise ValueError("Choose only supported track flags using true or false")
        return value

    @field_validator("track_names")
    @classmethod
    def valid_names(cls, value):
        names = {}
        for track_id, name in value.items():
            name = name.strip()
            if (
                track_id < 0
                or not name
                or len(name) > 255
                or any(unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp") for c in name)
            ):
                raise ValueError(
                    "Track names must be 1–255 characters on one line without control characters"
                )
            names[track_id] = name
        return names

    @model_validator(mode="after")
    def names_for_selected_tracks(self):
        if (self.track_names.keys() | self.track_flags.keys() | self.track_languages.keys()) - set(
            self.audio_track_ids + self.subtitle_track_ids
        ):
            raise ValueError("Custom names, flags, and languages may only be supplied for selected tracks")
        return self

    @field_validator("audio_track_ids", "subtitle_track_ids")
    @classmethod
    def unique_ids(cls, value):
        if len(value) != len(set(value)) or any(x < 0 for x in value):
            raise ValueError("Track IDs must be unique nonnegative integers")
        return value


class SelectEncode(EncodeTarget):
    codec: str
    profile: str


class Login(StrictModel):
    token: str


class ReplaceScreenshot(StrictModel):
    candidate_id: int


class SelectScreenshotDecoder(StrictModel):
    decoder: ScreenshotDecoder


class SelectScreenshotBestCount(StrictModel):
    best_count: int = Field(ge=2, le=40, strict=True)


class ReviewScreenshots(StrictModel):
    best_count: int | None = Field(default=None, ge=2, le=40, strict=True)


class SelectScreenshots(StrictModel):
    candidate_ids: list[StrictInt] = Field(min_length=1, max_length=15)

    @field_validator("candidate_ids")
    @classmethod
    def unique_candidates(cls, value):
        if len(value) != len(set(value)) or any(candidate < 1 for candidate in value):
            raise ValueError("Choose unique positive candidate IDs")
        return value


class CurateScreenshots(SelectScreenshots):
    candidate_ids: list[StrictInt] = Field(max_length=40)


class ReleaseSource(StrictModel):
    source: str = Field(min_length=1, max_length=1000)

    @field_validator("source")
    @classmethod
    def source_text(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Source must be a single line without control characters")
        value = value.strip()
        if not value:
            raise ValueError("Source is required")
        return value


class ReleaseDetails(ReleaseSource):
    upload_screenshots: bool = Field(default=False, strict=True)
    chinese_name: str = Field(min_length=1, max_length=300)
    extra_description: str = Field(default="", max_length=6000)
    movie_description: str = Field(default="", max_length=100000)
    tracker: str = Field(min_length=1, max_length=4096)

    @field_validator("chinese_name", "extra_description", "movie_description", "tracker")
    @classmethod
    def clean_text(cls, value, info):
        value = value.strip()
        if any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("Text contains unsupported control characters")
        if info.field_name not in ("extra_description", "movie_description") and not value:
            raise ValueError("This field is required")
        return value

    @field_validator("tracker")
    @classmethod
    def tracker_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("https", "http", "udp") or not parsed.hostname:
            raise ValueError("Tracker must be an absolute HTTP, HTTPS, or UDP announce URL")
        if any(c.isspace() for c in value) or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("Tracker must not contain whitespace, fragments, or user credentials")
        if parsed.port is not None and parsed.port < 1:
            raise ValueError("Invalid tracker port")
        return value


class SaveReleaseDetails(ReleaseDetails):
    shared_revision: int | None = Field(default=None, ge=0, strict=True)


class UpdateQueue(StrictModel):
    max_encoding_tasks: int | None = Field(default=None, ge=1, le=64, strict=True)
    max_crf_tasks: int | None = Field(default=None, ge=1, le=64, strict=True)
    max_other_tasks: int | None = Field(default=None, ge=1, le=64, strict=True)
    paused: bool | None = Field(default=None, strict=True)


class HoldTask(StrictModel):
    held: bool = Field(strict=True)


class OrderQueue(StrictModel):
    task_ids: list[UUID]

    @field_validator("task_ids")
    @classmethod
    def unique_tasks(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Queue task IDs must be unique")
        return value
