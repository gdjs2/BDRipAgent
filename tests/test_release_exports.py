import json
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shared.paths import job_dir
from worker.pipeline.release_exports import publish, release_paths


def context(settings, name="Movie.2026.1080p.BluRay.x264-WiKi"):
    return SimpleNamespace(
        settings=settings,
        job=SimpleNamespace(id=str(uuid4()), release_name=name),
        check=lambda: None,
        progress=lambda *a, **kw: None,
    )


def outputs(ctx):
    root = job_dir(ctx.settings.workspace_root, ctx.job.id)
    root.mkdir(parents=True)
    items = []
    for suffix, kind in [
        (".mkv", "MEDIA"),
        (".nfo", "NFO"),
        (".md5", "MD5"),
        (".bbcode.txt", "BBCODE"),
        (".torrent", "TORRENT"),
        (".encoder-info.txt", "ENCODER_INFO"),
    ]:
        path = root / ("encoder-info.txt" if kind == "ENCODER_INFO" else ctx.job.release_name + suffix)
        path.write_text(kind)
        items.append({"path": str(path), "kind": "RELEASE_" + kind, "storage": "workspace"})
    return items


def test_release_has_one_timestamped_bundle_with_unchanged_torrent_payload(environment):
    ctx = context(environment)
    items = outputs(ctx)
    result = publish(ctx, items)
    metadata = release_paths(result, environment.artifacts_root)
    bundle = environment.artifacts_root / metadata["bundle_path"]
    name = ctx.job.release_name
    assert re.fullmatch(r"\d{8}-\d{6} \[ART\] " + re.escape(name), bundle.name)
    assert {p.suffix for p in (bundle / name).iterdir()} == {".mkv", ".nfo", ".md5"}
    assert (bundle / f"{name}.torrent").read_text() == "TORRENT"
    assert (bundle / f"{name}.bbcode.txt").read_text() == "BBCODE"
    assert (bundle / f"{name}.encoder.txt").read_text() == "ENCODER_INFO"
    assert len(list(bundle.iterdir())) == 4
    assert all(item["storage"] == "artifacts" for item in result)
    assert publish(ctx, items) == result  # Stable timestamp and links on regeneration/retry.
    owner = environment.artifacts_root / ".owners" / f"{ctx.job.id}.json"
    assert json.loads(owner.read_text())["bundle"] == bundle.name
    assert not (environment.artifacts_root.parent / "torrents").exists()


def test_identical_release_names_and_same_second_do_not_overwrite_user_files(environment, monkeypatch):
    from datetime import UTC, datetime

    from worker.pipeline import release_exports

    instant = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(release_exports, "datetime", SimpleNamespace(now=lambda tz: instant))
    first, second = context(environment), context(environment)
    existing = environment.artifacts_root / f"20260920-120000 [ART] {first.job.release_name}"
    existing.mkdir()
    (existing / "user.txt").write_text("untouched")
    a, b = publish(first, outputs(first)), publish(second, outputs(second))
    assert (
        release_paths(a, environment.artifacts_root)["bundle_path"]
        != release_paths(b, environment.artifacts_root)["bundle_path"]
    )
    assert (existing / "user.txt").read_text() == "untouched"
    assert all(Path(item["path"]).is_file() for item in a + b)


def test_incomplete_release_is_not_exported(environment):
    ctx = context(environment)
    with pytest.raises(ValueError, match="incomplete"):
        publish(ctx, outputs(ctx)[:-1])
    assert not list(environment.artifacts_root.iterdir())


def test_export_copy_fallback_and_long_release_names(environment, monkeypatch):
    from worker.pipeline import release_exports

    ctx = context(environment, "M" * 221 + "-WiKi")
    items = outputs(ctx)

    def cross_device(*args):
        raise OSError("Cross-device link")

    monkeypatch.setattr(release_exports.os, "link", cross_device)
    exported = publish(ctx, items)
    by_kind = {item["kind"]: item for item in items}
    for item in exported:
        assert Path(item["path"]).read_bytes() == Path(by_kind[item["kind"]]["path"]).read_bytes()
        assert all(len(part.encode()) <= 255 for part in Path(item["path"]).parts)
    assert not list(environment.artifacts_root.rglob("*.tmp"))
    ctx.job.release_name = "M" * 222 + "-WiKi"
    with pytest.raises(ValueError, match="shorter movie title"):
        publish(ctx, items)
