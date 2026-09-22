from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from shared.config import get_settings
from shared.db import engine


def test_split_queue_migration_preserves_pause_and_enforces_both_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/queue-upgrade.sqlite")
    get_settings.cache_clear()
    engine.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        command.upgrade(config, "0005")
        with engine().begin() as connection:
            connection.execute(sa.text("UPDATE queue_settings SET max_concurrent_jobs=7, paused=true"))
        command.upgrade(config, "head")
        with engine().connect() as connection:
            settings = connection.execute(sa.text("SELECT * FROM queue_settings")).mappings().one()
            assert (
                settings["paused"]
                and settings["max_encoding_tasks"] == 1
                and settings["max_other_tasks"] == 3
            )
            assert "max_concurrent_jobs" not in settings
        for field in ("max_encoding_tasks", "max_crf_tasks", "max_other_tasks"):
            with pytest.raises(IntegrityError), engine().begin() as connection:
                connection.execute(sa.text(f"UPDATE queue_settings SET {field}=0"))
        with engine().begin() as connection:
            connection.execute(sa.text("UPDATE queue_settings SET max_encoding_tasks=2, max_other_tasks=5"))
        command.check(config)
        command.downgrade(config, "0005")
        with engine().connect() as connection:
            settings = connection.execute(sa.text("SELECT * FROM queue_settings")).mappings().one()
            assert settings["paused"] and settings["max_concurrent_jobs"] == 2
            assert "max_encoding_tasks" not in settings and "max_other_tasks" not in settings
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine().dispose()
        engine.cache_clear()
        get_settings.cache_clear()


def test_crf_queue_upgrade_preserves_existing_limits_and_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/crf-upgrade.sqlite")
    get_settings.cache_clear()
    engine.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        command.upgrade(config, "0007")
        with engine().begin() as connection:
            connection.execute(
                sa.text("UPDATE queue_settings SET max_encoding_tasks=2, max_other_tasks=5, paused=true")
            )
        command.upgrade(config, "head")
        with engine().connect() as connection:
            value = connection.execute(sa.text("SELECT * FROM queue_settings")).mappings().one()
            assert value["max_encoding_tasks"] == 2 and value["max_other_tasks"] == 5 and value["paused"]
            assert value["max_crf_tasks"] == 1
        for invalid in (0, 65):
            with pytest.raises(IntegrityError), engine().begin() as connection:
                connection.execute(
                    sa.text("UPDATE queue_settings SET max_crf_tasks=:limit"), {"limit": invalid}
                )
        command.check(config)
        command.downgrade(config, "0007")
        with engine().connect() as connection:
            value = connection.execute(sa.text("SELECT * FROM queue_settings")).mappings().one()
            assert "max_crf_tasks" not in value
            assert value["max_encoding_tasks"] == 2 and value["max_other_tasks"] == 5 and value["paused"]
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine().dispose()
        engine.cache_clear()
        get_settings.cache_clear()
