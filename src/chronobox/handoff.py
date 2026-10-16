"""Script hand-off: check a change script by replaying it on a fresh fork, then save it for the user.

Some changes are actions, not file edits: a migration or a backfill, restarting services in order,
a setting outside the project. A patch cannot carry them, so the agent writes them as a shell
script. ``verify_script`` replays that script the way the user will run it on the real machine:
on a new fork of a checkpoint (by default ``start``, the machine as it was before the agent's work),
after deploying the project's changed files there from main or a fork. When the script, and an
optional check after it, succeed, the project patch, the script and the replay's report are saved
together in ``.chronobox/exports/handoff/``.
"""

from __future__ import annotations

import io
import itertools
import shlex
import shutil
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from chronobox.project import EXPORTS_DIR, ProjectBoot, ProjectError, project_diff
from chronobox.service import Method
from chronobox.vm import Machine, VMError
from chronobox.workspace import MAIN, START, Workspace, WorkspaceError

HANDOFF_DIR = EXPORTS_DIR / "handoff"
SCRIPT_FILE = "apply.sh"
PATCH_FILE = "changes.patch"
REPORT_FILE = "replay.txt"
STATE_DIR = "/root/.chronobox"
REPLAY_FORK = "replay"
SHEBANG = "#!"
EXECUTABLE = 0o755
OUTPUT_LIMIT = 4000


@dataclass(frozen=True)
class Replay:
    """What happened when a script was replayed on a fork.

    Attributes:
        fork: The fork the script ran on; it stays open for inspection.
        checkpoint: The checkpoint the fork started from.
        code_from: The machine whose project files were deployed before the script.
        code: The script's exit code.
        output: The script's output.
        check: The check command, empty if none.
        check_code: The check's exit code; ``None`` without a check.
        check_output: The check's output.
        seconds: How long the script ran.
    """

    fork: str
    checkpoint: str
    code_from: str
    code: int
    output: str
    check: str
    check_code: int | None
    check_output: str
    seconds: float

    @property
    def succeeded(self) -> bool:
        """The script exited 0, and so did the check if there was one."""
        return self.code == 0 and self.check_code in (None, 0)

    def report(self) -> str:
        """The replay as text, for the agent and for ``replay.txt``."""
        lines = [
            f"replayed the script on fork {self.fork!r} (from checkpoint {self.checkpoint!r}, project "
            f"files from {self.code_from}): exit {self.code} after {self.seconds:.1f} s",
            "--- script output",
            _shorten(self.output) or "(none)",
        ]
        if self.check:
            header = f"--- check `{self.check}`: exit {self.check_code}"
            lines += [header, _shorten(self.check_output) or "(none)"]
        return "\n".join(lines)


async def replay_script(
    workspace: Workspace, boot: ProjectBoot, script: str, *, check: str, code_from: str, checkpoint: str
) -> Replay:
    """Run ``script`` on a new fork of ``checkpoint`` after deploying ``code_from``'s project files.

    Raises:
        WorkspaceError: If the checkpoint or ``code_from`` does not exist, or forks are not available.
        VMError: If the files cannot be moved or a command cannot be sent.
    """
    source = workspace.machine(code_from)
    name = _free_fork_name(workspace)
    await workspace.fork(name, checkpoint)
    machine = workspace.machine(name)
    await deploy_project(source, machine, boot)
    await machine.put(STATE_DIR, _single_file(SCRIPT_FILE, script.encode()))
    command = f"cd {shlex.quote(boot.config.target)} && {_script_command(script)}"
    started = time.monotonic()
    code, output = await machine.run(command)
    seconds = time.monotonic() - started
    workspace.forks[name].commands.append((f"verify_script: {SCRIPT_FILE}", code))
    check_code, check_output = None, ""
    if check and code == 0:
        check_code, check_output = await machine.run(f"cd {shlex.quote(boot.config.target)} && {check}")
        workspace.forks[name].commands.append((check, check_code))
    return Replay(name, checkpoint, code_from, code, output, check, check_code, check_output, seconds)


async def deploy_project(source: Machine, target: Machine, boot: ProjectBoot) -> None:
    """Make the project folder on ``target`` match ``source``'s: copy its files over, and delete the
    ones it deleted since the project was copied in (excluded paths are left alone on both).

    Raises:
        VMError: If the files cannot be read or written.
    """
    config = boot.config
    archive = await source.get(config.target, exclude=config.exclude)
    deleted = _file_names(boot.baseline.read_bytes()) - _file_names(archive)
    if deleted:
        names = " ".join(shlex.quote(name) for name in sorted(deleted))
        code, output = await target.run(f"cd {shlex.quote(config.target)} && rm -f -- {names}")
        if code != 0:
            raise VMError(f"could not delete files on the replay fork: {output[-500:]}")
    await target.put(config.target, archive)


def save_handoff(project: Path, script: str, patch: str, report: str) -> Path:
    """Write the hand-off (script, patch, report) to ``.chronobox/exports/handoff/``, replacing the
    previous one: the folder always holds the last verified hand-off."""
    folder = project / HANDOFF_DIR
    if folder.is_symlink():
        folder.unlink()
    elif folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    (folder / SCRIPT_FILE).write_text(script if script.endswith("\n") else script + "\n")
    (folder / SCRIPT_FILE).chmod(EXECUTABLE)
    (folder / PATCH_FILE).write_text(patch)
    (folder / REPORT_FILE).write_text(report + "\n")
    return folder


def handoff_tools(workspace: Workspace, boot: ProjectBoot) -> dict[str, Method]:
    """The ``verify_script`` tool method, for ``WorkspaceService.extra_methods``."""

    async def verify_script(
        script: str, check: str = "", code_from: str = MAIN, checkpoint: str = START
    ) -> str:
        """Replay ``script``; on success save the hand-off. Failures come back as text for the agent."""
        if not script.strip():
            return "error: the script is empty"
        try:
            replay = await replay_script(
                workspace, boot, script, check=check, code_from=code_from, checkpoint=checkpoint
            )
            report = replay.report()
            if not replay.succeeded:
                return f"{report}\nnot saved: fix the script (or the project files) and verify it again."
            baseline = boot.baseline.read_bytes()
            patch = await project_diff(workspace.machine(code_from), boot.config, baseline)
            folder = save_handoff(boot.config.path, script, patch, report)
        except (WorkspaceError, VMError, ProjectError, tarfile.TarError, OSError) as error:
            return f"error: {error}"
        files = f"{SCRIPT_FILE}, {PATCH_FILE}" if patch else f"{SCRIPT_FILE} (no project changes)"
        return f"{report}\nhand-off saved in {folder}: {files}, {REPORT_FILE}"

    return {"verify_script": verify_script}


def _free_fork_name(workspace: Workspace) -> str:
    """The first ``replay-N`` that is not an open fork."""
    names = (f"{REPLAY_FORK}-{number}" for number in itertools.count(1))
    return next(name for name in names if name not in workspace.forks)


def _script_command(script: str) -> str:
    """How to run the saved script: by its own interpreter if it has a ``#!`` line, else with sh."""
    path = f"{STATE_DIR}/{SCRIPT_FILE}"
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


def _shorten(text: str) -> str:
    """Long output keeps its start and end."""
    text = text.strip()
    if len(text) <= OUTPUT_LIMIT:
        return text
    half = OUTPUT_LIMIT // 2
    return f"{text[:half]}\n[... {len(text) - OUTPUT_LIMIT} characters cut ...]\n{text[-half:]}"
