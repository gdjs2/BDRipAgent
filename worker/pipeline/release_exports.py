"""Publish a WiKi payload and its supporting files in one timestamped ART folder."""

import fcntl
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from shared.paths import artifact_root, contained, write_json

KINDS = {
    "RELEASE_MEDIA",
    "RELEASE_NFO",
    "RELEASE_MD5",
    "RELEASE_TORRENT",
    "RELEASE_BBCODE",
    "RELEASE_ENCODER_INFO",
}


def validate_name(name):
    if Path(name).name != name or not name.endswith("-WiKi"):
        raise ValueError("Release export name must be a WiKi release basename")
    # Leave room for the UTC timestamp and ' [ART] ', including collision precision.
    if len(name.encode()) > 226:
        raise ValueError(
            "Release name is too long for an ART folder; use a shorter movie title (226 bytes max)"
        )


def release_paths(items, root):
    paths = {item["kind"]: Path(item["path"]).relative_to(root) for item in items}
    return {
        "bundle_path": str(paths["RELEASE_TORRENT"].parent),
        "package_path": str(paths["RELEASE_MEDIA"].parent),
        "package_storage": "artifacts",
        "torrent_path": str(paths["RELEASE_TORRENT"]),
        "torrent_storage": "artifacts",
    }


def publish(ctx, items):
    name = ctx.job.release_name
    validate_name(name)
    if {item["kind"] for item in items} != KINDS or len(items) != len(KINDS):
        raise ValueError("Release output set is incomplete")
    sources = []
    for item in items:
        source = Path(item["path"])
        root = artifact_root(ctx.settings, ctx.job.id, item["storage"])
        contained(root, source.relative_to(root), exists=True)
        sources.append((source, item["kind"]))

    total_bytes = sum(source.stat().st_size for source, _ in sources)
    completed_bytes = 0
    last_report = 0.0

    def report(force=False):
        nonlocal last_report
        now = time.monotonic()
        if force or now - last_report >= 0.5:
            ctx.progress(
                100 * completed_bytes / max(1, total_bytes),
                phase="Publishing release ART directory",
                completed_bytes=completed_bytes,
                total_bytes=total_bytes,
            )
            last_report = now

    report(True)
    owner = contained(ctx.settings.artifacts_root, f".owners/{ctx.job.id}.json")
    owner.parent.mkdir(parents=True, exist_ok=True)
    # A new generation owns a fresh bundle. An interrupted attempt may resume its
    # own paths, but must never overwrite the previous successful release.
    with owner.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ctx.check()
        record = json.loads(owner.read_text()) if owner.exists() else {}
        if record and record.get("job_id") != ctx.job.id:
            raise ValueError("ART directory belongs to another job")
        generation = getattr(ctx, "task_id", "legacy")
        if record.get("generation") == generation and record.get("release_name") == name:
            bundle = contained(ctx.settings.artifacts_root, record["bundle"])
        else:
            timestamp = datetime.now(UTC)
            bundle_name = timestamp.strftime("%Y%m%d-%H%M%S") + f" [ART] {name}"
            bundle = contained(ctx.settings.artifacts_root, bundle_name)
            # Never claim an existing user directory. mkdir also arbitrates two
            # jobs publishing the same basename in the same second.
            for collision in range(1000):
                try:
                    bundle.mkdir()
                    break
                except FileExistsError:
                    precise = timestamp + timedelta(microseconds=collision)
                    bundle = contained(
                        ctx.settings.artifacts_root, precise.strftime("%Y%m%d-%H%M%S.%f") + f" [ART] {name}"
                    )
            else:
                raise ValueError("Could not allocate a unique ART directory")
            write_json(
                owner,
                {
                    "job_id": ctx.job.id,
                    "release_name": name,
                    "bundle": bundle.name,
                    "generation": generation,
                    "bundles": sorted(
                        set(
                            [
                                *record.get("bundles", []),
                                *([record["bundle"]] if record.get("bundle") else []),
                                bundle.name,
                            ]
                        )
                    ),
                },
            )
        package = contained(bundle, name)
        destinations = []
        for source, kind in sources:
            if kind in ("RELEASE_MEDIA", "RELEASE_NFO", "RELEASE_MD5"):
                # Preserve the upstream three-file torrent payload exactly.
                target = contained(package, source.name)
            else:
                suffix = {
                    "RELEASE_TORRENT": ".torrent",
                    "RELEASE_BBCODE": ".bbcode.txt",
                    "RELEASE_ENCODER_INFO": ".encoder.txt",
                }[kind]
                target = contained(bundle, name + suffix)
            destinations.append((source, target, kind))
        # Publish the torrent after the payload its pieces refer to is ready.
        for source, target, _ in sorted(destinations, key=lambda item: item[2] == "RELEASE_TORRENT"):
            ctx.check()
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".release-{uuid4()}.tmp")
            try:
                try:
                    os.link(source, temporary)
                    completed_bytes += source.stat().st_size
                except OSError:
                    with source.open("rb") as incoming, temporary.open("wb") as outgoing:
                        while block := incoming.read(8 * 1024 * 1024):
                            ctx.check()
                            outgoing.write(block)
                            completed_bytes += len(block)
                            report()
                ctx.check()
                temporary.replace(target)
                report(True)
            finally:
                temporary.unlink(missing_ok=True)
    return [{"path": str(target), "kind": kind, "storage": "artifacts"} for _, target, kind in destinations]
