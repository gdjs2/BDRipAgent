"""Read Codex app-server message deltas without exposing internal reasoning events."""

import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time

from shared.config import behavior, get_settings


def invoke(prompt, images, schema, emit, check):
    settings = get_settings()
    with (
        tempfile.TemporaryDirectory(prefix="screenshot-agent-") as directory,
        tempfile.TemporaryFile(mode="w+") as stderr,
    ):
        command = [settings.codex_bin, "app-server", "--stdio"]
        for override in (
            'approval_policy="never"',
            'sandbox_mode="read-only"',
            'web_search="disabled"',
            "features.shell_tool=false",
            "features.unified_exec=false",
            "features.multi_agent=false",
            "project_doc_max_bytes=0",
            "mcp_servers={}",
        ):
            command += ["-c", override]
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            in (
                "PATH",
                "HOME",
                "CODEX_HOME",
                "LANG",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
            )
        }
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
            env=env,
            cwd=directory,
            start_new_session=True,
        )
        messages = queue.Queue(maxsize=256)
        stopped = threading.Event()

        def read_stdout():
            for line in process.stdout:
                while not stopped.is_set():
                    try:
                        messages.put(line, timeout=0.2)
                        break
                    except queue.Full:
                        continue
                if stopped.is_set():
                    break

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        deadline = time.monotonic() + behavior()["agent"]["timeout_seconds"]
        finals = []
        completed = None

        def send(value):
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()

        def receive():
            while True:
                check()
                if time.monotonic() >= deadline:
                    raise TimeoutError("Codex screenshot selection timed out")
                try:
                    line = messages.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive() and messages.empty():
                        stderr.seek(0)
                        raise RuntimeError(
                            f"Codex selection exited before completing: {stderr.read()[-3000:]}"
                        )
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise RuntimeError("Invalid Codex streaming event")
                return value

        def notification(value):
            nonlocal completed
            method, params = value.get("method"), value.get("params", {})
            if method and "id" in value:
                send(
                    {
                        "id": value["id"],
                        "error": {"code": -32601, "message": "Interactive tools are disabled"},
                    }
                )
            elif method == "item/agentMessage/delta":
                item_id, delta = params["itemId"], params["delta"]
                emit({"type": "delta", "item_id": item_id, "text": delta})
            elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                item = params["item"]
                finals.append(item)
                emit({"type": "message", "item_id": item["id"], "text": item["text"]})
            elif method == "turn/completed":
                completed = params["turn"]
            elif method == "error" and not params.get("willRetry", False):
                raise RuntimeError(params.get("error", {}).get("message", "Codex selection failed"))

        def request(request_id, method, params):
            send({"id": request_id, "method": method, "params": params})
            while True:
                value = receive()
                if value.get("id") == request_id and not value.get("method"):
                    if "error" in value:
                        raise RuntimeError(value["error"]["message"])
                    return value["result"]
                notification(value)

        try:
            request(0, "initialize", {"clientInfo": {"name": "bdripagent", "version": "0.1.0"}})
            send({"method": "initialized", "params": {}})
            thread = request(
                1,
                "thread/start",
                {
                    "cwd": directory,
                    "ephemeral": True,
                    "sandbox": "read-only",
                    "approvalPolicy": "never",
                    **({"model": behavior()["agent"]["model"]} if behavior()["agent"].get("model") else {}),
                },
            )["thread"]["id"]
            emit({"type": "status", "text": "Selecting screenshots", "thread_id": thread})
            request(
                2,
                "turn/start",
                {
                    "threadId": thread,
                    "input": [
                        {"type": "text", "text": prompt},
                        *({"type": "localImage", "path": str(p)} for p in images),
                    ],
                    "outputSchema": schema,
                },
            )
            while completed is None:
                notification(receive())
            if completed["status"] != "completed":
                error = completed.get("error") or {}
                raise RuntimeError(error.get("message", "Codex selection did not complete"))
            final = next((item for item in reversed(finals) if item.get("phase") == "final_answer"), None)
            text = final["text"] if final else finals[-1]["text"] if finals else ""
            if not text:
                raise RuntimeError("Codex selection returned no response")
            return text, thread
        finally:
            stopped.set()
            # Terminate the whole process group even if its leader has exited.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            reader.join(timeout=2)
            process.stdin.close()
            process.stdout.close()
