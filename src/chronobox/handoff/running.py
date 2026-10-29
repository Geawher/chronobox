"""Running a script on a machine detached in its own session, and polling it until it ends: nothing
waits on the machine meanwhile, so it can be snapshotted for the crash test."""

from __future__ import annotations

import asyncio
import shlex
import time
from collections.abc import Awaitable, Callable

from chronobox.handoff.scripts import STATE_DIR
from chronobox.vm import Machine, VMError

CHECK_TIMEOUT_S = 300.0
POLL_TIMEOUT_S = 30.0
TIMED_OUT = 124


# A detached script is polled often at first, then every (seconds so far / POLL_SLOWDOWN),
# within these bounds.
POLL_MIN_S = 0.5


POLL_MAX_S = 5.0
POLL_SLOWDOWN = 20.0
Tick = Callable[[float], Awaitable[None]]


async def run_detached(
    machine: Machine, folder: str, name: str, timeout: float, tick: Tick | None = None
) -> tuple[int, str]:
    """Run ``STATE_DIR/name`` as ``./name`` from ``folder``, detached in its own session, and poll it
    until it ends: nothing waits on the machine meanwhile, so it can be snapshotted.

    Returns:
        The script's exit code (``TIMED_OUT`` once it was killed for the timeout) and its output.
    """
    path = f"{STATE_DIR}/{name}"
    rc, out, pid = f"{path}.rc", f"{path}.out", f"{path}.pid"
    inner = shlex.quote(f"echo $$ > {pid}; {path} > {out} 2>&1; echo $? > {rc}")
    code, output = await machine.run(
        f"rm -f {rc} {out} {pid} && chmod +x {path} && cd {shlex.quote(folder)} && (command -v setsid "
        f">/dev/null && exec setsid sh -c {inner} || exec sh -c {inner}) </dev/null >/dev/null 2>&1 &",
        CHECK_TIMEOUT_S,
    )
    if code != 0:
        return code, output
    started = time.monotonic()
    while True:
        seconds = time.monotonic() - started
        if tick is not None:
            await tick(seconds)
        finished = await _poll(machine, rc)
        if finished is not None:
            break
        if seconds > timeout:
            await machine.run(kill_script(name), CHECK_TIMEOUT_S, isolated=True)
            finished = TIMED_OUT
            break
        await asyncio.sleep(min(POLL_MAX_S, max(POLL_MIN_S, seconds / POLL_SLOWDOWN)))
    _, output = await machine.run(f"cat {out} 2>/dev/null", CHECK_TIMEOUT_S, isolated=True)
    suffix = f"\n[killed after {timeout:g} s]" if finished == TIMED_OUT else ""
    return finished, output + suffix


async def _poll(machine: Machine, rc: str) -> int | None:
    """The exit code a detached script left in ``rc``; ``None`` while it runs or the poll failed."""
    try:
        _, text = await machine.run(f"cat {rc} 2>/dev/null", POLL_TIMEOUT_S, isolated=True)
    except (VMError, OSError, TimeoutError):
        return None
    text = text.strip()
    return int(text) if text.lstrip("-").isdigit() else None


def kill_script(name: str) -> str:
    """Kill a detached script and everything in its session (what a crash or a lost terminal does)."""
    pid = f"{STATE_DIR}/{name}.pid"
    return (
        f"p=$(cat {pid} 2>/dev/null) || {{ echo 'not running'; exit 0; }}; "
        "kill -KILL -- -$p 2>/dev/null || kill -KILL $p 2>/dev/null; echo killed; true"
    )
