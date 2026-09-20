"""Recover short interruptions of the internal screenshot selection service."""

import json
import time

import httpx


def select_screenshots(ctx, timeout):
    attempts = 4
    for attempt in range(1, attempts + 1):
        ctx.check()
        ctx.progress(90, phase="Selecting screenshots", agent_request_attempt=attempt)
        ctx.log(f"Requesting screenshot selection from the agent (attempt {attempt}/{attempts}).")
        try:
            # Reconnect and resolve the service address after a container replacement.
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
                        return consume_stream(ctx, response)
                    response.read()
        except (httpx.NetworkError, httpx.RemoteProtocolError, httpx.ConnectTimeout) as error:
            reason = f"Agent connection interrupted ({type(error).__name__})"
        else:
            ctx.check()
            if response.status_code == 409:
                # The service serializes selections; a lost connection may leave
                # the previous request running. Never bypass that lock.
                reason = "Screenshot agent is still busy"
            elif response.is_error:
                raise RuntimeError(
                    f"Screenshot agent returned {response.status_code}: {response.text[:3000]}"
                )
            else:
                ctx.log("Screenshot agent returned a selection; validating the result.")
                return response.json()
        if attempt == attempts:
            raise RuntimeError(
                f"{reason} after {attempts} attempts. Check the agent container and retry the latest "
                "screenshot selection attempt; prepared B-frame candidates are retained."
            ) from None
        delay = 5 * 2 ** (attempt - 1)
        ctx.log(f"{reason}; reconnecting in {delay} seconds without repeating frame preparation.")
        ctx.progress(90, phase="Reconnecting to screenshot agent", agent_request_attempt=attempt)
        for _ in range(delay):
            ctx.check()
            time.sleep(1)


def consume_stream(ctx, response, label="Screenshot agent"):
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
                if kind in ("prompt", "message", "status", "complete", "error"):
                    ctx.agent_event(message)
                if kind == "error" and "invocation_id" not in message:
                    raise RuntimeError(label + " failed: " + message.get("text", "Unknown error"))
        raise httpx.RemoteProtocolError("Agent stream ended before returning a selection")
    finally:
        flush()
