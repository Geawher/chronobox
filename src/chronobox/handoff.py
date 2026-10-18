"""Script hand-off: check a change script by replaying it on a fresh fork, then save it for the user.

Some changes are actions, not file edits: a migration or a backfill, restarting services in order,
a setting outside the project. A patch cannot carry them, so the agent writes them as a shell
script. ``verify_script`` replays that script the way the user will run it on the real machine
(``./apply.sh``: by its ``#!`` line, ``/bin/sh`` when it has none): on a new fork of a checkpoint
(by default ``start``, the machine as it was before the agent's work), after deploying the
project's changed files there (from main, a fork, or the local folder).

A replay passes only when all of these hold:

- ``assume`` (what the script relies on) holds on the replay, as it must on the real machine;
- the script succeeds; with ``twice`` (the default) it also succeeds when run again once it ended,
  quickly (a retry must not redo the whole job), and when killed midway and run again, on copies
  of the replay taken while it ran: deployments get retried, sometimes after a crash;
- the agent's own ``check`` succeeds, then the project's ``[handoff]`` checks;
- every service still answers its ``ready`` command;
- the traffic run during the replay (the project's ``[handoff] load`` and the agent's ``load``)
  saw no problem (``load_check``);
- the ``[handoff] compare`` commands print the same on the agent's machine and on the replay;
- a project process the agent restarted on its machine was restarted by the script too, so the
  real machine does not keep running the old code.

``sample`` commands may shrink the replay's data first, so a long operation's logic is checked in
minutes; the report then says so. Then the project patch, the scripts and the replay's report are
saved together in ``.chronobox/exports/handoff/``. A failed replay withdraws the hand-off saved
before (moved to ``handoff-withdrawn``): it no longer matches what the agent is working on.

Scripts run detached in their own session and are polled: a snapshot resets the machine's
connections, so nothing may be waiting on one while the replay is snapshotted for the crash test.
A replay can also run in the background, collected later with ``verify_result``.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import itertools
import re
import shlex
import shutil
import tarfile
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from chronobox.config import HandoffSettings, ProjectConfig
from chronobox.project import (
    EXPORTS_DIR,
    ProjectBoot,
    ProjectError,
    _git_diff,
    _unpack_for_diff,
    pack_project,
    project_diff,
)
from chronobox.service import Method
from chronobox.vm import Machine, Snapshot, VMError
from chronobox.workspace import MAIN, START, Fork, Workspace, WorkspaceError

HANDOFF_DIR = EXPORTS_DIR / "handoff"
WITHDRAWN_DIR = EXPORTS_DIR / "handoff-withdrawn"
SCRIPT_FILE = "apply.sh"
PRE_FILE = "pre.sh"
ROLLBACK_FILE = "rollback.sh"
ASSUME_FILE = "assume.sh"
PATCH_FILE = "changes.patch"
REPORT_FILE = "replay.txt"
LOCAL = "local"  # code_from: the project folder on the host, as the agent edited it
STATE_DIR = "/root/.chronobox"
LOAD_LOG_DIR = "/var/log/chronobox"
REPLAY_FORK = "replay"
SHEBANG = "#!"
DEFAULT_SHEBANG = "#!/bin/sh"
SH_INTERPRETERS = frozenset({"sh", "dash", "ash"})
EXECUTABLE = 0o755
OUTPUT_LIMIT = 4000
STEP_OUTPUT_LIMIT = 1500
SHOWN_DIFFERENCES = 6
DEFAULT_SCRIPT_TIMEOUT_S = 900.0
MAX_SCRIPT_TIMEOUT_S = 3600.0
CHECK_TIMEOUT_S = 300.0
POLL_TIMEOUT_S = 30.0
LOAD_WARMUP_S = 2.0
READY_GRACE_S = 30.0
READY_POLL_S = 1.0
TIMED_OUT = 124
MAX_WAIT_S = 600.0
# A detached script is polled often at first, then every (seconds so far / POLL_SLOWDOWN),
# within these bounds.
POLL_MIN_S = 0.5
POLL_MAX_S = 5.0
POLL_SLOWDOWN = 20.0
# The second run must end within this share of the first run's time (but may always take
# SECOND_RUN_MIN_S): a script that skips finished work re-runs quickly.
SECOND_RUN_SHARE = 0.5
SECOND_RUN_MIN_S = 60.0
# While the first run goes, the replay is snapshotted at CRASH_FIRST_S, then each time CRASH_RATIO
# times later, keeping the last CRASH_KEPT; once it ends, up to CRASH_FORKS of them taken before
# CRASH_LATEST_SHARE of its time become copies where it is killed and run again.
CRASH_FIRST_S = 3.0
CRASH_RATIO = 3.0
CRASH_KEPT = 3
CRASH_FORKS = 2
CRASH_LATEST_SHARE = 0.9
# Bash features that /bin/sh (dash, busybox) lacks or reads differently.
BASHISMS = {
    "[[ ... ]]": re.compile(r"\[\["),
    "{1..N} ranges": re.compile(r"\{-?\d+\.\.-?\d+\}"),
    "the function keyword": re.compile(r"^\s*function\s+\w+", re.MULTILINE),
    "source": re.compile(r"^\s*source\s", re.MULTILINE),
    "arrays": re.compile(r"\b\w+=\("),
    "here-strings (<<<)": re.compile(r"<<<"),
    "${var//...} or ${var:N}": re.compile(r"\$\{\w+(//|:-?\d)"),
    "&> redirections": re.compile(r"&>"),
    "declare / let": re.compile(r"^\s*(declare|let)\s", re.MULTILINE),
}
NO_TRAFFIC_NOTE = (
    "nothing checked what users saw while the script ran (no load_check): if the change can lose or "
    "block writes (restarts, failovers, migrations under traffic), pass load_check (and load, unless "
    "the machine's own clients already write); a check after the script cannot see what happened during it"
)
# Steps whose passing the summary at the top of the report lists.
SHOWN_CHECKS = ("check `", "[handoff] check `", "load_check `", "[handoff] load_check `")
LOAD_CHECK_LABEL = "load_check `"
SAMPLE_CAVEAT = "the logic was checked, not the time or locks at full size"
# A failed replay whose script ran this long gets a hint to check the logic on a sample first.
SLOW_REPLAY_S = 120.0
# A reviewer (``[handoff] review``) gets this long to answer; its first line is OK when it sees no gap.
REVIEW_TIMEOUT_S = 300.0
REVIEW_OK = "OK"
REVIEW_PROMPT = """You review a change script before it runs on a production machine. \
It passed a replay on a copy of the machine, judged by the check below. Your job: find ways \
the change could still be wrong on production while that check passes.

What the user asked:
{goal}

The scripts (run as ./apply.sh on production; assume.sh first, if any):
{scripts}

The check that has to prove the change works:
{check}

Traffic during the replay: {load}
Traffic check after it: {load_check}

The replay's report:
{report}

Look for what the check does not test, in particular:
- writes by users while the script runs (lost, blocked, or missed by a backfill);
- rows or requests that arrive after the script (do they get the new behaviour?);
- data the script's logic gets wrong: records that look alike but are not the same, nulls, \
  other owners, values production has and the copy may not;
- effects that come later: scheduled jobs, compaction, caches, the next restart;
- production being bigger or different from the copy.

Answer in plain text. If the check covers what the user asked, the first line is exactly OK. \
Otherwise list at most 3 gaps, most likely first, each as: the gap in one sentence, then a shell \
command the check could add to catch it."""
# verify_script waits this long for a replay, then leaves it running in the background.
FOREGROUND_WAIT_S = 300.0
# Each line: port, pid, working folder and command line of a process listening on TCP. Only bash,
# awk and coreutils, which any image has: sockets from /proc/net/tcp*, their owners from /proc/*/fd.
LISTENERS = r"""
L=$(for f in /proc/net/tcp /proc/net/tcp6; do
  [ -r "$f" ] && awk 'NR>1 && $4=="0A" {split($2,a,":"); print a[2], $10}' "$f"; done | sort -u)
S=$(for fd in /proc/[0-9]*/fd/*; do
  l=$(readlink "$fd" 2>/dev/null) || continue
  case "$l" in "socket:["*) i=${l#socket:[}; echo "${i%]} ${fd#/proc/}";; esac; done)
echo "$L" | while read -r hex inode; do
  [ -n "$inode" ] || continue
  pid=$(echo "$S" | awk -v i="$inode" '$1==i {split($2,p,"/"); print p[1]; exit}')
  [ -n "$pid" ] || continue
  printf '%d\t%s\t%s\t%s\n' "0x$hex" "$pid" "$(readlink /proc/$pid/cwd)" \
    "$(tr '\0' ' ' < /proc/$pid/cmdline | cut -c1-100)"
done
"""

Tick = Callable[[float], Awaitable[None]]


class _Stopped(Exception):
    """A replay step failed in a way that makes the later steps meaningless."""


@dataclass(frozen=True)
class Scripts:
    """What the agent hands over.

    Attributes:
        apply: Run right after the project's new files are deployed.
        pre: Run before they are deployed, while the old code still runs (expand, then contract).
        rollback: Undoes ``apply`` once the old files are back; replayed too.
        assume: Checks what the others rely on (sizes, versions, which node leads...); run first,
            and the user stops if it fails.
    """

    apply: str
    pre: str = ""
    rollback: str = ""
    assume: str = ""

    def files(self) -> dict[str, str]:
        """The non-empty scripts by file name, each with a ``#!`` line (``/bin/sh`` if it had none)."""
        named = {
            ASSUME_FILE: self.assume, PRE_FILE: self.pre,
            SCRIPT_FILE: self.apply, ROLLBACK_FILE: self.rollback,
        }  # fmt: skip
        return {name: runnable(text) for name, text in named.items() if text.strip()}


@dataclass(frozen=True)
class Options:
    """How to replay a hand-off (see ``verify_script``).

    Attributes:
        check: Must succeed after the script: the change works as the user asked.
        load: Traffic the agent runs during the replay, next to the project's own.
        load_check: Run once the traffic stopped; fails if users had a problem.
        sample: Commands that shrink the replay's data before anything else.
        twice: Also run the script again once it ended, and after a kill midway.
        timeout: Limit for each run of a script, in seconds.
    """

    check: str
    load: str = ""
    load_check: str = ""
    sample: str = ""
    twice: bool = True
    timeout: float = DEFAULT_SCRIPT_TIMEOUT_S


@dataclass(frozen=True)
class Listener:
    """A process listening on a TCP port."""

    port: int
    pid: int
    cwd: str
    command: str


@dataclass(frozen=True)
class Step:
    """One command of a replay and how it ended."""

    label: str
    code: int
    output: str
    seconds: float


@dataclass(frozen=True)
class CrashPoint:
    """The replay as it was ``at`` seconds into the script's first run."""

    at: float
    snapshot: Snapshot


@dataclass
class Replay:
    """What happened when a hand-off was replayed on a fork.

    Attributes:
        fork: The fork it ran on; it stays open for inspection.
        checkpoint: The checkpoint (or fork) it started from.
        code_from: Where the deployed project files came from (a machine, or ``local``).
        steps: Every command run, in order.
        problems: Why the hand-off cannot be trusted; empty when it passed.
        notes: What the user should know although it passed.
        sampled: The data was shrunk first (``sample``).
        current: The step running now, for a replay in the background.
        started: When the replay started (``time.monotonic``).
    """

    fork: str
    checkpoint: str
    code_from: str
    steps: list[Step] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sampled: bool = False
    current: str = "creating the fork"
    started: float = field(default_factory=time.monotonic)

    @property
    def succeeded(self) -> bool:
        """Every step and check passed."""
        return not self.problems

    def showed(self) -> list[str]:
        """What the steps of a passed replay showed, for whoever reviews the hand-off."""
        shown = [f"on a sample of the data ({SAMPLE_CAVEAT})"] if self.sampled else []
        for step in self.steps:
            label = step.label
            if label == ASSUME_FILE:
                shown.append(f"./{ASSUME_FILE} passed on the copy (the user runs it first on production)")
            elif label == SCRIPT_FILE:
                shown.append(f"./{SCRIPT_FILE} finished in {step.seconds:.1f} s")
            elif label == f"{SCRIPT_FILE} (second run)":
                shown.append(f"run again right after, it finished in {step.seconds:.1f} s")
            elif label.startswith("[killed") and label.endswith("(run again)"):
                shown.append(f"{label.split(']')[0].lstrip('[')}: it then finished the job")
            elif label.startswith("load `"):
                shown.append(f"traffic during the script: {label.removesuffix(' (started)')}")
            elif label.startswith(SHOWN_CHECKS):
                shown.append(f"passed: {label}")
        if not any(LOAD_CHECK_LABEL in step.label for step in self.steps):
            shown.append("not checked: what users saw while the script ran (no load_check)")
        return shown

    def report(self) -> str:
        """The replay as text, for the agent and for ``replay.txt``."""
        verdict = "passed" if self.succeeded else "FAILED"
        sample = f" ON A SAMPLE of the data ({SAMPLE_CAVEAT})"
        lines = [
            f"replayed on fork {self.fork!r} (from checkpoint {self.checkpoint!r}, project files "
            f"from {self.code_from}){sample if self.sampled else ''}: {verdict}"
        ]
        for step in self.steps:
            limit = OUTPUT_LIMIT if step.label.startswith(SCRIPT_FILE) else STEP_OUTPUT_LIMIT
            lines += [
                f"--- {step.label}: exit {step.code} after {step.seconds:.1f} s",
                _shorten(step.output, limit),
            ]
        if self.succeeded:
            lines[1:1] = ["--- what this replay showed", *(f"- {line}" for line in self.showed())]
        if self.problems:
            lines += ["--- problems", *(f"- {problem}" for problem in self.problems)]
        if self.notes:
            lines += ["--- notes", *(f"- {note}" for note in self.notes)]
        return "\n".join(lines)


@dataclass(frozen=True)
class Project:
    """The project side of a hand-off: what is deployed on the replay and diffed for the patch."""

    boot: ProjectBoot

    @property
    def config(self) -> ProjectConfig:
        """The project's config."""
        return self.boot.config

    async def archive(self, workspace: Workspace, code_from: str) -> tuple[bytes, set[str]]:
        """The project files of ``code_from`` and the names it deleted since the session started.

        Raises:
            WorkspaceError: If ``code_from`` is neither a machine of the workspace nor ``local``.
            VMError: If the files cannot be read from the machine.
        """
        if code_from == LOCAL:
            archive = await asyncio.to_thread(pack_project, self.config)
            start = await asyncio.to_thread(self.boot.copied.read_bytes)
        else:
            machine = workspace.machine(code_from)
            archive = await machine.get(self.config.target, exclude=self.config.exclude)
            start = await asyncio.to_thread(self.boot.baseline.read_bytes)
        return archive, _file_names(start) - _file_names(archive)

    async def patch(self, workspace: Workspace, code_from: str) -> str:
        """The patch the user applies to their copy of the project.

        Raises:
            ProjectError: If git fails on the host.
            VMError: If the files cannot be read from the machine.
        """
        if code_from != LOCAL:
            baseline = await asyncio.to_thread(self.boot.baseline.read_bytes)
            return await project_diff(workspace.machine(code_from), self.config, baseline)
        copied = await asyncio.to_thread(self.boot.copied.read_bytes)
        current = await asyncio.to_thread(pack_project, self.config)
        return await asyncio.to_thread(_archive_diff, copied, current)


class ReplayRun:
    """Runs one replay on its fork, recording each step and collecting problems.

    Args:
        workspace: The agent's workspace.
        project: The project to deploy, or ``None`` for a machine without one (script only).
        settings: What the project adds to every replay.
        replay: Where the steps and problems go; its fork must exist.
        reference: The machine the agent worked on, which ``compare`` and the restart check
            hold the replay against.
        options: How to replay.
    """

    def __init__(
        self,
        workspace: Workspace,
        project: Project | None,
        settings: HandoffSettings,
        replay: Replay,
        reference: str,
        options: Options,
    ) -> None:
        """Set up the run."""
        self.workspace = workspace
        self.project = project
        self.settings = settings
        self.replay = replay
        self.machine = workspace.machine(replay.fork)
        self.reference = reference
        self.options = options
        self.folder = project.config.target if project else "/root"
        self.loads = [load for load in (settings.load, options.load) if load]

    async def __call__(self, scripts: Scripts) -> None:
        """Replay the hand-off: prepare, traffic, scripts, then every check."""
        try:
            await self._run(scripts)
        except _Stopped:
            await self.stop_loads()

    async def step(self, label: str, command: str, timeout: float = CHECK_TIMEOUT_S) -> Step:
        """Run ``command`` in the project folder; record it in the report and the fork's history."""
        self.replay.current = label
        started = time.monotonic()
        code, output = await self.machine.run(f"cd {shlex.quote(self.folder)} && {command}", timeout)
        return self.record(Step(label, code, output, time.monotonic() - started))

    async def script(self, label: str, name: str, timeout: float, tick: Tick | None = None) -> Step:
        """Run a saved script as the user would (``./name`` from the project folder); record it.

        Args:
            label: The step's name in the report.
            name: The script's file in ``STATE_DIR``.
            timeout: Seconds before it is killed (exit ``TIMED_OUT``).
            tick: Called with the seconds so far between polls, while nothing waits on the machine.
        """
        self.replay.current = label
        started = time.monotonic()
        code, output = await run_detached(self.machine, self.folder, name, timeout, tick)
        return self.record(Step(label, code, output, time.monotonic() - started))

    def record(self, step: Step) -> Step:
        """Add a step to the report and the fork's history."""
        self.replay.steps.append(step)
        self.workspace.forks[self.replay.fork].commands.append((step.label, step.code))
        return step

    def problem(self, text: str) -> None:
        """Record why the hand-off cannot be trusted."""
        self.replay.problems.append(text)

    async def required(
        self, label: str, command: str, problem: str, timeout: float = CHECK_TIMEOUT_S
    ) -> None:
        """Run a step the later ones depend on.

        Raises:
            _Stopped: If it fails (the problem is recorded first).
        """
        if (await self.step(label, command, timeout)).code != 0:
            self.problem(problem)
            raise _Stopped

    async def required_script(self, name: str, problem: str) -> None:
        """Run a saved script the later steps depend on.

        Raises:
            _Stopped: If it fails (the problem is recorded first).
        """
        if (await self.script(name, name, self.options.timeout)).code != 0:
            self.problem(problem)
            raise _Stopped

    async def after_script(self, before: dict[int, Listener], *, note_traffic: bool = True) -> None:
        """Everything checked once the script ended: checks, traffic, comparisons, restarts."""
        await self._checks()
        await self.stop_loads()
        await self._load_checks(note_traffic)
        await self._compare()
        if self.project is not None:
            await self._restarts(before)

    async def kill_and_retry(self, at: float, before: dict[int, Listener]) -> None:
        """On a copy taken ``at`` seconds into the first run: kill the script, run it again, check.

        Raises:
            _Stopped: If the second attempt fails.
        """
        self.replay.current = "killing the script"
        code, output = await self.machine.run(kill_script(SCRIPT_FILE), CHECK_TIMEOUT_S, isolated=True)
        self.record(Step(f"kill {SCRIPT_FILE} {at:.0f} s in", code, output, 0.0))
        step = await self.script(f"{SCRIPT_FILE} (run again)", SCRIPT_FILE, self.options.timeout)
        if step.code != 0:
            self.problem(f"{SCRIPT_FILE} failed when run again: it must finish the job after a crash halfway")
            raise _Stopped
        await self.after_script(before, note_traffic=False)

    async def stop_loads(self) -> None:
        """Stop the traffic ``_start_loads`` started (no-op without any)."""
        for number in range(1, len(self.loads) + 1):
            pid = _load_pid(number)
            stop = (
                f"[ -f {pid} ] && p=$(cat {pid}) && rm -f {pid} && {{ kill -TERM -- -$p 2>/dev/null || "
                "kill -TERM $p; sleep 1; kill -KILL -- -$p 2>/dev/null; true; }"
            )
            await self.machine.run(stop, CHECK_TIMEOUT_S)

    async def _run(self, scripts: Scripts) -> None:
        """The replay's steps in order.

        Raises:
            _Stopped: If a step the rest depends on fails.
        """
        for command in self.settings.prepare:
            await self.required(
                "[handoff] prepare", command, f"the project's handoff.prepare failed: {command}"
            )
        await self._sample()
        await self._put_scripts(scripts)
        self._note_shell_features(scripts)
        if scripts.assume.strip():
            await self.required_script(
                ASSUME_FILE,
                f"{ASSUME_FILE} failed on the replay: it checks what the script relies on, so on a "
                "machine like this one the user would stop before running anything",
            )
        before = await self._listeners(self.machine)
        await self._start_loads()
        if scripts.pre.strip():
            await self.required_script(
                PRE_FILE, f"{PRE_FILE} failed (it runs before the new files are deployed, with the old code)"
            )
        await self._deploy()
        first, points = await self._first_run()
        crashes = [
            asyncio.create_task(self._crash(number, point, before)) for number, point in enumerate(points, 1)
        ]
        try:
            if self.options.twice:
                await self._second_run(first)
            await self.after_script(before)
            if scripts.rollback.strip() and self.replay.succeeded:
                await self._rollback()
        finally:
            for at, crash in await asyncio.gather(*crashes):
                self._merge(at, crash)

    async def _sample(self) -> None:
        """Run the sample commands (the project's, then the agent's), which shrink the data.

        Raises:
            _Stopped: If one fails.
        """
        for command in (*self.settings.sample, self.options.sample):
            if command.strip():
                self.replay.sampled = True
                await self.required(
                    f"sample `{command}`", command, "the sample command failed", self.options.timeout
                )

    def _note_shell_features(self, scripts: Scripts) -> None:
        """Note scripts that run with sh but use bash features."""
        for name, text in scripts.files().items():
            features = bash_features(text)
            if features:
                self.replay.notes.append(
                    f"{name} runs with /bin/sh (its #! line), which is not bash on many machines, but uses "
                    f"{', '.join(features)}: start it with #!/bin/bash, or write it in sh"
                )

    async def _first_run(self) -> tuple[Step, list[CrashPoint]]:
        """The script's first run; with ``twice`` the replay is snapshotted while it runs, and the
        snapshots to kill it at are returned.

        Raises:
            _Stopped: If it fails.
        """
        points: list[CrashPoint] = []
        backend = self.workspace.backend
        due = [CRASH_FIRST_S]

        async def snapshot_when_due(seconds: float) -> None:
            """Snapshot the replay when the next crash point is due."""
            if seconds < due[0]:
                return
            due[0] = seconds * CRASH_RATIO
            try:
                points.append(CrashPoint(seconds, await backend.snapshot(self.machine)))
            except (VMError, OSError) as error:
                self.replay.notes.append(f"a snapshot for the crash test failed: {error}")
                due[0] = float("inf")
            if len(points) > CRASH_KEPT:
                await backend.release(points.pop(0).snapshot)

        tick = snapshot_when_due if self.options.twice else None
        first = await self.script(SCRIPT_FILE, SCRIPT_FILE, self.options.timeout, tick)
        chosen = [point for point in points if point.at <= first.seconds * CRASH_LATEST_SHARE][-CRASH_FORKS:]
        if first.code != 0:
            chosen = []
        for point in points:
            if point not in chosen:
                await backend.release(point.snapshot)
        if first.code != 0:
            self.problem(f"{SCRIPT_FILE} failed")
            raise _Stopped
        if self.options.twice and not chosen:
            self.replay.notes.append(
                f"{SCRIPT_FILE} ended within {first.seconds:.0f} s, too soon to be killed midway: only "
                "the second run checked a retry"
            )
        return first, chosen

    async def _second_run(self, first: Step) -> None:
        """Run the script again on the replay: it must succeed, and quickly.

        Raises:
            _Stopped: If it fails or takes about as long as the first run.
        """
        budget = min(self.options.timeout, max(SECOND_RUN_MIN_S, first.seconds * SECOND_RUN_SHARE))
        step = await self.script(f"{SCRIPT_FILE} (second run)", SCRIPT_FILE, budget)
        if step.code == TIMED_OUT and budget < self.options.timeout:
            self.problem(
                f"the second run of {SCRIPT_FILE} was still going after {budget:.0f} s (the first took "
                f"{first.seconds:.0f} s): a retry would redo the whole job. Make the script skip what "
                "is already done (check before each step), so running it again is quick"
            )
            raise _Stopped
        if step.code != 0:
            self.problem(f"{SCRIPT_FILE} failed when run a second time: make it safe to run again")
            raise _Stopped

    async def _crash(
        self, number: int, point: CrashPoint, before: dict[int, Listener]
    ) -> tuple[float, Replay]:
        """A copy of the replay at ``point``, where the script is killed and run again. The copy is
        stopped unless something failed there; then it stays open for inspection."""
        name = f"{self.replay.fork}-crash-{number}"
        crash = Replay(name, self.replay.fork, self.replay.code_from)
        backend = self.workspace.backend
        try:
            machine = await backend.restore(point.snapshot)
        except (VMError, OSError) as error:
            crash.notes.append(f"the copy to kill the script on could not start: {error}")
            return point.at, crash
        finally:
            await backend.release(point.snapshot)
        self.workspace.forks[name] = Fork(name, machine, self.replay.fork, time.time())
        run = ReplayRun(self.workspace, self.project, self.settings, crash, self.reference, self.options)
        try:
            await run.kill_and_retry(point.at, before)
        except _Stopped:
            await run.stop_loads()
        except (VMError, OSError) as error:
            crash.notes.append(f"the crash test could not finish: {error}")
        if crash.succeeded:
            del self.workspace.forks[name]
            await backend.stop(machine)
        return point.at, crash

    def _merge(self, at: float, crash: Replay) -> None:
        """Add a crash copy's steps, problems and notes to this replay."""
        where = f"killed {at:.0f} s in and run again"
        self.replay.steps += [
            Step(f"[{where}] {step.label}", step.code, step.output, step.seconds) for step in crash.steps
        ]
        kept = "" if crash.succeeded else f" (on fork {crash.fork!r}, kept for inspection)"
        self.replay.problems += [f"after the script was {where}{kept}: {issue}" for issue in crash.problems]
        self.replay.notes += [f"{where}: {note}" for note in crash.notes]

    async def _checks(self) -> None:
        """The agent's check, the project's checks, then every service's ready command."""
        check = self.options.check
        if (await self.step(f"check `{check}`", check)).code != 0:
            self.problem("your check failed")
        for command in self.settings.checks:
            if (await self.step(f"[handoff] check `{command}`", command)).code != 0:
                self.problem(f"the project's check failed: {command}")
        await self._services_ready("after the script")

    async def _load_checks(self, note_traffic: bool) -> None:
        """Each traffic check, once the traffic stopped; a note when none watched the users."""
        traffic = "your traffic" if self.options.load else "the machine's traffic"
        checks = [
            ("[handoff] load_check", self.settings.load_check, "the project's traffic"),
            ("load_check", self.options.load_check, traffic),
        ]
        for label, command, whose in checks:
            if command and (await self.step(f"{label} `{command}`", command)).code != 0:
                self.problem(f"{whose} saw a problem during the replay ({label} failed)")
        if note_traffic and not any(command for _, command, _ in checks):
            self.replay.notes.append(NO_TRAFFIC_NOTE)

    async def _services_ready(self, when: str) -> None:
        """Every service with a ``ready`` command answers within ``READY_GRACE_S``."""
        services = self.project.config.services if self.project is not None else ()
        for service in (service for service in services if service.ready):
            deadline = time.monotonic() + READY_GRACE_S
            code, output = await self.machine.run(service.ready, CHECK_TIMEOUT_S)
            while code != 0 and time.monotonic() < deadline:
                await asyncio.sleep(READY_POLL_S)
                code, output = await self.machine.run(service.ready, CHECK_TIMEOUT_S)
            if code != 0:
                answer = output.strip()[-200:]
                self.problem(f"service {service.name} is not ready {when} (`{service.ready}`: {answer})")

    async def _start_loads(self) -> None:
        """Start each traffic command in its own process group, so it can be stopped whole.

        Raises:
            _Stopped: If one cannot start.
        """
        for number, load in enumerate(self.loads, 1):
            quoted = shlex.quote(load)
            command = (
                f"mkdir -p {STATE_DIR} {LOAD_LOG_DIR} && (command -v setsid >/dev/null && exec setsid "
                f"sh -c {quoted} || exec sh -c {quoted}) > {LOAD_LOG_DIR}/handoff-load-{number}.log 2>&1 "
                f"< /dev/null & echo $! > {_load_pid(number)}"
            )
            await self.required(f"load `{load}` (started)", command, f"the traffic could not start: {load}")
        if self.loads:
            await asyncio.sleep(LOAD_WARMUP_S)

    async def _put_scripts(self, scripts: Scripts) -> None:
        """Copy the scripts to ``STATE_DIR`` on the fork."""
        for name, text in scripts.files().items():
            await self.machine.put(STATE_DIR, _single_file(name, text.encode()))

    async def _deploy(self) -> None:
        """Make the fork's project folder match ``code_from``'s files (no-op without a project).

        Raises:
            _Stopped: If files removed since the start cannot be deleted.
        """
        if self.project is None:
            return
        archive, deleted = await self.project.archive(self.workspace, self.replay.code_from)
        if deleted:
            names = " ".join(shlex.quote(name) for name in sorted(deleted))
            await self.required(
                "deploy: delete removed files", f"rm -f -- {names}", "could not delete removed files"
            )
        await self.machine.put(self.project.config.target, archive)

    async def _compare(self) -> None:
        """Each ``compare`` command prints the same on the reference machine and on the replay."""
        reference = self.workspace.machine(self.reference)
        for command in self.settings.compare:
            wrapped = f"cd {shlex.quote(self.folder)} && {command}"
            (_, expected), (code, actual) = await asyncio.gather(
                reference.run(wrapped, CHECK_TIMEOUT_S), self.machine.run(wrapped, CHECK_TIMEOUT_S)
            )
            same = expected.strip() == actual.strip()
            self.record(Step(f"[handoff] compare `{command}`", code, "same" if same else "different", 0.0))
            if not same:
                self.problem(difference(command, self.reference, expected, actual))

    async def _restarts(self, before: dict[int, Listener]) -> None:
        """A project process the agent restarted on its machine must be restarted on the replay too."""
        target = self.project.config.target if self.project is not None else ""
        reference, after = await asyncio.gather(
            self._listeners(self.workspace.machine(self.reference)), self._listeners(self.machine)
        )
        for port, old in before.items():
            in_project = old.cwd == target or old.cwd.startswith(f"{target}/")
            if not in_project or port not in reference or reference[port].pid == old.pid:
                continue
            if port not in after:
                self.problem(
                    f"nothing listens on port {port} after the replay; on {self.reference}: "
                    f"{reference[port].command}"
                )
            elif after[port].pid == old.pid:
                self.problem(
                    f"port {port} is still served by the process started before the new files were deployed "
                    f"(pid {old.pid}: {old.command}), but you restarted it on {self.reference}: the script "
                    "must restart it too (and wait until it answers), or the real machine keeps the old code"
                )

    async def _rollback(self) -> None:
        """Put the old project files back, run the rollback script, and check the services answer."""
        if self.project is not None:
            config = self.project.config
            start = await asyncio.to_thread(self.project.boot.baseline.read_bytes)
            current = await self.machine.get(config.target, exclude=config.exclude)
            added = _file_names(current) - _file_names(start)
            if added:
                names = " ".join(shlex.quote(name) for name in sorted(added))
                await self.step("rollback: delete new files", f"rm -f -- {names}")
            await self.machine.put(config.target, start)
        if (await self.script(ROLLBACK_FILE, ROLLBACK_FILE, self.options.timeout)).code != 0:
            self.problem(f"{ROLLBACK_FILE} failed (it runs once the old project files are back)")
            return
        await self._services_ready("after the rollback")

    async def _listeners(self, machine: Machine) -> dict[int, Listener]:
        """The processes listening on TCP ports, by port (empty if they cannot be read)."""
        code, output = await machine.run(LISTENERS, CHECK_TIMEOUT_S)
        listeners = {}
        for line in output.splitlines() if code == 0 else []:
            parts = line.split("\t")
            if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
                listeners[int(parts[0])] = Listener(int(parts[0]), int(parts[1]), parts[2], parts[3].strip())
        return listeners


async def replay_script(
    workspace: Workspace,
    project: Project | None,
    settings: HandoffSettings,
    scripts: Scripts,
    replay: Replay,
    options: Options,
) -> Replay:
    """Replay a hand-off on a new fork named ``replay.fork``, made from ``replay.checkpoint`` (the
    steps are in the module docstring).

    Raises:
        WorkspaceError: If the checkpoint or ``code_from`` does not exist, or forks are not available.
        VMError: If files cannot be moved or a command cannot be sent.
    """
    if replay.code_from != LOCAL:
        workspace.machine(replay.code_from)  # an unknown machine fails before a fork is made
    await workspace.fork(replay.fork, replay.checkpoint)
    reference = MAIN if replay.code_from == LOCAL else replay.code_from
    await ReplayRun(workspace, project, settings, replay, reference, options)(scripts)
    return replay


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


def runnable(script: str) -> str:
    """The script as saved and run: with its ``#!`` line, or ``#!/bin/sh`` added."""
    text = script.lstrip("\n")
    if not text.startswith(SHEBANG):
        text = f"{DEFAULT_SHEBANG}\n{text}"
    return text if text.endswith("\n") else text + "\n"


def bash_features(script: str) -> list[str]:
    """The bash features a script uses although its ``#!`` line runs it with sh."""
    first = runnable(script).splitlines()[0].removeprefix(SHEBANG).split()
    interpreter = first[-1] if first and first[0].endswith("env") else (first[0] if first else "")
    if PurePosixPath(interpreter).name not in SH_INTERPRETERS:
        return []
    return [feature for feature, pattern in BASHISMS.items() if pattern.search(script)]


def difference(command: str, reference: str, expected: str, actual: str) -> str:
    """Why a ``compare`` command's outputs differ: what only each side has, and what to do."""
    only_reference, only_replay = [], []
    lines = difflib.unified_diff(expected.strip().splitlines(), actual.strip().splitlines(), n=0, lineterm="")
    for line in lines:
        if line.startswith(("---", "+++", "@@")):
            continue
        (only_reference if line.startswith("-") else only_replay).append(line[1:])
    parts = [f"`{command}` differs between {reference} and the replay"]
    if only_reference:
        parts.append(
            f"only {reference} has (the script does not make it: add it to the script, or if it is a "
            f"leftover of an experiment, remove it from {reference}):\n" + _listing(only_reference)
        )
    if only_replay:
        parts.append(
            f"only the replay has ({reference} lacks it: do that part of the change on {reference} too and "
            "test it there, or drop it from the script):\n" + _listing(only_replay)
        )
    return "\n".join(parts)


def how_to_run(scripts: Scripts, has_patch: bool, folder: str) -> str:
    """What the user does with the hand-off, in order."""
    steps = []
    if scripts.assume.strip():
        steps.append(f"./{ASSUME_FILE} (it checks what the change relies on: stop if it fails)")
    if scripts.pre.strip():
        steps.append(f"./{PRE_FILE} (before the new files, while the old code still runs)")
    if has_patch:
        steps.append(f"apply {PATCH_FILE} to the project (git apply)")
    steps.append(f"./{SCRIPT_FILE}")
    undo = f" To undo: put the old files back, then ./{ROLLBACK_FILE}." if scripts.rollback.strip() else ""
    return f"to run on the real machine, as root, from {folder}: " + ", then ".join(steps) + "." + undo


def save_handoff(folder: Path, scripts: Scripts, patch: str, report: str) -> Path:
    """Write the hand-off (scripts, patch, report) to ``folder``, replacing what was there: the
    folder always holds the last verified hand-off."""
    _remove(folder)
    folder.mkdir(parents=True)
    for name, text in scripts.files().items():
        (folder / name).write_text(text)
        (folder / name).chmod(EXECUTABLE)
    if patch:
        (folder / PATCH_FILE).write_text(patch)
    (folder / REPORT_FILE).write_text(report + "\n")
    return folder


def withdraw_handoff(folder: Path, withdrawn: Path) -> bool:
    """Move a saved hand-off to ``withdrawn`` (replacing an older one); whether there was one."""
    if not folder.exists():
        return False
    _remove(withdrawn)
    folder.rename(withdrawn)
    return True


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

    async def replay_and_save(scripts: Scripts, replay: Replay, options: Options, goal: str) -> str:
        """Replay; on success (and the review, if any) save the hand-off, on failure withdraw the
        one saved before."""
        try:
            await replay_script(workspace, project, settings, scripts, replay, options)
            report = replay.report()
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
        replay = Replay(_free_fork_name(workspace, replays), checkpoint, code_from)
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


async def review(command: str, goal: str, scripts: Scripts, options: Options, report: str) -> str:
    """Ask the project's reviewer (a command on the host, the prompt on its stdin) what the check
    misses; its answer, as a report section, with what to do about it.

    The reviewer only advises: the hand-off is saved either way, and a reviewer that fails or does
    not answer in time is reported as such.
    """
    prompt = REVIEW_PROMPT.format(
        goal=goal.strip() or "(not given: judge from the scripts and the check)",
        scripts="\n".join(f"--- {name}\n{text}" for name, text in scripts.files().items()),
        check=options.check, load=options.load or "(none of the agent's)",
        load_check=options.load_check or "(none)", report=report,
    )  # fmt: skip
    header = f"--- review (by `{command}`)"
    try:
        process = await asyncio.create_subprocess_shell(
            command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        output, _ = await asyncio.wait_for(process.communicate(prompt.encode()), REVIEW_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        return f"{header}\nthe reviewer did not answer within {REVIEW_TIMEOUT_S:.0f} s"
    except OSError as error:
        return f"{header}\nthe reviewer could not run: {error}"
    answer = output.decode(errors="replace").strip()
    if process.returncode != 0:
        failed = _shorten(answer, STEP_OUTPUT_LIMIT)
        return f"{header}\nthe reviewer failed (exit {process.returncode}): {failed}"
    if answer.splitlines()[:1] == [REVIEW_OK]:
        return f"{header}\n{REVIEW_OK}: no gap found in the check"
    return (
        f"{header}\n{_shorten(answer, OUTPUT_LIMIT)}\nThe hand-off is saved, but the reviewer thinks the "
        "check may miss the above: if a gap is real, make the check (or load_check) catch it, fix the "
        "script if needed, and verify again"
    )


def _slow_hint(replay: Replay, options: Options) -> str:
    """For a slow failed replay of the full data: how to check the logic faster ("" otherwise)."""
    first = next((step for step in replay.steps if step.label == SCRIPT_FILE), None)
    if replay.sampled or options.sample or first is None or first.seconds < SLOW_REPLAY_S:
        return ""
    return (
        f". The script ran {first.seconds:.0f} s here: check the logic on a sample first (sample= "
        "commands that shrink the data, e.g. delete most rows), then replay the full data once it passes"
    )


def _free_fork_name(workspace: Workspace, taken: dict[str, object]) -> str:
    """The first ``replay-N`` that is neither an open fork nor a replay being made."""
    names = (f"{REPLAY_FORK}-{number}" for number in itertools.count(1))
    return next(name for name in names if name not in workspace.forks and name not in taken)


def _load_pid(number: int) -> str:
    """Where the process id of the ``number``-th traffic command is kept on the replay."""
    return f"{STATE_DIR}/load-{number}.pid"


def _listing(lines: list[str]) -> str:
    """Up to ``SHOWN_DIFFERENCES`` lines, indented, with a count of the rest."""
    shown = [f"    {line}" for line in lines[:SHOWN_DIFFERENCES]]
    if len(lines) > SHOWN_DIFFERENCES:
        shown.append(f"    ... and {len(lines) - SHOWN_DIFFERENCES} more")
    return "\n".join(shown)


def _single_file(name: str, content: bytes) -> bytes:
    """A tar archive holding one file."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        member.mode = EXECUTABLE
        tar.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def _file_names(archive: bytes) -> set[str]:
    """Paths of the files and links in a project archive, relative to the project folder."""
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return {str(PurePosixPath(member.name)) for member in tar.getmembers() if not member.isdir()}


def _archive_diff(before: bytes, after: bytes) -> str:
    """The patch from one project archive to another, named by project paths.

    Raises:
        ProjectError: If git fails on the host.
    """
    with tempfile.TemporaryDirectory(prefix="chronobox-diff-") as work_dir:
        work = Path(work_dir)
        _unpack_for_diff(before, work / "host")
        _unpack_for_diff(after, work / "machine")
        return _git_diff(work)


def _remove(path: Path) -> None:
    """Delete a file, link or folder if it exists."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _shorten(text: str, limit: int = OUTPUT_LIMIT) -> str:
    """Long output keeps its start and end; no output reads ``(none)``."""
    text = text.strip()
    if len(text) <= limit:
        return text or "(none)"
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - limit} characters cut ...]\n{text[-half:]}"
