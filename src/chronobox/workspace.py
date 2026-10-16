"""An agent's workspace: one live machine plus checkpoints, rollback and forks.

The workspace has a *main* machine, where real work happens, and optional
*forks*: exact copies (memory, processes, disk) where an agent can try something
risky, let time pass, and check the result before touching the main machine.
What is available depends on the profile:

- ``alone``: run commands on the main machine;
- ``rollback``: + named checkpoints and rollback to them, on one timeline;
- ``chronobox``: + forks (try on a copy, compare, promote or discard), automatic
  checkpoints before risky commands, reports from discarded or promoted forks, and
  lessons shown again on later forks and rollbacks.

With ``guard=True`` (chronobox profile only), destructive commands on the main
machine are refused until the same command succeeded on a fork, unless forced.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from chronobox import commands
from chronobox.vm import FirecrackerBackend, Machine, MachineSpec, Snapshot

MAIN = "main"
START = "start"
MIN_WAIT_S = 1.0
MAX_WAIT_S = 600.0
WAIT_MARGIN_S = 30.0
QUICK_COMMAND_TIMEOUT_S = 60.0
COMPARE_TIMEOUT_S = 120.0
MAX_LABEL_LENGTH = 40
REPORT_MAX_COMMANDS = 15
REPORT_MAX_FILES = 25
SHOWN_OTHER_LESSONS = 8
FORK_MARKER = "/root/.chronobox/fork-marker"
# Files modified on a fork since it was created, skipping virtual and volatile trees.
CHANGED_FILES = (
    "find / -xdev \\( -path /proc -o -path /sys -o -path /dev -o -path /run -o -path /tmp "
    "-o -path /data -o -path /root/.chronobox -o -path /var/log -o -path /var/cache "
    f"-o -path '*/__pycache__' \\) -prune -o -type f -newer {FORK_MARKER} -print 2>/dev/null"
)
GUARD_REFUSAL = (
    "[chronobox guard] not run: this command looks risky and has not been rehearsed. "
    "Fork the machine, run it there (where=<fork>), check the result (wait if effects can "
    "be delayed), then run it here again, or re-send it with force=true if you are sure."
)

Listener = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class Booted:
    """A main machine started by a custom boot.

    Attributes:
        machine: The running machine.
        snapshot: The machine as it was booted, if already saved (e.g. a warm start's): it
            becomes the start checkpoint instead of a new snapshot.
    """

    machine: Machine
    snapshot: Snapshot | None = None


Boot = Callable[[], Awaitable[Booted]]


class Profile(StrEnum):
    ALONE = "alone"
    ROLLBACK = "rollback"
    CHRONOBOX = "chronobox"


class WorkspaceError(RuntimeError):
    """A request the agent made that cannot be served (unknown fork, wrong profile...)."""


@dataclass
class Checkpoint:
    label: str
    snapshot: Snapshot
    created: float
    note: str = ""
    lessons: list[str] = field(default_factory=list)


@dataclass
class Fork:
    name: str
    machine: Machine
    origin: str
    created: float
    commands: list[tuple[str, int]] = field(default_factory=list)


@dataclass(frozen=True)
class Lesson:
    text: str
    source: str
    checkpoint: str


class Workspace:
    """The main machine, its checkpoints and forks, for one agent session.

    Args:
        backend: Starts, snapshots and restores machines.
        spec: How to start the main machine.
        profile: What the agent may do (see module docstring).
        guard: Refuse unrehearsed destructive commands on main (chronobox profile).
        max_output: Command output beyond this many characters is shortened in the middle.
        boot: Starts the main machine instead of ``backend.start(spec)`` (e.g. a project's
            warm start); its machine and snapshot must belong to ``backend``.
    """

    def __init__(
        self,
        backend: FirecrackerBackend,
        spec: MachineSpec,
        *,
        profile: Profile = Profile.CHRONOBOX,
        guard: bool = False,
        max_output: int = 6000,
        boot: Boot | None = None,
    ) -> None:
        """Set up an empty workspace; ``start`` boots the main machine.

        Raises:
            ValueError: If ``guard`` is set without the chronobox profile.
        """
        if guard and profile is not Profile.CHRONOBOX:
            raise ValueError("guard needs forks (profile chronobox)")
        self.backend = backend
        self.spec = spec
        self.profile = profile
        self.guard = guard
        self.auto_checkpoint = profile is Profile.CHRONOBOX
        self.max_output = max_output
        self._boot = boot
        self.main: Machine | None = None
        self.forks: dict[str, Fork] = {}
        self.checkpoints: dict[str, Checkpoint] = {}
        self.lessons: list[Lesson] = []
        self.events: list[dict[str, Any]] = []
        self.before_main_switch: list[Listener] = []
        self.after_main_switch: list[Listener] = []
        self._rehearsed: set[str] = set()
        self._auto_checkpoints = 0

    # ---- lifecycle
    async def start(self) -> None:
        """Boot the main machine and, unless the profile is alone, record it as the ``start`` checkpoint
        (the boot's own snapshot when it has one, e.g. a warm start).
        """
        booted = await self._boot() if self._boot else Booted(await self.backend.start(self.spec))
        self.main = booted.machine
        if self.profile is Profile.ALONE:
            return
        if booted.snapshot is None:
            await self._save_checkpoint(START, "initial state")
        else:
            self.checkpoints[START] = Checkpoint(START, booted.snapshot, time.time(), "initial state")

    async def close(self) -> None:
        """Stop the main machine and every fork; checkpoints stay with the backend until its cleanup."""
        machines = [self.main, *(fork.machine for fork in self.forks.values())]
        for machine in machines:
            if machine is not None:
                await self.backend.stop(machine)
        self.forks.clear()
        self.main = None

    # ---- commands
    async def run(
        self, command: str, where: str = MAIN, timeout: float | None = None, force: bool = False
    ) -> str:
        """Run a shell command on main or on a fork; returns its output and exit code."""
        machine = self._machine(where)
        on_main = where == MAIN
        if on_main and self._guard_refuses(command, force):
            self._log("guard_refused", command=command)
            return GUARD_REFUSAL
        prefix = ""
        if on_main and self.auto_checkpoint and commands.is_risky(command):
            prefix = await self._auto_checkpoint(command)
        started = time.time()
        code, output = await machine.run(command, timeout)
        if where in self.forks:
            self._note_fork_command(self.forks[where], command, code)
        self._log("run", where=where, command=command, code=code, seconds=round(time.time() - started, 2))
        return prefix + self._format(output, code)

    async def wait(self, seconds: float, where: str = MAIN) -> str:
        """Let time pass while services and workloads keep running."""
        seconds = max(MIN_WAIT_S, min(float(seconds), MAX_WAIT_S))
        await self._machine(where).run(f"sleep {seconds:g}", seconds + WAIT_MARGIN_S)
        self._log("wait", where=where, seconds=seconds)
        return f"waited {seconds:g}s on {where}"

    # ---- checkpoints and rollback (profiles rollback, chronobox)
    async def checkpoint(self, label: str, note: str = "") -> str:
        """Save the whole main machine under ``label``.

        Raises:
            WorkspaceError: If the profile has no checkpoints, or ``label`` is empty or already used.
        """
        self._require(Profile.ROLLBACK, Profile.CHRONOBOX)
        label = _clean_label(label)
        if label in self.checkpoints:
            raise WorkspaceError(f"checkpoint {label!r} already exists")
        await self._save_checkpoint(label, note)
        self._log("checkpoint", label=label)
        return f"checkpoint {label!r} saved (whole machine: disk, memory, running processes)"

    def list_checkpoints(self) -> str:
        """One entry per checkpoint (age, note, lessons, forks made from it), then the open forks."""
        lines = [self._describe_checkpoint(checkpoint) for checkpoint in self.checkpoints.values()]
        if self.forks:
            lines.append("forks: " + ", ".join(self.forks))
        return "\n".join(lines) or "no checkpoints"

    async def rollback(self, label: str, lesson: str = "") -> str:
        """Replace the main machine with a restore of checkpoint ``label``, optionally recording a lesson.

        Raises:
            WorkspaceError: If the profile has no rollback or the checkpoint does not exist.
        """
        self._require(Profile.ROLLBACK, Profile.CHRONOBOX)
        checkpoint = self._checkpoint_named(label)
        restored = await self.backend.restore(checkpoint.snapshot)
        await self._replace_main(restored)
        known = self._lessons_text(label)
        if lesson:
            self._add_lesson(lesson, "rollback", label)
        self._log("rollback", label=label, lesson=lesson)
        return (
            f"rolled back the main machine to {label!r} (disk, memory and processes restored "
            "exactly; anything done after it is gone)" + known
        )

    # ---- forks (profile chronobox)
    async def fork(self, name: str, from_checkpoint: str | None = None) -> str:
        """Start an independent copy of the main machine (as it is now, or as ``from_checkpoint``).

        A marker file touched in the fork at creation (``FORK_MARKER``) dates its changes: the
        fork's report lists the files newer than it.

        Raises:
            WorkspaceError: If the profile has no forks, the name is taken or the checkpoint is unknown.
        """
        self._require(Profile.CHRONOBOX)
        name = _clean_label(name)
        if name in self.forks or name == MAIN:
            raise WorkspaceError(f"fork {name!r} already exists")
        if from_checkpoint is None:
            origin = f"fork-point-{name}"
            await self._save_checkpoint(origin, f"state when fork {name!r} was created")
        else:
            origin = self._checkpoint_named(from_checkpoint).label
        machine = await self.backend.restore(self.checkpoints[origin].snapshot)
        await machine.run(
            f"mkdir -p {FORK_MARKER.rsplit('/', 1)[0]} && touch {FORK_MARKER}", QUICK_COMMAND_TIMEOUT_S
        )
        self.forks[name] = Fork(name, machine, origin, time.time())
        self._log("fork", name=name, source=from_checkpoint or MAIN)
        return (
            f"fork {name!r} created from {origin!r}: an exact, independent copy of the machine. "
            f"Run commands in it with where={name!r}; the main machine is untouched until you "
            "apply the change there or promote the fork." + self._lessons_text(origin)
        )

    async def changes(self, name: str) -> str:
        """What a fork did since it was created: commands and files changed."""
        self._require(Profile.CHRONOBOX)
        return await self._fork_report(self._fork(name), closing=False)

    async def compare(self, command: str, forks: list[str] | None = None) -> str:
        """Run ``command`` on main and on ``forks`` (default: all) and show the outputs side by side.

        Raises:
            WorkspaceError: If the profile has no forks or a named fork does not exist.
        """
        self._require(Profile.CHRONOBOX)
        targets = [MAIN, *(forks or list(self.forks))]
        parts = []
        for where in targets:
            code, output = await self._machine(where).run(command, COMPARE_TIMEOUT_S)
            parts.append(f"== {where} (exit {code})\n{output[-2000:]}")
        self._log("compare", command=command, targets=targets)
        return "\n".join(parts)

    async def promote(self, name: str) -> str:
        """Make a fork the main machine, after checkpointing the old main as ``before-promote-<name>``.

        Raises:
            WorkspaceError: If the profile has no forks or the fork does not exist.
        """
        self._require(Profile.CHRONOBOX)
        fork = self._fork(name)
        report = await self._fork_report(fork, closing=False)
        saved_as = f"before-promote-{name}"
        await self._save_checkpoint(saved_as, "main machine before a fork replaced it")
        del self.forks[name]
        await self._replace_main(fork.machine)
        self._log("promote", name=name)
        age = time.time() - fork.created
        return (
            f"fork {name!r} is now the main machine (the previous main was checkpointed as "
            f"{saved_as!r}). Everything the old main did in the last {age:.0f}s since the fork "
            f"was created (including work done by its services) is replaced by the fork's state.\n"
            f"{report}"
        )

    async def discard(self, name: str, lesson: str = "") -> str:
        """Stop a fork and return its report; record ``lesson``, or an automatic one if commands failed.

        Raises:
            WorkspaceError: If the profile has no forks or the fork does not exist.
        """
        self._require(Profile.CHRONOBOX)
        fork = self._fork(name)
        report = await self._fork_report(fork, closing=True)
        del self.forks[name]
        await self.backend.stop(fork.machine)
        failed = [command for command, code in fork.commands if code != 0]
        if lesson:
            self._add_lesson(lesson, f"fork {name}", fork.origin)
        elif failed:
            self._add_lesson(
                f"(automatic) on fork {name!r}, {len(failed)} command(s) failed, last: {failed[-1][:160]}",
                "auto",
                fork.origin,
            )
        self._log("discard", name=name, lesson=lesson)
        return f"fork {name!r} discarded.\n{report}"

    # ---- helpers
    def _guard_refuses(self, command: str, force: bool) -> bool:
        """Whether the guard blocks ``command`` on main: destructive, not forced, and not yet run
        successfully on a fork.
        """
        if not self.guard or force or not commands.needs_rehearsal(command):
            return False
        return commands.normalize(command) not in self._rehearsed

    async def _auto_checkpoint(self, command: str) -> str:
        """Checkpoint main before a risky command; returns the notice put before its output."""
        self._auto_checkpoints += 1
        label = f"auto-{self._auto_checkpoints}"
        await self._save_checkpoint(label, f"before: {command[:80]}")
        return f"[chronobox: checkpoint {label!r} saved automatically before this command]\n"

    def _note_fork_command(self, fork: Fork, command: str, code: int) -> None:
        """Record a fork's command; a successful destructive one counts as rehearsed for the guard."""
        fork.commands.append((command, code))
        if code == 0 and commands.needs_rehearsal(command):
            self._rehearsed.add(commands.normalize(command))

    async def _replace_main(self, machine: Machine) -> None:
        """Swap in a new main machine, stop the old one, and notify the listeners around the switch."""
        for listener in self.before_main_switch:
            await listener()
        previous, self.main = self.main, machine
        if previous is not None:
            await self.backend.stop(previous)
        for listener in self.after_main_switch:
            await listener()

    async def _save_checkpoint(self, label: str, note: str) -> None:
        """Snapshot the main machine and keep it under ``label`` (replacing any checkpoint of that name)."""
        snapshot = await self.backend.snapshot(self._machine(MAIN))
        self.checkpoints[label] = Checkpoint(label, snapshot, time.time(), note)

    async def _fork_report(self, fork: Fork, *, closing: bool) -> str:
        """What a fork did: its state-changing commands, failures and changed files.

        Args:
            fork: The fork to report on.
            closing: The fork is being discarded: remind the agent that main is unchanged.

        Returns:
            The report, one item per line.
        """
        lines = [
            f"Report from fork {fork.name!r} (started from {fork.origin!r}, {len(fork.commands)} commands):"
        ]
        changed = [(command, code) for command, code in fork.commands if commands.changes_state(command)]
        if changed:
            lines.append("  state-changing commands (exit code):")
            lines += [f"    [{code}] {command[:200]}" for command, code in changed[-REPORT_MAX_COMMANDS:]]
        failed = [command for command, code in fork.commands if code != 0]
        if failed:
            lines.append(f"  {len(failed)} command(s) failed; last: {failed[-1][:200]}")
        _, output = await fork.machine.run(CHANGED_FILES, QUICK_COMMAND_TIMEOUT_S)
        files = [line for line in output.splitlines() if line.startswith("/")]
        if files:
            lines.append(
                f"  files changed on the fork since it was created ({len(files)}): {_shorten_list(files)}"
            )
        if closing and any(code == 0 for _, code in changed):
            lines.append(
                "  NOTE: the main machine is unchanged by this fork. If what you did here worked, "
                "apply the same change to main (the commands above), then verify it there."
            )
        return "\n".join(lines)

    def _describe_checkpoint(self, checkpoint: Checkpoint) -> str:
        """A checkpoint's line for ``list_checkpoints``, with its lessons and open forks."""
        age = time.time() - checkpoint.created
        note = f": {checkpoint.note}" if checkpoint.note else ""
        lines = [f"- {checkpoint.label} ({age:.0f}s ago){note}"]
        lines += [f"    lesson: {lesson}" for lesson in checkpoint.lessons]
        lines += [
            f"    fork {fork.name!r} (open, {len(fork.commands)} commands)"
            for fork in self.forks.values()
            if fork.origin == checkpoint.label
        ]
        return "\n".join(lines)

    def _add_lesson(self, text: str, source: str, checkpoint: str) -> None:
        """Keep a lesson, attached to its checkpoint so later forks and rollbacks there show it."""
        self.lessons.append(Lesson(text, source, checkpoint))
        if checkpoint in self.checkpoints:
            self.checkpoints[checkpoint].lessons.append(text)

    def _lessons_text(self, label: str) -> str:
        """Lessons to show when arriving at checkpoint ``label``: all from it, the last few from elsewhere."""
        if not self.lessons:
            return ""
        here = [lesson for lesson in self.lessons if lesson.checkpoint == label]
        elsewhere = [lesson for lesson in self.lessons if lesson.checkpoint != label]
        lines = ["", "Lessons recorded so far:"]
        lines += [f"  - from this state ({lesson.source}): {lesson.text}" for lesson in here]
        lines += [
            f"  - {lesson.source}, from {lesson.checkpoint!r}: {lesson.text}"
            for lesson in elsewhere[-SHOWN_OTHER_LESSONS:]
        ]
        return "\n".join(lines)

    def machine(self, where: str = MAIN) -> Machine:
        """The main machine or a fork's.

        Raises:
            WorkspaceError: If it does not exist.
        """
        return self._machine(where)

    def _machine(self, where: str) -> Machine:
        """The main machine or a fork's.

        Raises:
            WorkspaceError: If the workspace is not started or the fork does not exist.
        """
        if where != MAIN:
            return self._fork(where).machine
        if self.main is None:
            raise WorkspaceError("workspace not started")
        return self.main

    def _fork(self, name: str) -> Fork:
        """The open fork called ``name``.

        Raises:
            WorkspaceError: If the profile has no forks or there is no such fork.
        """
        if self.profile is not Profile.CHRONOBOX:
            raise WorkspaceError("forks are not available in this profile")
        try:
            return self.forks[name]
        except KeyError:
            raise WorkspaceError(f"no fork named {name!r}; forks: {list(self.forks)}") from None

    def _checkpoint_named(self, label: str) -> Checkpoint:
        """The checkpoint called ``label``.

        Raises:
            WorkspaceError: If there is none.
        """
        try:
            return self.checkpoints[label]
        except KeyError:
            raise WorkspaceError(
                f"no checkpoint named {label!r}; checkpoints: {list(self.checkpoints)}"
            ) from None

    def _require(self, *profiles: Profile) -> None:
        """Refuse a tool the current profile does not offer.

        Raises:
            WorkspaceError: If the profile is not one of ``profiles``.
        """
        if self.profile not in profiles:
            raise WorkspaceError(f"not available in profile {self.profile.value!r}")

    def _format(self, output: str, code: int) -> str:
        """Command output, shortened to ``max_output``, followed by its exit code."""
        return f"{_shorten(output, self.max_output)}[exit code {code}]"

    def _log(self, kind: str, **details: Any) -> None:
        """Append an event (with a timestamp) to ``events``, the record harnesses read."""
        self.events.append({"t": round(time.time(), 2), "event": kind, **details})


def _clean_label(label: str) -> str:
    """A label safe for checkpoint and fork names: other characters become ``-``, length is capped.

    Raises:
        WorkspaceError: If nothing is left.
    """
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label.strip())[:MAX_LABEL_LENGTH]
    if not label:
        raise WorkspaceError("label must not be empty")
    return label


def _shorten(text: str, limit: int) -> str:
    """Keep the start and the end of long output."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n{text[-half:]}"


def _shorten_list(items: list[str]) -> str:
    """The first few items, comma-separated, with a count of the rest."""
    shown = ", ".join(items[:REPORT_MAX_FILES])
    hidden = len(items) - REPORT_MAX_FILES
    return f"{shown} (+{hidden} more)" if hidden > 0 else shown
