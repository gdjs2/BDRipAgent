from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
ScreenshotDecoder = Literal["cpu", "cuda"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://movie:movie@postgres/movie"
    redis_url: str = "redis://redis:6379/0"
    source_root: Path = Path("/source")
    workspace_root: Path = Path("/workspace")
    completed_root: Path = Path("/completed")
    artifacts_root: Path = Path("/artifacts")
    cache_root: Path = Path("/cache")
    config_path: Path = ROOT / "config/application.yaml"
    profiles_root: Path = ROOT / "profiles"
    api_token: str = ""
    agent_token: str = ""
    agent_url: str = "http://agent:8001"
    cookie_secure: bool = False
    handbrake_bin: str = "HandBrakeCLI"
    mkvmerge_bin: str = "mkvmerge"
    mkvextract_bin: str = "mkvextract"
    mediainfo_bin: str = "mediainfo"
    ffprobe_bin: str = "ffprobe"
    ffmpeg_bin: str = "ffmpeg"
    crf_studio_bin: str = "/opt/crf-studio/.venv/bin/bdrip"
    sup2sup_bin: str = "/opt/sup2sup/.venv/bin/sup2sup"
    audio_review_python: str = "/opt/audio-review/bin/python"
    bdrip_python: str = "/opt/crf-studio/.venv/bin/python"
    tu_ttg_token: SecretStr = SecretStr("")
    codex_bin: str = "codex"
    imdb_timeout_seconds: float = Field(default=25, ge=1, le=55)
    worker_capacity: int = Field(default=8, ge=1, le=64)


class ScreenshotPolicy(BaseModel):
    decoder: ScreenshotDecoder = "cuda"
    count: int = Field(default=7, ge=2, le=30)
    representative: int = Field(default=4, ge=0)
    encode_challenging: int = Field(default=3, ge=0)
    min_spacing_seconds: float = Field(default=30, ge=0)
    min_timeline_bins: int = Field(default=3, ge=1, le=4)
    max_per_scene: int = Field(default=1, ge=1)
    policy: str = Field(
        default="Focus on characters, faces, hair, clothing, fine texture, grain and dark detail. "
        "Avoid credits, title cards, blur, fades, transitions and repeated shots.",
        max_length=12000,
    )

    @model_validator(mode="after")
    def category_counts(self):
        if self.representative + self.encode_challenging != self.count:
            raise ValueError("Screenshot category counts must add up to count")
        if self.min_timeline_bins > self.count:
            raise ValueError("Timeline bins cannot exceed screenshot count")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def behavior() -> dict:
    return yaml.safe_load(get_settings().config_path.read_text())


def profiles() -> dict:
    return {
        p.stem: yaml.safe_load(p.read_text()) for p in sorted(get_settings().profiles_root.glob("*.yaml"))
    }
