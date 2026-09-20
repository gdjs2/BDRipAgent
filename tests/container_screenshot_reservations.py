"""Run only against a disposable PostgreSQL database, never the application DB."""

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from shared.config import ScreenshotPolicy, get_settings
from shared.db import Base, engine, session
from shared.models import MovieJob, Screenshot
from shared.screenshot_rules import check_other_variants


def main():
    if not get_settings().database_url.endswith("/screenshot_reservation_test"):
        raise ValueError("This test requires the disposable screenshot_reservation_test database")
    Base.metadata.create_all(engine())
    with session() as db:
        jobs = []
        for codec in ("x264", "x265"):
            job = MovieJob(
                source_path="concurrent.mkv",
                source_size=1000,
                source_mtime_ns="123",
                title="Concurrency test",
                year=2026,
                release_name=f"Test.{codec}-WiKi",
                analysis_profile=f"{codec}-live",
                screenshot_policy=ScreenshotPolicy().model_dump(),
            )
            db.add(job)
            db.flush()
            db.add(
                Screenshot(
                    job_id=job.id,
                    candidate_id=1,
                    info={
                        "source_frame_number": 1000,
                        "timeline_seconds": 1000 / 24,
                    },
                )
            )
            jobs.append(job.id)
        db.commit()
    barrier = threading.Barrier(2)

    def choose(job_id):
        with session() as db:
            job = db.scalar(select(MovieJob).where(MovieJob.id == job_id).with_for_update())
            shot = db.scalar(select(Screenshot).where(Screenshot.job_id == job_id))
            barrier.wait(timeout=10)
            try:
                check_other_variants(db, job, [shot.info])
            except ValueError:
                db.rollback()
                return "conflict"
            shot.selected = True
            db.commit()
            return "selected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(choose, jobs))
    assert sorted(results) == ["conflict", "selected"], results
    with session() as db:
        assert len(list(db.scalars(select(Screenshot).where(Screenshot.selected.is_(True))))) == 1
    print("PASS: simultaneous x264/x265 reservations allow exactly one selection of frame 1000.")


if __name__ == "__main__":
    main()
