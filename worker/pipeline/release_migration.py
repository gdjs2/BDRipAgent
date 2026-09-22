"""Relocate legacy release exports without changing payload bytes or artifact IDs.

Run once BEFORE removing the legacy /torrents mount:
  python -m worker.pipeline.release_migration --legacy-torrents /torrents
"""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from backend.app.services import manifest
from shared.config import get_settings
from shared.db import session
from shared.models import Artifact, MovieJob, Task
from shared.paths import artifact_root, contained, job_dir, write_json
from worker.pipeline.release_exports import KINDS, publish, release_paths


def legacy_path(settings, job_id, item, torrents):
    root = torrents if item["storage"] == "torrents" else artifact_root(settings, job_id, item["storage"])
    return contained(root, item["path"])


def migrate(db, job, settings, torrents):
    result = job.analysis.get("release_result")
    if not result or result.get("package_storage") != "artifacts":
        return None
    owner = contained(settings.artifacts_root, f".owners/{job.id}.json")
    if result.get("bundle_path") and (
        not owner.exists() or not json.loads(owner.read_text()).get("legacy_paths")
    ):
        return result["bundle_path"]
    if db.scalar(select(Task.id).where(Task.job_id == job.id, Task.status.in_(["QUEUED", "RUNNING"]))):
        raise ValueError(f"Job {job.id} has active work; finish or pause it before relocating its release")
    staging, staged = None, []
    if not result.get("bundle_path"):
        items = result["artifacts"]
        if {item["kind"] for item in items} != KINDS or len(items) != len(KINDS):
            raise ValueError("Legacy release has an incomplete artifact set")
        rows, originals, staged = [], [], []
        for item in items:
            row = db.scalar(
                select(Artifact).where(
                    Artifact.job_id == job.id,
                    Artifact.path == item["path"],
                    Artifact.storage == item["storage"],
                )
            )
            if not row or row.artifact_type != item["kind"]:
                raise ValueError("Legacy release artifact record is missing or inconsistent")
            source = legacy_path(settings, job.id, item, torrents)
            stat = source.stat()
            originals.append({**item, "device": stat.st_dev, "inode": stat.st_ino})
            rows.append(row)
        # Existing artifact files can be linked within the same mount. Only the
        # small legacy torrent needs staging across the removed storage root.
        staging = contained(job_dir(settings.workspace_root, job.id), "release-layout-migration")
        staging.mkdir(parents=True, exist_ok=True)
        for item in originals:
            source = legacy_path(settings, job.id, item, torrents)
            if item["storage"] != "torrents":
                staged.append({"path": str(source), "kind": item["kind"], "storage": item["storage"]})
                continue
            target = contained(staging, source.name)
            if not target.exists():
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copyfile(source, target)
            elif not os.path.samefile(source, target):
                with source.open("rb") as a, target.open("rb") as b:
                    if hashlib.file_digest(a, "sha256").digest() != hashlib.file_digest(b, "sha256").digest():
                        raise ValueError("Migration staging file already exists with different content")
            staged.append({"path": str(target), "kind": item["kind"], "storage": "workspace"})
        exported = publish(
            SimpleNamespace(settings=settings, job=job, check=lambda: None, progress=lambda *a, **kw: None),
            staged,
        )
        record = json.loads(owner.read_text())
        # Persist cleanup provenance before committing new database paths. A rerun
        # after interruption can finish cleanup without removing unrelated files.
        write_json(owner, {**record, "legacy_paths": originals})
        updated = []
        for row, item in zip(rows, exported, strict=True):
            row.path = str(Path(item["path"]).relative_to(settings.artifacts_root))
            row.storage = "artifacts"
            updated.append({"path": row.path, "kind": row.artifact_type, "storage": row.storage})
        result = {**result, **release_paths(exported, settings.artifacts_root), "artifacts": updated}
        job.analysis = {**job.analysis, "release_result": result}
        db.commit()
        manifest(db, job)
    if owner.exists():
        record = json.loads(owner.read_text())
        for item in record.get("legacy_paths", []):
            source = legacy_path(settings, job.id, item, torrents)
            if not source.exists():
                continue
            stat = source.stat()
            if (stat.st_dev, stat.st_ino) != (item["device"], item["inode"]):
                raise ValueError("A legacy file changed after migration; leaving it untouched")
            if db.scalar(
                select(Artifact.id).where(Artifact.storage == item["storage"], Artifact.path == item["path"])
            ):
                raise ValueError("A legacy file is still referenced; leaving it untouched")
            source.unlink()
            if source.parent not in (settings.artifacts_root, torrents):
                try:
                    source.parent.rmdir()
                except OSError:
                    pass
        write_json(owner, {k: v for k, v in record.items() if k != "legacy_paths"})
    if staging is not None:
        for item in staged:
            path = Path(item["path"])
            if path.parent == staging:
                path.unlink(missing_ok=True)
        try:
            staging.rmdir()
        except OSError:
            pass
    try:
        torrents.rmdir()  # Only succeeds if empty; never recursively remove user files.
    except OSError:
        pass
    return result["bundle_path"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-torrents", type=Path, default=Path("/torrents"))
    args = parser.parse_args()
    settings = get_settings()
    with session() as db:
        ids = list(db.scalars(select(MovieJob.id)))
    for job_id in ids:
        with session() as db:
            job = db.scalar(select(MovieJob).where(MovieJob.id == job_id).with_for_update())
            bundle = migrate(db, job, settings, args.legacy_torrents)
            if bundle:
                print(f"{job_id}: artifacts/{bundle}")


if __name__ == "__main__":
    main()
