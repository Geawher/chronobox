"""One replay of a hand-off on a fresh fork: prepare, traffic, the scripts, the crash copies, then
every check (the steps are in the package docstring)."""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import time
from dataclasses import dataclass

from chronobox.config import HandoffSettings
from chronobox.handoff.files import LOCAL, Project, file_names, single_file
from chronobox.handoff.report import STEP_OUTPUT_LIMIT, Replay, Step, difference, shorten
from chronobox.handoff.running import CHECK_TIMEOUT_S, TIMED_OUT, Tick, kill_script, run_detached
from chronobox.handoff.scripts import (
    ASSUME_FILE,
    PRE_FILE,
    ROLLBACK_FILE,
    SCRIPT_FILE,
    STATE_DIR,
    Options,
    Scripts,
    bash_features,
)
from chronobox.vm import Machine, Snapshot, VMError
from chronobox.workspace import MAIN, Fork, Workspace

LOAD_LOG_DIR = "/var/log/chronobox"
REPLAY_FORK = "replay"
CRASH_SUFFIX = "-crash-"
LOAD_WARMUP_S = 2.0
READY_GRACE_S = 30.0
READY_POLL_S = 1.0


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


NO_TRAFFIC_NOTE = (
    "nothing checked what users saw while the script ran (no load_check): if the change can lose or "
    "block writes (restarts, failovers, migrations under traffic), pass load_check (and load, unless "
    "the machine's own clients already write); a check after the script cannot see what happened during it"
)


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


class _Stopped(Exception):
    """A replay step failed in a way that makes the later steps meaningless."""


@dataclass(frozen=True)
class Listener:
    """A process listening on a TCP port."""

    port: int
    pid: int
    cwd: str
    command: str


@dataclass(frozen=True)
class CrashPoint:
    """The replay as it was ``at`` seconds into the script's first run."""

    at: float
    snapshot: Snapshot


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
            pass
        finally:
            with contextlib.suppress(VMError, OSError):  # the fork is gone or broken: so is its traffic
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
            for result in await asyncio.gather(*crashes, return_exceptions=True):
                if isinstance(result, BaseException):
                    self.replay.notes.append(f"a crash test could not finish: {result!r}")
                else:
                    self._merge(*result)

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
        name = f"{self.replay.fork}{CRASH_SUFFIX}{number}"
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
            self.workspace.forks.pop(name, None)
            try:
                await backend.stop(machine)
            except (VMError, OSError) as error:
                crash.notes.append(f"the copy could not be stopped: {error}")
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
            await self.machine.put(STATE_DIR, single_file(name, text.encode()))

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
            (expected_code, expected), (code, actual) = await asyncio.gather(
                reference.run(wrapped, CHECK_TIMEOUT_S, isolated=True),
                self.machine.run(wrapped, CHECK_TIMEOUT_S, isolated=True),
            )
            if expected_code != 0 or code != 0:
                self.record(Step(f"[handoff] compare `{command}`", code or expected_code, "failed", 0.0))
                failed = f"{self.reference} (exit {expected_code})" if expected_code else ""
                failed += (" and " if failed and code else "") + (f"the replay (exit {code})" if code else "")
                self.problem(f"the project's compare `{command}` failed on {failed}, so it shows nothing: "
                             f"{shorten(actual if code else expected, STEP_OUTPUT_LIMIT)}")  # fmt: skip
                continue
            same = expected.strip() == actual.strip()
            self.record(Step(f"[handoff] compare `{command}`", code, "same" if same else "different", 0.0))
            if not same and self.replay.sampled:
                # Only the replay's data was shrunk: a compare of the data differs anyway.
                self.replay.notes.append(
                    f"[handoff] compare `{command}` differs from {self.reference}, but the replay ran on a "
                    "sample of the data: replay the full data to compare"
                )
            elif not same:
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
            added = file_names(current) - file_names(start)
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
        # Not isolated (an isolated command does not see the machine's listeners): in a subshell, so
        # its variables do not stay in the agent's shell on main.
        code, output = await machine.run(f"(\n{LISTENERS}\n)", CHECK_TIMEOUT_S)
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


def _load_pid(number: int) -> str:
    """Where the process id of the ``number``-th traffic command is kept on the replay."""
    return f"{STATE_DIR}/load-{number}.pid"
