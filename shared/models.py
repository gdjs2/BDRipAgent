from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base

DATA = JSON().with_variant(JSONB(), "postgresql")


def now():
    return datetime.now(UTC)


def uid():
    return str(uuid4())


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100), unique=True)


class MovieJob(Base):
    __tablename__ = "movie_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_path: Mapped[str] = mapped_column(Text)
    source_size: Mapped[int] = mapped_column(type_=BigInteger)
    source_mtime_ns: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    year: Mapped[int | None]
    imdb_id: Mapped[str | None] = mapped_column(String(16))
    imdb_metadata: Mapped[dict | None] = mapped_column(DATA)
    release_name: Mapped[str] = mapped_column(String(300))
    analysis_profile: Mapped[str] = mapped_column(String(100), default="x265-live")
    state: Mapped[str] = mapped_column(String(60), default="NEW", index=True)
    manifest_path: Mapped[str] = mapped_column(Text, default="manifest.yaml")
    codex_thread_id: Mapped[str | None] = mapped_column(String(100))
    analysis: Mapped[dict] = mapped_column(DATA, default=dict)
    screenshot_policy: Mapped[dict] = mapped_column(DATA, default=dict)
    validation: Mapped[dict] = mapped_column(DATA, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MovieTrack(Base):
    __tablename__ = "movie_tracks"
    __table_args__ = (UniqueConstraint("job_id", "track_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("movie_jobs.id"), index=True)
    track_id: Mapped[int]
    kind: Mapped[str] = mapped_column(String(20))
    info: Mapped[dict] = mapped_column(DATA)


class TrackSelection(Base):
    __tablename__ = "track_selections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("movie_jobs.id"), unique=True)
    audio_track_ids: Mapped[list] = mapped_column(DATA)
    subtitle_track_ids: Mapped[list] = mapped_column(DATA)
    selected_by: Mapped[str] = mapped_column(String(100), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index(
            "one_active_task_per_job",
            "job_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED','RUNNING')"),
            sqlite_where=text("status IN ('QUEUED','RUNNING')"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("movie_jobs.id"), index=True)
    type: Mapped[str] = mapped_column(String(60))
    stage: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    attempt: Mapped[int] = mapped_column(default=1)
    retry_of: Mapped[str | None] = mapped_column(String(36))
    progress: Mapped[float] = mapped_column(Float, default=0)
    progress_detail: Mapped[dict] = mapped_column(DATA, default=dict)
    command_json: Mapped[list] = mapped_column(DATA, default=list)
    worker_id: Mapped[str | None] = mapped_column(String(200))
    run_token: Mapped[str | None] = mapped_column(String(36))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    can_pause: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    held: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    queue_priority: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    exit_code: Mapped[int | None]
    log_path: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str | None] = mapped_column(Text)


class QueueSettings(Base):
    __tablename__ = "queue_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="queue_singleton"),
        CheckConstraint("max_concurrent_jobs BETWEEN 1 AND 64", name="queue_limit_bounds"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("job_id", "path"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("movie_jobs.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    artifact_type: Mapped[str] = mapped_column(String(50))
    path: Mapped[str] = mapped_column(Text)
    storage: Mapped[str] = mapped_column(String(20), default="workspace")
    size: Mapped[int] = mapped_column(type_=BigInteger)
    checksum: Mapped[str | None] = mapped_column(String(100))
    info: Mapped[dict] = mapped_column(DATA, default=dict)


class CRFResult(Base):
    __tablename__ = "crf_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("movie_jobs.id"), unique=True)
    data: Mapped[dict] = mapped_column(DATA)


class EncodeConfig(Base):
    __tablename__ = "encode_configs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("movie_jobs.id"), unique=True)
    data: Mapped[dict] = mapped_column(DATA)
    selected_by: Mapped[str] = mapped_column(String(100), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Screenshot(Base):
    __tablename__ = "screenshots"
    __table_args__ = (UniqueConstraint("job_id", "candidate_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("movie_jobs.id"), index=True)
    candidate_id: Mapped[int]
    selected: Mapped[bool] = mapped_column(default=False)
    shortlisted: Mapped[bool] = mapped_column(default=False)
    info: Mapped[dict] = mapped_column(DATA)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("movie_jobs.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    result: Mapped[dict] = mapped_column(DATA)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("movie_jobs.id"), index=True)
    type: Mapped[str] = mapped_column(String(50))
    data: Mapped[dict] = mapped_column(DATA, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
