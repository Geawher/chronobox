"""The agent's ``verify_script`` and ``verify_result`` tools: start replays, wait for them or leave
them in the background, and save or withdraw the hand-off."""

from __future__ import annotations

import asyncio
import itertools
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

from chronobox.config import HandoffSettings
from chronobox.handoff.files import HANDOFF_DIR, LOCAL, WITHDRAWN_DIR, Project, save_handoff, withdraw_handoff
from chronobox.handoff.replay import CRASH_SUFFIX, REPLAY_FORK, replay_script
from chronobox.handoff.report import Replay
from chronobox.handoff.review import review
from chronobox.handoff.scripts import (
    DEFAULT_SCRIPT_TIMEOUT_S,
    MAX_SCRIPT_TIMEOUT_S,
    SCRIPT_FILE,
    Options,
    Scripts,
    how_to_run,
)
from chronobox.project import (
    EXPORTS_DIR,
    ProjectBoot,
    ProjectError,
)
from chronobox.service import Method
from chronobox.vm import VMError
from chronobox.workspace import MAIN, START, Workspace, WorkspaceError

MAX_WAIT_S = 600.0


# A failed replay whose script ran this long gets a hint to check the logic on a sample first.
SLOW_REPLAY_S = 120.0


# verify_script waits this long for a replay, then leaves it running in the background.
FOREGROUND_WAIT_S = 300.0


def handoff_tools(
    workspace: Workspace,
    boot: ProjectBoot | None,
    *,
    settings: HandoffSettings | None = None,
    exports: Path | None = None,
) -> dict[str, Method]:
    """The ``verify_script`` and ``verify_result`` tool methods, for ``WorkspaceService.extra_methods``.

    Args:
        workspace: The agent's workspace (with forks).
        boot: The project's boot; ``None`` for a machine without a project (the hand-off is the
            scripts alone, nothing is deployed).
        settings: What every replay adds; by default the project's ``[handoff]`` table.
        exports: Where hand-offs are saved; by default the project's ``.chronobox/exports``.

    Raises:
        ValueError: If there is neither a project nor an ``exports`` folder.
    """
    project = Project(boot) if boot is not None else None
    if boot is None and exports is None:
        raise ValueError("a hand-off without a project needs an exports folder")
    if settings is None:
        settings = boot.config.handoff if boot is not None else HandoffSettings()
    exports = exports if exports is not None else boot.config.path / EXPORTS_DIR  # type: ignore[union-attr]
    folder, withdrawn = exports / HANDOFF_DIR.name, exports / WITHDRAWN_DIR.name
    target = project.config.target if project is not None else "/root"
    replays: dict[str, tuple[asyncio.Task[str], Replay]] = {}
    numbers = itertools.count(1)
    # The replay started last: only its result saves or withdraws the hand-off, whatever finishes first.
    newest = [""]

    async def replay_and_save(scripts: Scripts, replay: Replay, options: Options, goal: str) -> str:
        """Replay; on success (and the review, if any) save the hand-off, on failure withdraw the
        one saved before."""
        try:
            await replay_script(workspace, project, settings, scripts, replay, options)
            report = replay.report()
            if replay.fork != newest[0]:
                return (
                    f"{report}\nnot saved: {newest[0]} was started after this replay, and the hand-off is "
                    f"what the newest replay shows (collect it with verify_result(fork={newest[0]!r}))"
                )
            if not replay.succeeded:
                gone = withdraw_handoff(folder, withdrawn)
                note = f" The hand-off saved before was withdrawn (moved to {withdrawn})." if gone else ""
                fix = "fix the script (or the project files) and verify it again"
                fix += _slow_hint(replay, options)
                return f"{report}\nnot saved: {fix}.{note}"
            patch = await project.patch(workspace, replay.code_from) if project is not None else ""
            if settings.review:
                report += "\n" + await review(settings.review, goal, scripts, options, report)
            report = f"{how_to_run(scripts, bool(patch), target)}\n{report}"
            saved = save_handoff(folder, scripts, patch, report)
        except (WorkspaceError, VMError, ProjectError, tarfile.TarError, OSError) as error:
            return f"error: {error}"
        files = ", ".join(sorted(path.name for path in saved.iterdir()))
        unchanged = " (no project changes)" if project is not None and not patch else ""
        return f"{report}\nhand-off saved in {saved}: {files}{unchanged}"

    async def collect(fork: str) -> str:
        """The result of a finished replay, forgotten once given."""
        task, _ = replays.pop(fork)
        return task.result()

    async def verify_script(
        script: str,
        check: str,
        pre: str = "",
        rollback: str = "",
        assume: str = "",
        load: str = "",
        load_check: str = "",
        sample: str = "",
        twice: bool = True,
        background: bool = False,
        code_from: str = MAIN,
        checkpoint: str = START,
        timeout: float = DEFAULT_SCRIPT_TIMEOUT_S,
        goal: str = "",
    ) -> str:
        """Replay the hand-off and save or withdraw it; in the background, only start the replay."""
        if not script.strip():
            return "error: the script is empty"
        if not check.strip():
            return (
                "error: a check is required: a command that fails unless the change works as the user "
                "asked (use it as users would, including new writes), run after the script"
            )
        if settings.review and not goal.strip():
            return (
                "error: this project reviews each hand-off's check against what the user asked: pass "
                "goal, the user's request in their words"
            )
        if code_from == LOCAL and project is None:
            return "error: code_from='local' needs a project"
        scripts = Scripts(script, pre, rollback, assume)
        timeout = min(max(float(timeout), 1.0), MAX_SCRIPT_TIMEOUT_S)
        options = Options(check, load, load_check, sample, bool(twice), timeout)
        await _close_old_replays(workspace, replays)
        replay = Replay(_free_fork_name(workspace, replays, numbers), checkpoint, code_from)
        newest[0] = replay.fork
        task = asyncio.create_task(replay_and_save(scripts, replay, options, goal))
        replays[replay.fork] = (task, replay)
        if background:
            return (
                f"replaying in the background on fork {replay.fork!r}; keep working, then call "
                f"verify_result(fork={replay.fork!r}) for the result (it can wait for it)"
            )
        try:
            await asyncio.wait_for(asyncio.shield(task), FOREGROUND_WAIT_S)
        except TimeoutError:
            return (
                f"still replaying after {FOREGROUND_WAIT_S:.0f} s, now at: {replay.current}. Nothing is "
                f"saved until it passes: keep working, then call verify_result(fork={replay.fork!r}, "
                "wait=...) for the result"
            )
        return await collect(replay.fork)

    async def verify_result(fork: str = "", wait: float = 0) -> str:
        """The result of a replay started in the background, waiting up to ``wait`` seconds for it."""
        if not replays:
            return "error: no replay is waiting to be collected"
        name = fork or next(reversed(replays))
        if name not in replays:
            return f"error: no replay on fork {name!r}; waiting: {', '.join(replays)}"
        task, replay = replays[name]
        try:
            await asyncio.wait_for(asyncio.shield(task), min(max(float(wait), 0.0), MAX_WAIT_S))
        except TimeoutError:
            seconds = time.monotonic() - replay.started
            return f"still replaying on fork {name!r} after {seconds:.0f} s, now at: {replay.current}"
        return await collect(name)

    return {"verify_script": verify_script, "verify_result": verify_result}


def _slow_hint(replay: Replay, options: Options) -> str:
    """For a slow failed replay of the full data: how to check the logic faster ("" otherwise)."""
    first = next((step for step in replay.steps if step.label == SCRIPT_FILE), None)
    if replay.sampled or options.sample or first is None or first.seconds < SLOW_REPLAY_S:
        return ""
    return (
        f". The script ran {first.seconds:.0f} s here: check the logic on a sample first (sample= "
        "commands that shrink the data, e.g. delete most rows), then replay the full data once it passes"
    )


async def _close_old_replays(workspace: Workspace, running: dict[str, object]) -> None:
    """Stop the forks of earlier replays (and their crash copies), except those still running: a
    failed replay stays open to inspect until the next one, so they do not pile up in memory."""
    for name, fork in list(workspace.forks.items()):
        replay = name.split(CRASH_SUFFIX)[0]
        if name.startswith(f"{REPLAY_FORK}-") and replay not in running:
            del workspace.forks[name]
            await workspace.backend.stop(fork.machine)


def _free_fork_name(workspace: Workspace, taken: dict[str, object], numbers: Iterator[int]) -> str:
    """The next ``replay-N`` (from ``numbers``, never reused in a session, so a closed replay's name
    always means that replay) that is neither an open fork nor a replay being made."""
    names = (f"{REPLAY_FORK}-{number}" for number in numbers)
    return next(name for name in names if name not in workspace.forks and name not in taken)
