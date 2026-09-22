"""Isolated native-media API fixture for browser-media-preview.cjs (no production data)."""

import tempfile
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from shared.db import session
from shared.models import MovieJob, Task
from tests.conftest import environment, new_job
from tests.test_artifact_media import (
    media_fixture,
    test_legacy_encoder_summary_and_cached_source_video_bitrate,
)


def main():
    with (
        tempfile.TemporaryDirectory(prefix="bdrip-browser-media-") as folder,
        pytest.MonkeyPatch.context() as patch,
    ):
        setup = environment.__wrapped__(Path(folder), patch)
        settings = next(setup)
        try:
            from backend.app.main import app

            with TestClient(app) as client:
                client.headers["Authorization"] = f"Bearer {settings.api_token}"
                job = new_job.__wrapped__(client, settings)
                media_fixture.__wrapped__(client, job, settings)
                test_legacy_encoder_summary_and_cached_source_video_bitrate(client, job, settings)
                with session() as db:
                    row = db.get(MovieJob, job["id"])
                    row.state = "COMPLETE"
                    row.analysis = {
                        **row.analysis,
                        "video": {
                            **row.analysis["video"],
                            "codec": "h264",
                            "bit_depth": 8,
                            "width": 320,
                            "height": 180,
                            "fps": 24,
                            "duration": 18,
                        },
                    }
                    task = db.get(Task, job["tasks"][0]["id"])
                    task.stage = "ENCODING"
                    task.progress = 100
                    db.commit()
            uvicorn.run(app, host="0.0.0.0", port=8000)
        finally:
            setup.close()


if __name__ == "__main__":
    main()
