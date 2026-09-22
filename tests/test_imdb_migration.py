from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from shared.config import get_settings
from shared.db import engine


def test_imdb_migration_preserves_existing_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/upgrade.sqlite")
    get_settings.cache_clear()
    engine.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        command.upgrade(config, "0001")
        with engine().begin() as connection:
            table = sa.Table("movie_jobs", sa.MetaData(), autoload_with=connection)
            connection.execute(
                table.insert().values(
                    id="existing-job",
                    source_path="Movie.mkv",
                    source_size=1,
                    source_mtime_ns="1",
                    title="Existing Movie",
                    year=2000,
                    release_name="Existing.Movie.2000.1080p.BluRay.x264-WiKi",
                    analysis_profile="x264-live",
                    state="COMPLETE",
                    manifest_path="manifest.yaml",
                    analysis={"final_path": "preserved.mkv"},
                    screenshot_policy={},
                    validation={"valid": True},
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            tasks = sa.Table("tasks", sa.MetaData(), autoload_with=connection)
            connection.execute(
                tasks.insert().values(
                    id="existing-task",
                    job_id="existing-job",
                    type="encode",
                    stage="ENCODING",
                    status="SUCCEEDED",
                    attempt=1,
                    progress=100,
                    progress_detail={},
                    command_json=[],
                    cancel_requested=False,
                    created_at=datetime.now(UTC),
                    log_path="logs/existing.log",
                )
            )
        command.upgrade(config, "head")
        with engine().connect() as connection:
            table = sa.Table("movie_jobs", sa.MetaData(), autoload_with=connection)
            row = connection.execute(sa.select(table)).mappings().one()
            assert row["title"] == "Existing Movie"
            assert row["analysis"]["final_path"] == "preserved.mkv"
            assert row["imdb_id"] is None and row["imdb_metadata"] is None
            tasks = sa.Table("tasks", sa.MetaData(), autoload_with=connection)
            task = connection.execute(sa.select(tasks)).mappings().one()
            assert task["status"] == "SUCCEEDED" and not task["held"] and task["queue_priority"] == 0
            assert task["lane"] == "pipeline"
            indexes = {i["name"]: i for i in sa.inspect(connection).get_indexes("tasks")}
            assert indexes["one_active_task_per_lane"]["column_names"] == ["job_id", "lane"]
            assert "one_active_task_per_job" not in indexes
            assert not task["can_pause"] and not task["pause_requested"] and task["paused_at"] is None
            queue = sa.Table("queue_settings", sa.MetaData(), autoload_with=connection)
            settings = connection.execute(sa.select(queue)).mappings().one()
            assert settings["max_encoding_tasks"] == 1 and settings["max_other_tasks"] == 3
            assert not settings["paused"]
        command.check(config)
    finally:
        engine().dispose()
        engine.cache_clear()
        get_settings.cache_clear()
