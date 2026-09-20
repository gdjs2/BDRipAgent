"""Recover short interruptions of the internal screenshot selection service."""

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
                response = client.post(
                    ctx.settings.agent_url + "/select",
                    headers={"Authorization": f"Bearer {ctx.settings.agent_token}"},
                    json={"job_id": ctx.job.id, "task_id": ctx.task_id},
                )
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
