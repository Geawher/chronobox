"""Script hand-off: check a change script by replaying it on a fresh fork, then save it for the user.

Some changes are actions, not file edits: a migration or a backfill, restarting services in order,
a setting outside the project. A patch cannot carry them, so the agent writes them as a shell
script. ``verify_script`` replays that script the way the user will run it on the real machine:
on a new fork of a checkpoint (by default ``start``, the machine as it was before the agent's work),
after deploying the project's changed files there (from main, a fork, or the local folder).

A replay passes only when all of these hold:

- the script succeeds, and again when run a second time (deployments get retried);
- the agent's own ``check`` succeeds, then the project's ``[handoff]`` checks;
- every service still answers its ``ready`` command;
- the project's traffic (``[handoff] load``), run during the replay, saw no problem (``load_check``);
- the ``[handoff] compare`` commands print the same on the agent's machine and on the replay;
- a project process the agent restarted on its machine was restarted by the script too, so the
  real machine does not keep running the old code.

Then the project patch, the scripts and the replay's report are saved together in
``.chronobox/exports/handoff/``. A failed replay withdraws the hand-off saved before (moved to
``handoff-withdrawn``): it no longer matches what the agent is working on.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import itertools
import shlex
import shutil
import tarfile
import tempfile
import time
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
from chronobox.vm import Machine, VMError
from chronobox.workspace import MAIN, START, Workspace, WorkspaceError

HANDOFF_DIR = EXPORTS_DIR / "handoff"
WITHDRAWN_DIR = EXPORTS_DIR / "handoff-withdrawn"
SCRIPT_FILE = "apply.sh"
PRE_FILE = "pre.sh"
ROLLBACK_FILE = "rollback.sh"
PATCH_FILE = "changes.patch"
REPORT_FILE = "replay.txt"
LOCAL = "local"  # code_from: the project folder on the host, as the agent edited it
STATE_DIR = "/root/.chronobox"
LOAD_PID = f"{STATE_DIR}/load.pid"
LOAD_LOG = "/var/log/chronobox/handoff-load.log"
REPLAY_FORK = "replay"
SHEBANG = "#!"
EXECUTABLE = 0o755
OUTPUT_LIMIT = 4000
STEP_OUTPUT_LIMIT = 1500
DIFF_LINES = 40
DEFAULT_SCRIPT_TIMEOUT_S = 900.0
MAX_SCRIPT_TIMEOUT_S = 3600.0
CHECK_TIMEOUT_S = 300.0
LOAD_WARMUP_S = 2.0
READY_GRACE_S = 30.0
READY_POLL_S = 1.0
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
STOP_LOAD = (
    f"[ -f {LOAD_PID} ] && p=$(cat {LOAD_PID}) && rm -f {LOAD_PID} && "
    "{ kill -TERM -- -$p 2>/dev/null || kill -TERM $p; sleep 1; kill -KILL -- -$p 2>/dev/null; true; }"
)


class _Stopped(Exception):
    """A replay step failed in a way that makes the later steps meaningless."""


@dataclass(frozen=True)
class Scripts:
    """What the agent hands over.

    Attributes:
        apply: Run right after the project's new files are deployed.
        pre: Run before they are deployed, while the old code still runs (expand, then contract).
        rollback: Undoes ``apply`` once the old files are back; replayed too.
    """

    apply: str
    pre: str = ""
    rollback: str = ""

    def files(self) -> dict[str, str]:
        """The non-empty scripts by file name."""
        named = {SCRIPT_FILE: self.apply, PRE_FILE: self.pre, ROLLBACK_FILE: self.rollback}
        return {name: text for name, text in named.items() if text.strip()}


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


@dataclass
class Replay:
    """What happened when a hand-off was replayed on a fork.

    Attributes:
        fork: The fork it ran on; it stays open for inspection.
        checkpoint: The checkpoint the fork started from.
        code_from: Where the deployed project files came from (a machine, or ``local``).
        steps: Every command run, in order.
        problems: Why the hand-off cannot be trusted; empty when it passed.
    """

    fork: str
    checkpoint: str
    code_from: str
    steps: list[Step] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """Every step and check passed."""
        return not self.problems

    def report(self) -> str:
        """The replay as text, for the agent and for ``replay.txt``."""
        verdict = "passed" if self.succeeded else "FAILED"
        lines = [
            f"replayed on fork {self.fork!r} (from checkpoint {self.checkpoint!r}, project files "
            f"from {self.code_from}): {verdict}"
        ]
        for step in self.steps:
            limit = OUTPUT_LIMIT if step.label.startswith(SCRIPT_FILE) else STEP_OUTPUT_LIMIT
            lines += [
                f"--- {step.label}: exit {step.code} after {step.seconds:.1f} s",
                _shorten(step.output, limit),
            ]
        if self.problems:
            lines += ["--- problems", *(f"- {problem}" for problem in self.problems)]
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
    """

    def __init__(
        self,
        workspace: Workspace,
        project: Project | None,
        settings: HandoffSettings,
        replay: Replay,
        reference: str,
    ) -> None:
        """Set up the run."""
        self.workspace = workspace
        self.project = project
        self.settings = settings
        self.replay = replay
        self.machine = workspace.machine(replay.fork)
        self.reference = reference
        self.folder = project.config.target if project else "/root"

    async def __call__(self, scripts: Scripts, *, check: str, twice: bool, timeout: float) -> None:
        """Replay the hand-off: prepare, traffic, scripts, then every check."""
        try:
            await self._run(scripts, check=check, twice=twice, timeout=timeout)
        except _Stopped:
            await self._stop_load()

    async def step(self, label: str, command: str, timeout: float = CHECK_TIMEOUT_S) -> Step:
        """Run ``command`` in the project folder; record it in the report and the fork's history."""
        started = time.monotonic()
        code, output = await self.machine.run(f"cd {shlex.quote(self.folder)} && {command}", timeout)
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

    async def _run(self, scripts: Scripts, *, check: str, twice: bool, timeout: float) -> None:
        """The replay's steps in order.

        Raises:
            _Stopped: If a step the rest depends on fails.
        """
        for command in self.settings.prepare:
            await self.required(
                "[handoff] prepare", command, f"the project's handoff.prepare failed: {command}"
            )
        before = await self._listeners(self.machine)
        await self._start_load()
        await self._put_scripts(scripts)
        if scripts.pre.strip():
            failed = f"{PRE_FILE} failed (it runs before the new files are deployed, with the old code)"
            await self.required(PRE_FILE, _script_command(scripts.pre, PRE_FILE), failed, timeout)
        await self._deploy()
        apply = _script_command(scripts.apply, SCRIPT_FILE)
        await self.required(SCRIPT_FILE, apply, f"{SCRIPT_FILE} failed", timeout)
        if twice:
            again = f"{SCRIPT_FILE} failed when run a second time: make it safe to run again"
            await self.required(f"{SCRIPT_FILE} (second run)", apply, again, timeout)
        await self._checks(check)
        await self._stop_load()
        if (
            self.settings.load_check
            and (await self.step("[handoff] load_check", self.settings.load_check)).code
        ):
            self.problem("the project's traffic saw a problem during the replay (handoff.load_check failed)")
        await self._compare()
        if self.project is not None:
            await self._restarts(before)
        if scripts.rollback.strip() and self.replay.succeeded:
            await self._rollback(scripts.rollback, timeout)

    async def _checks(self, check: str) -> None:
        """The agent's check, the project's checks, then every service's ready command."""
        if (await self.step(f"check `{check}`", check)).code != 0:
            self.problem("your check failed")
        for command in self.settings.checks:
            if (await self.step(f"[handoff] check `{command}`", command)).code != 0:
                self.problem(f"the project's check failed: {command}")
        await self._services_ready("after the script")

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

    async def _start_load(self) -> None:
        """Start the project's traffic in its own process group, so it can be stopped whole.

        Raises:
            _Stopped: If it cannot start.
        """
        if not self.settings.load:
            return
        load = shlex.quote(self.settings.load)
        command = (
            f"mkdir -p {STATE_DIR} && (command -v setsid >/dev/null && exec setsid sh -c {load} "
            f"|| exec sh -c {load}) > {LOAD_LOG} 2>&1 < /dev/null & echo $! > {LOAD_PID}"
        )
        await self.required("[handoff] load (started)", command, "the project's handoff.load could not start")
        await asyncio.sleep(LOAD_WARMUP_S)

    async def _stop_load(self) -> None:
        """Stop the traffic ``_start_load`` started (no-op without one)."""
        if self.settings.load:
            await self.machine.run(STOP_LOAD, CHECK_TIMEOUT_S)

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
            if same:
                continue
            lines = difflib.unified_diff(
                expected.strip().splitlines(),
                actual.strip().splitlines(),
                self.reference,
                "replay",
                lineterm="",
                n=1,
            )
            shown = "\n".join(itertools.islice(lines, DIFF_LINES))
            self.problem(
                f"`{command}` differs between {self.reference} and the replay: the script does not "
                f"reproduce what you tested there (or {self.reference} has leftovers of experiments)\n{shown}"
            )

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

    async def _rollback(self, rollback: str, timeout: float) -> None:
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
        if (await self.step(ROLLBACK_FILE, _script_command(rollback, ROLLBACK_FILE), timeout)).code != 0:
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
    *,
    check: str,
    code_from: str = MAIN,
    checkpoint: str = START,
    twice: bool = True,
    timeout: float = DEFAULT_SCRIPT_TIMEOUT_S,
) -> Replay:
    """Replay a hand-off on a new fork of ``checkpoint`` (the steps are in the module docstring).

    Raises:
        WorkspaceError: If the checkpoint or ``code_from`` does not exist, or forks are not available.
        VMError: If files cannot be moved or a command cannot be sent.
    """
    if code_from != LOCAL:
        workspace.machine(code_from)  # an unknown machine fails before a fork is made
    name = _free_fork_name(workspace)
    await workspace.fork(name, checkpoint)
    replay = Replay(name, checkpoint, code_from)
    reference = MAIN if code_from == LOCAL else code_from
    await ReplayRun(workspace, project, settings, replay, reference)(
        scripts, check=check, twice=twice, timeout=timeout
    )
    return replay


def save_handoff(folder: Path, scripts: Scripts, patch: str, report: str) -> Path:
    """Write the hand-off (scripts, patch, report) to ``folder``, replacing what was there: the
    folder always holds the last verified hand-off."""
    _remove(folder)
    folder.mkdir(parents=True)
    for name, text in scripts.files().items():
        (folder / name).write_text(text if text.endswith("\n") else text + "\n")
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
    """The ``verify_script`` tool method, for ``WorkspaceService.extra_methods``.

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

    async def verify_script(
        script: str,
        check: str,
        pre: str = "",
        rollback: str = "",
        twice: bool = True,
        code_from: str = MAIN,
        checkpoint: str = START,
        timeout: float = DEFAULT_SCRIPT_TIMEOUT_S,
    ) -> str:
        """Replay the hand-off; on success save it, on failure withdraw the one saved before."""
        if not script.strip():
            return "error: the script is empty"
        if not check.strip():
            return (
                "error: a check is required: a command that fails unless the change works as the user "
                "asked (use it as users would, including new writes), run after the script"
            )
        if code_from == LOCAL and project is None:
            return "error: code_from='local' needs a project"
        scripts = Scripts(script, pre, rollback)
        timeout = min(max(float(timeout), 1.0), MAX_SCRIPT_TIMEOUT_S)
        try:
            replay = await replay_script(
                workspace, project, settings, scripts,
                check=check, code_from=code_from, checkpoint=checkpoint, twice=twice, timeout=timeout,
            )  # fmt: skip
            report = replay.report()
            if not replay.succeeded:
                gone = withdraw_handoff(folder, withdrawn)
                note = f" The hand-off saved before was withdrawn (moved to {withdrawn})." if gone else ""
                return (
                    f"{report}\nnot saved: fix the script (or the project files) and verify it again.{note}"
                )
            patch = await project.patch(workspace, code_from) if project is not None else ""
            saved = save_handoff(folder, scripts, patch, report)
        except (WorkspaceError, VMError, ProjectError, tarfile.TarError, OSError) as error:
            return f"error: {error}"
        files = ", ".join(sorted(path.name for path in saved.iterdir()))
        unchanged = " (no project changes)" if project is not None and not patch else ""
        return f"{report}\nhand-off saved in {saved}: {files}{unchanged}"

    return {"verify_script": verify_script}


def _free_fork_name(workspace: Workspace) -> str:
    """The first ``replay-N`` that is not an open fork."""
    names = (f"{REPLAY_FORK}-{number}" for number in itertools.count(1))
    return next(name for name in names if name not in workspace.forks)


def _script_command(script: str, name: str = SCRIPT_FILE) -> str:
    """How to run a saved script: by its own interpreter if it has a ``#!`` line, else with sh."""
    path = f"{STATE_DIR}/{name}"
    if script.startswith(SHEBANG):
        return f"chmod +x {path} && {path}"
    return f"sh {path}"


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
