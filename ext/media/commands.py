"""External command runner with a non-silent failure mode.

Upstream ``utils.helpers.run_cmd`` swallows timeouts and non-zero exits by
returning an empty string. Media preparation must never mistake a failed ffmpeg
run for an empty probe result, so this variant raises on request.
"""

import asyncio


async def run_cmd(*cmd: str, timeout: float = 30, check: bool = False) -> str:
    """Run an external command and return its stdout; ``check=True`` raises instead of returning ''."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        if check:
            raise TimeoutError("external_command_timeout") from None
        return ""
    if check and proc.returncode:
        raise RuntimeError("external_command_failed")
    return stdout.decode().strip()
