"""Bring a project up in a machine from its ``chronobox.toml``, and get its work back out.

A fresh boot copies the project into the machine, runs ``prepare``, starts the services
(each kept running by a small restart loop, logged to ``/var/log/chronobox/<name>.log``)
and waits until each is ready, then runs ``setup``. The prepared machine is saved as a
**warm snapshot**, keyed by the config, the project's files and the VM disks; while none of
them changes, later sessions restore it in about a second instead of preparing again.

Getting work out: ``project_diff`` is a patch of the project as the machine has it against
the host's copy; ``export`` copies a path from the machine into ``.chronobox/exports``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import io
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from chronobox.config import ProjectConfig, Service
from chronobox.images import TOOLS_DISK, image_disk
from chronobox.service import Method
from chronobox.vm import FirecrackerBackend, Machine, MachineSpec, VMError
from chronobox.workspace import MAIN, Booted, Workspace

LOG_DIR = "/var/log/chronobox"
SERVICES_DIR = "/var/lib/chronobox/services"
EXPORTS_DIR = Path(".chronobox") / "exports"
READY_POLL_S = 0.5
READY_CHECK_TIMEOUT_S = 10.0
RESTART_DELAY_S = 1
LOG_TAIL_LINES = 30
WARM_COMPLETE = "complete"
PARTIAL_SUFFIX = ".partial"
NO_DIFFERENCES = 0
DIFFERENCES = 1
SIDE_FOLDER = re.compile(r"(?<=\s)(a/|b/)?(?:host|machine)/")
DIFF_HEADERS = ("diff --git ", "--- ", "+++ ", "rename from ", "rename to ", "Binary files ")


def cache_dir() -> Path:
    """``$XDG_CACHE_HOME/chronobox`` (``~/.cache/chronobox``)."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "chronobox"


def default_warm_dir() -> Path:
    return cache_dir() / "warm"


def session_root() -> Path:
    """A new folder for one session's VMs and snapshots, outside the project (the backend
    deletes it when it cleans up)."""
    return cache_dir() / "sessions" / uuid.uuid4().hex[:10]


def project_id(config: ProjectConfig) -> str:
    """Names the project by where its chronobox.toml is (two copies of a repo are two projects)."""
    return hashlib.sha256(str(config.file).encode()).hexdigest()[:16]


def up_socket(config: ProjectConfig) -> Path:
    """Where ``chronobox up`` serves the project's machine: ``$XDG_RUNTIME_DIR/chronobox``
    (``/run/user/<uid>``, short and meant for sockets), else the cache folder."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    folder = Path(runtime) / "chronobox" if runtime else cache_dir() / "up"
    return folder / f"{project_id(config)}.sock"


def machine_spec(config: ProjectConfig) -> MachineSpec:
    return MachineSpec(
        image=config.image.name,
        mem_mib=config.mem_mib,
        vcpus=config.vcpus,
        data_mib=config.disk_mib,
        command_timeout=config.command_timeout,
    )


def is_excluded(relative: PurePosixPath, patterns: tuple[str, ...]) -> bool:
    """A pattern without "/" matches any path component; one with "/" a path and what is under it.

    The guest agent applies the same rule when it packs files.
    """
    text = str(relative)
    for pattern in patterns:
        if "/" in pattern:
            prefix = pattern.strip("/")
            if fnmatch.fnmatch(text, prefix) or text.startswith(prefix + "/"):
                return True
        elif any(fnmatch.fnmatch(part, pattern) for part in relative.parts):
            return True
    return False


def project_entries(config: ProjectConfig) -> Iterator[PurePosixPath]:
    """Relative paths of the folders, files and links that go into the machine, sorted."""
    for folder, dirs, files in os.walk(config.path):
        relative_folder = PurePosixPath(Path(folder).relative_to(config.path))
        dirs[:] = sorted(name for name in dirs if not is_excluded(relative_folder / name, config.exclude))
        for name in [*dirs, *sorted(files)]:
            relative = relative_folder / name
            if not is_excluded(relative, config.exclude):
                yield relative


def pack_project(config: ProjectConfig) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for relative in project_entries(config):
            tar.add(config.path / relative, arcname=str(relative), recursive=False)
    return buffer.getvalue()


def warm_key(config: ProjectConfig, vm_dir: Path) -> str:
    """Changes whenever the config, a project file (by size and modification time) or a VM
    disk changes, so a warm snapshot is only reused for the same starting point."""
    digest = hashlib.sha256(config.file.read_bytes())
    for relative in project_entries(config):
        stat = (config.path / relative).lstat()
        digest.update(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    for disk in (vm_dir / TOOLS_DISK, image_disk(vm_dir, config.image.name)):
        stat = disk.stat()
        digest.update(f"{disk}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:24]


def service_command(service: Service, target: str) -> str:
    """Start a service in the background, under a restart loop unless ``restart`` is off.

    The loop lives in its own script, so its command line (``sh <script>``) never contains the
    service's command: ``pkill -f <command>`` stops the service, which then restarts, and not
    the loop that restarts it.
    """
    workdir = service.workdir or target
    once = f"cd {shlex.quote(workdir)} && {service.start}"
    if service.env:
        variables = " ".join(f"{name}={shlex.quote(value)}" for name, value in service.env.items())
        once = f"env {variables} sh -c {shlex.quote(once)}"
    else:
        once = f"sh -c {shlex.quote(once)}"
    loop = once
    if service.restart:
        notice = f'echo "[chronobox] {service.name} exited ($?), restarting in {RESTART_DELAY_S} s"'
        loop = f"while :; do {once}; {notice}; sleep {RESTART_DELAY_S}; done"
    script = f"{SERVICES_DIR}/{service.name}.sh"
    log = f"{LOG_DIR}/{service.name}.log"
    return (
        f"mkdir -p {LOG_DIR} {SERVICES_DIR} && printf '%s\\n' {shlex.quote(loop)} > {script}"
        f" && nohup sh {script} >> {log} 2>&1 &"
    )


class ProjectBoot:
    """Starts a project's main machine: from its warm snapshot if one matches, else fresh.

    A warm start's snapshot is also the workspace's start checkpoint. A newer warm snapshot of
    the same project (after its files or config changed) replaces the older one, so a session
    still running on the older one can no longer roll back to its start.

    Args:
        backend: The backend the machine belongs to.
        config: The project's config.
        warm_dir: Where warm snapshots are kept; ``None`` disables them.
    """

    def __init__(self, backend: FirecrackerBackend, config: ProjectConfig, warm_dir: Path | None) -> None:
        self.backend = backend
        self.config = config
        self.warm_dir = warm_dir
        self.was_warm = False
        self.seconds = 0.0

    async def __call__(self) -> Booted:
        started = time.monotonic()
        warm = self._warm_path()
        if warm is not None and (warm / WARM_COMPLETE).exists():
            snapshot = self.backend.borrow(warm)
            machine = await self.backend.restore(snapshot)
            self.was_warm = True
            self.seconds = time.monotonic() - started
            return Booted(machine, snapshot)
        machine = await self.backend.start(machine_spec(self.config))
        try:
            await self._prepare(machine)
        except VMError:
            await self.backend.stop(machine)
            raise
        if warm is not None:
            await self._save_warm(machine, warm)
        self.seconds = time.monotonic() - started
        return Booted(machine)

    async def _prepare(self, machine: Machine) -> None:
        config = self.config
        await machine.put(config.target, await asyncio.to_thread(pack_project, config))
        await _check(machine, f"cd {shlex.quote(config.target)}", "enter the project")
        for command in config.prepare:
            await _check(machine, command, "prepare")
        for service in config.services:
            await _check(machine, service_command(service, config.target), f"start {service.name}")
            await _wait_ready(machine, service)
        for command in config.setup:
            await _check(machine, command, "setup")
        await _check(machine, f"cd {shlex.quote(config.target)}", "enter the project")

    def _warm_path(self) -> Path | None:
        if self.warm_dir is None:
            return None
        return self.warm_dir / project_id(self.config) / warm_key(self.config, self.backend.vm_dir)

    async def _save_warm(self, machine: Machine, warm: Path) -> None:
        # Write next to the final place, then rename: a crash never leaves a half snapshot
        # that looks complete. Older snapshots of the project are dropped.
        partial = warm.with_name(warm.name + PARTIAL_SUFFIX)
        shutil.rmtree(partial, ignore_errors=True)
        await self.backend.save(machine, partial)
        (partial / WARM_COMPLETE).touch()
        for old in warm.parent.iterdir():
            if old != partial:
                shutil.rmtree(old, ignore_errors=True)
        partial.rename(warm)


async def project_diff(machine: Machine, config: ProjectConfig) -> str:
    """A unified diff from the host's project to the project as the machine has it now.

    Raises:
        VMError: If the project folder cannot be read in the machine.
        RuntimeError: If ``git`` (used for the diff) is missing on the host.
    """
    archive = await machine.get(config.target, exclude=config.exclude)
    with tempfile.TemporaryDirectory(prefix="chronobox-diff-") as work_dir:
        work = Path(work_dir)
        _unpack(archive, work / "machine")
        _unpack(await asyncio.to_thread(pack_project, config), work / "host")
        return await asyncio.to_thread(_git_diff, work)


async def export(machine: Machine, config: ProjectConfig, path: str, where: str) -> Path:
    """Copy ``path`` from the machine into ``<project>/.chronobox/exports/<where>/``.

    Raises:
        VMError: If the path does not exist in the machine.
    """
    archive = await machine.get(path)
    destination = config.path / EXPORTS_DIR / where / path.strip("/")
    if destination.is_dir():
        shutil.rmtree(destination)
    elif destination.exists():
        destination.unlink()
    # A file comes under its own name, so it goes into its parent folder.
    _unpack(archive, destination if _holds_a_folder(archive) else destination.parent)
    return destination


def project_tools(workspace: Workspace, config: ProjectConfig) -> dict[str, Method]:
    """The project's tool methods (``diff``, ``export``), for ``WorkspaceService.extra_methods``."""

    async def diff(where: str = MAIN) -> str:
        try:
            patch = await project_diff(workspace.machine(where), config)
        except (VMError, RuntimeError) as error:
            return f"error: {error}"
        return patch or f"no changes: {config.target} on {where} matches the local project"

    async def export_path(path: str, where: str = MAIN) -> str:
        try:
            destination = await export(workspace.machine(where), config, path, where)
        except VMError as error:
            return f"error: {error}"
        return f"copied {path} from {where} to {destination}"

    return {"diff": diff, "export": export_path}


def project_note(config: ProjectConfig) -> str:
    """What the agent is told about the project, next to the profile's instructions."""
    services = ", ".join(service.name for service in config.services) or "none"
    return (
        f"The project {config.path.name} is copied at {config.target} in the machine (commands start "
        f"there). Its services ({services}) are running, restarted if they exit; logs in {LOG_DIR}. "
        "`diff` shows what changed in the project on the main machine or a fork, as a patch for the "
        "local copy; `export` copies files out of the machine."
    )


async def _check(machine: Machine, command: str, step: str) -> str:
    code, output = await machine.run(command)
    if code != 0:
        raise VMError(f"{step} failed ({code}): {command}\n{output[-1500:]}")
    return output


async def _wait_ready(machine: Machine, service: Service) -> None:
    if not service.ready:
        return
    deadline = time.monotonic() + service.ready_timeout
    while time.monotonic() < deadline:
        code, _ = await machine.run(service.ready, READY_CHECK_TIMEOUT_S, isolated=True)
        if code == 0:
            return
        await asyncio.sleep(READY_POLL_S)
    _, log = await machine.run(f"tail -n {LOG_TAIL_LINES} {LOG_DIR}/{service.name}.log", isolated=True)
    waited = f"{service.ready_timeout:g}s"
    raise VMError(f"service {service.name} not ready after {waited}: {service.ready}\n{log}")


def _unpack(archive: bytes, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")


def _holds_a_folder(archive: bytes) -> bool:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return any(member.name == "." or member.name.startswith("./") for member in tar.getmembers())


def _git_diff(work: Path) -> str:
    if shutil.which("git") is None:
        raise RuntimeError("git is needed on the host for diffs")
    done = subprocess.run(
        ["git", "diff", "--no-index", "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "host", "machine"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    if done.returncode not in (NO_DIFFERENCES, DIFFERENCES):
        raise RuntimeError(f"git diff failed: {done.stderr.strip()}")
    return "".join(_project_paths(line) for line in done.stdout.splitlines(keepends=True))


def _project_paths(line: str) -> str:
    """git names files by the folder they are in (``a/host/x``, ``b/machine/x``, and for an added
    or deleted file the same folder on both sides); name them by project path instead."""
    if not line.startswith(DIFF_HEADERS):
        return line
    return SIDE_FOLDER.sub(r"\1", line)
