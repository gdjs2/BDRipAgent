"""Prepare selected outputs and run the pinned BDRip_Scripts release library."""

import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from backend.app.schemas import ReleaseSource
from shared.config import profiles
from shared.db import session
from shared.encoding import is_smoke_test
from shared.models import Task
from shared.paths import contained, job_dir, write_json
from shared.release import selected_pairs
from worker.adapters.release_progress import ReleaseProgressReader
from worker.pipeline.release_exports import publish, release_paths, validate_name


def generate(ctx):
    details = ctx.job.analysis["release_details"]
    # Validate again for saved jobs created before the source field was required.
    # A retry must fail before file generation or uploads if this field is absent.
    source = ReleaseSource(source=details.get("source", "")).source
    details = {**details, "source": source}
    validate_name(ctx.job.release_name)
    ctx.source()
    with session() as db:
        pairs = selected_pairs(db, ctx.job, ctx.settings)
        encode_task = db.scalar(
            select(Task)
            .where(Task.job_id == ctx.job.id, Task.type == "encode", Task.status == "SUCCEEDED")
            .order_by(Task.created_at.desc())
        )
    movie = contained(ctx.settings.completed_root, ctx.job.analysis["final_path"], exists=True)
    signature = []
    for pair in pairs:
        for kind in ("src", "encode"):
            path = Path(pair[kind])
            stat = path.stat()
            signature.append([str(path), stat.st_size, stat.st_mtime_ns])
    fingerprint = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
    cache = contained(ctx.workspace, f"release-cache/{fingerprint}")
    package = contained(
        job_dir(ctx.settings.completed_root, ctx.job.id),
        f"releases/{ctx.task_id}/{ctx.job.release_name}",
    )
    output = ctx.output("release", "result.json")
    progress = output.with_name("progress.json")
    request = output.with_name("request.json")
    write_json(
        request,
        {
            "details": details,
            "title": ctx.job.title,
            "release_name": ctx.job.release_name,
            "year": ctx.job.year,
            "imdb_id": ctx.job.imdb_id,
            "smoke_test": is_smoke_test(ctx.job),
            "codec": profiles()[ctx.job.analysis_profile]["codec"],
            "movie_path": str(movie),
            "package_dir": str(package),
            "pairs": pairs,
            "cache_dir": str(cache),
            "output_dir": str(output.parent),
            "metadata_cache": str(contained(ctx.workspace, "release-cache/movie-metadata.json")),
            "metadata_timeout": ctx.settings.imdb_timeout_seconds,
            "remote_suffix": f"{ctx.job.id[:8]}-{fingerprint[:12]}",
            "encoder_log": str(contained(ctx.workspace, encode_task.log_path, exists=True))
            if encode_task
            else None,
        },
    )
    ctx.progress(0, phase="Preparing release")
    ctx.run(
        [ctx.settings.bdrip_python, Path(__file__).parents[1] / "adapters/release_runner.py", request],
        progress_reader=ReleaseProgressReader(progress),
        env={"TU_TTG_TOKEN": ctx.settings.tu_ttg_token.get_secret_value()},
    )
    result = json.loads(output.read_text())
    artifacts = []
    ctx.progress(99, phase="Preparing release ART directory")
    exported = publish(ctx, result.pop("artifacts"))
    for item in exported:
        # The bridge runs locally; paths still must belong to configured storage.
        relative = ctx.artifact(
            Path(item["path"]),
            item["kind"],
            storage=item["storage"],
            info={"smoke_test": is_smoke_test(ctx.job)},
        )
        artifacts.append({"path": relative, "kind": item["kind"], "storage": item["storage"]})
    result["artifacts"] = artifacts
    result.update(release_paths(exported, ctx.settings.artifacts_root))
    result["candidate_ids"] = [p["candidate_id"] for p in pairs]
    result["details"] = details
    result["task_id"] = ctx.task_id
    # The public result contains relative artifact paths, never upload credentials.
    write_json(output, result)
    ctx.artifact(output, "RELEASE_REPORT")

    def save(db, job):
        job.analysis = {**job.analysis, "release_result": result}

    return save
