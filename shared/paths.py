import json
import os
from pathlib import Path
from uuid import UUID, uuid4


def contained(root: Path, relative: str | Path, *, exists: bool = False) -> Path:
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts or "\x00" in str(part):
        raise ValueError("Expected a relative path without traversal")
    target = (root.resolve() / part).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise ValueError("Path escapes its storage root")
    if exists and not target.is_file():
        raise ValueError("File does not exist")
    return target


def job_dir(root: Path, job_id: str) -> Path:
    return contained(root, str(UUID(job_id)))


def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        with temporary.open("w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value):
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def artifact_root(settings, job_id, storage):
    roots = {
        "workspace": job_dir(settings.workspace_root, job_id),
        "completed": settings.completed_root,
        "artifacts": settings.artifacts_root,
    }
    if storage not in roots:
        raise ValueError("Unknown artifact storage")
    return roots[storage]
