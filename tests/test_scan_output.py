import json
import sys

from sqlalchemy import select

from shared.db import session
from shared.models import Task
from worker.adapters import media
from worker.adapters.handbrake import parse_scan
from worker.runtime import TaskContext


def test_analysis_keeps_scan_json_separate_from_diagnostics(new_job, environment, tmp_path, monkeypatch):
    # Force a stderr write between stdout chunks. This corrupts the JSON when
    # both streams share a log, regardless of process scheduling or movie size.
    scanner = tmp_path / "scan.py"
    scanner.write_text(
        "import os\n"
        'os.write(1, b\'JSON Title Set: {"TitleList": [{"Index": 1, "Crop": [104,104,0,0], \''
        ' b\'"SubtitleList": [{"Clos\')\n'
        "os.write(2, b'HandBrake has exited.\\n')\n"
        "os.write(1, b'edCaption\": false}]}]}\\n')\n"
    )
    run = TaskContext.run

    def tools(ctx, command, **kwargs):
        if command[0] == environment.handbrake_bin:
            return run(ctx, [sys.executable, scanner], **kwargs)
        if command[0] == environment.mkvmerge_bin:
            result = {
                "tracks": [
                    {"id": 0, "type": "video"},
                    {
                        "id": 4,
                        "type": "subtitles",
                        "codec": "HDMV PGS",
                        "properties": {
                            "codec_id": "S_HDMV/PGS",
                            "language": "eng",
                            "track_name": "English SDH",
                            "flag_hearing_impaired": True,
                        },
                    },
                ]
            }
        elif command[0] == environment.mediainfo_bin:
            result = {}
        else:
            assert command[0] == environment.ffprobe_bin
            result = {
                "streams": [{"codec_type": "video", "width": 1920, "height": 1080, "pix_fmt": "yuv420p"}],
                "format": {"duration": "5673.71"},
            }
        text = json.dumps(result)
        kwargs["output"].write_text(text)
        return text

    monkeypatch.setattr(TaskContext, "run", tools)
    with session() as db:
        task = db.scalar(select(Task).where(Task.job_id == new_job["id"]))
        task.status, task.run_token = "RUNNING", "scan-test"
        task_id = task.id
        db.commit()
    ctx = TaskContext(task_id, "scan-test")
    try:
        analysis = media.analyze(ctx)
        assert analysis["crop"] == {"top": 104, "bottom": 104, "left": 0, "right": 0}
        subtitle = analysis["tracks"][0]
        assert subtitle["source_order"] == 1
        assert subtitle["hearing_impaired"] is None
        assert subtitle["mux_name"] == "English PGS"
        assert subtitle["source_properties"]["flag_hearing_impaired"] is True
        scan = ctx.output("metadata", "handbrake-scan.txt").read_text()
        assert "HandBrake has exited." not in scan
        assert parse_scan(scan).argument() == "104:104:0:0"
        assert "HandBrake has exited." in ctx.log_path.read_text()
    finally:
        ctx.close()
