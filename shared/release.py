"""Release inputs are exclusively the rendered final screenshot selection."""

from sqlalchemy import select

from shared.models import Screenshot
from shared.paths import contained, job_dir
from shared.screenshot_rules import is_b_frame_pair


def selected_pairs(db, job, settings):
    workspace = job_dir(settings.workspace_root, job.id)
    contained(settings.completed_root, job.analysis.get("final_path", ""), exists=True)
    shots = list(
        db.scalars(select(Screenshot).where(Screenshot.job_id == job.id, Screenshot.selected.is_(True)))
    )
    expected = job.analysis.get("screenshot_selection", {}).get("count", job.screenshot_policy["count"])
    if not 1 <= len(shots) <= 15 or len(shots) != expected:
        raise ValueError("Confirm 1–15 final screenshot pairs before generating the release")
    pairs = []
    for shot in sorted(shots, key=lambda s: s.info["source_frame_number"]):
        if not is_b_frame_pair(shot.info):
            raise ValueError("Release screenshots must be verified B-frames in both videos")
        paths = shot.info.get("comparisons", {})
        if set(paths) != {"src", "encode"}:
            raise ValueError("Render every chosen comparison pair before generating the release")
        pairs.append(
            {
                "candidate_id": shot.candidate_id,
                "frame_number": shot.info["source_frame_number"],
                **{kind: str(contained(workspace, path, exists=True)) for kind, path in paths.items()},
            }
        )
    return pairs
