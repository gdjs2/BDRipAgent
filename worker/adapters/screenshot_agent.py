"""Recover short interruptions of the internal screenshot selection service."""

import json
import time

import httpx

from shared.config import behavior


def select_screenshots(ctx, timeout):
    # Capacity contention is not a failed connection. A subtitle cleanup can
    # legitimately own the shared agent for many minutes between requests.
    failures, request_number = 0, 0
    busy_started = None
    busy_limit = max(1, float(behavior()["agent"].get("busy_timeout_seconds", 1800)))
    ctx.progress(None, phase="Selecting screenshots", agent_request_attempt=1)
    while True:
        ctx.check()
        request_number += 1
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=10)) as client:
                with client.stream(
                    "POST",
                    ctx.settings.agent_url + "/select",
                    headers={
                        "Authorization": f"Bearer {ctx.settings.agent_token}",
                        "Accept": "application/x-ndjson",
                    },
                    json={"job_id": ctx.job.id, "task_id": ctx.task_id},
                ) as response:
                    if not response.is_error and response.headers.get("content-type", "").startswith(
                        "application/x-ndjson"
                    ):
                        ctx.progress(
                            None,
                            phase="Selecting screenshots",
                            agent_busy=False,
                            agent_request_attempt=request_number,
                        )
                        return consume_stream(ctx, response)
                    response.read()
        except (httpx.NetworkError, httpx.RemoteProtocolError, httpx.ConnectTimeout) as error:
            failures += 1
            reason = f"Agent connection interrupted ({type(error).__name__})"
            if failures >= 4:
                raise RuntimeError(
                    f"{reason} after 4 attempts. Check the agent container and retry the latest "
                    "screenshot selection attempt; prepared B-frame candidates are retained."
                ) from None
            delay = 5 * 2 ** (failures - 1)
            ctx.log(f"{reason}; reconnecting in {delay} seconds without repeating frame preparation.")
            ctx.progress(None, phase="Reconnecting to screenshot agent", agent_request_attempt=request_number)
        else:
            ctx.check()
            if response.status_code == 409:
                if busy_started is None:
                    busy_started = time.monotonic()
                waited = time.monotonic() - busy_started
                if waited >= busy_limit:
                    raise RuntimeError(
                        f"Screenshot agent remained busy for {busy_limit:g} seconds. "
                        "Retry when the other review finishes; prepared B-frame candidates are retained."
                    )
                delay = min(5, busy_limit - waited)
                ctx.log(
                    "Screenshot agent is processing another review; waiting for an available slot. Prepared frames are retained."
                )
                ctx.progress(
                    None,
                    phase="Waiting for screenshot agent — another review is running",
                    agent_busy=True,
                    agent_wait_seconds=round(waited),
                    agent_wait_limit_seconds=busy_limit,
                    agent_request_attempt=request_number,
                )
            elif response.is_error:
                raise RuntimeError(
                    f"Screenshot agent returned {response.status_code}: {response.text[:3000]}"
                )
            else:
                ctx.progress(
                    None,
                    phase="Selecting screenshots",
                    agent_busy=False,
                    agent_request_attempt=request_number,
                )
                ctx.log("Screenshot agent returned a selection; validating the result.")
                return response.json()
        # Short, cancellable waits keep the lease alive and never redo source frames.
        remaining = delay
        while remaining > 0:
            ctx.check()
            step = min(1, remaining)
            time.sleep(step)
            remaining -= step


def consume_stream(ctx, response, label="Screenshot agent", *, phase=None):
    pending = None
    last_flush = time.monotonic()

    def flush():
        nonlocal pending, last_flush
        if pending is not None:
            ctx.agent_event(pending)
            pending = None
        last_flush = time.monotonic()

    try:
        for line in response.iter_lines():
            ctx.check()
            if not line:
                continue
            message = json.loads(line)
            kind = message.get("type")
            if kind == "heartbeat":
                flush()
                continue
            if kind == "result":
                flush()
                ctx.log(f"{label} returned a response; validating the result.")
                return message["result"]
            if kind == "delta":
                if pending and (pending.get("invocation_id"), pending.get("item_id")) == (
                    message.get("invocation_id"),
                    message.get("item_id"),
                ):
                    pending["text"] += message["text"]
                else:
                    flush()
                    pending = message
                if time.monotonic() - last_flush >= 0.25 or len(pending["text"]) >= 8192:
                    flush()
            else:
                flush()
                if kind == "status" and message.get("agent_queue_state") in ("waiting", "running"):
                    waiting = message["agent_queue_state"] == "waiting"
                    ctx.progress(
                        None,
                        phase=(f"{phase} · {message['text']}" if phase else message["text"])
                        if waiting
                        else phase or f"{label} processing",
                        agent_processing_phase=phase or f"{label} processing",
                        agent_busy=waiting,
                        agent_queue_position=message.get("queue_position", 0),
                    )
                if kind in ("prompt", "message", "status", "complete", "error"):
                    ctx.agent_event(message)
                if kind == "error" and "invocation_id" not in message:
                    raise RuntimeError(label + " failed: " + message.get("text", "Unknown error"))
        raise httpx.RemoteProtocolError("Agent stream ended before returning a selection")
    finally:
        flush()
