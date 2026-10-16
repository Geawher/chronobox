"""Bring a project up in a machine from its ``chronobox.toml``, and get its work back out.

A fresh boot copies the project into the machine, runs ``prepare``, starts the services
(each kept running by a small restart loop, logged to ``/var/log/chronobox/<name>.log``)
and waits until each is ready, then runs ``setup`` and starts the ``after_setup`` services. The
prepared machine is saved as a **warm snapshot**, keyed by the config, the project's files and
the VM disks; while none of them changes, later sessions restore it in about 0.1 s instead of
preparing again.

Getting work out: ``project_diff`` is a patch from the project as it was when the machine was
ready (kept with the machine) to the project as the machine has it now; ``export`` copies a path from
the machine into ``.chronobox/exports``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import io
import logging
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
from chronobox.vm import FirecrackerBackend, Machine, MachineSpec, VMError, try_lock_saved
from chronobox.workspace import MAIN, Booted, Workspace

log = logging.getLogger(__name__)

LOG_DIR = "/var/log/chronobox"
SERVICES_DIR = "/var/lib/chronobox/services"
EXPORTS_DIR = Path(".chronobox") / "exports"
READY_POLL_S = 0.5
READY_CHECK_TIMEOUT_S = 10.0
RESTART_DELAY_S = 1
LOG_TAIL_LINES = 30
WARM_COMPLETE = "complete"
PARTIAL_SUFFIX = ".partial"
STALE_PARTIAL_S = 3600  # a half-written warm snapshot this old was left by a crash
# Bump when what a prepared machine holds changes (e.g. how services are started), so warm
# snapshots made by older code are not reused.
WARM_FORMAT = "4"
BASELINE = "project.tar"  # the project once the machine was ready: what diffs compare against
COPIED = "copied.tar"  # the local files as copied in: what `sync` finds local edits against
NO_DIFFERENCES = 0
DIFFERENCES = 1
DIFF_COMMAND = (
    "git", "-c", "core.quotepath=off", "diff", "--no-index", "--no-ext-diff", "--no-color",
    "--binary", "--src-prefix=a/", "--dst-prefix=b/", "host", "machine",
)  # fmt: skip
# Header lines name files by the scratch folder they are in: a/host/..., b/machine/..., or
# (added or deleted files, renames) the same folder on both sides.
SIDE_FOLDER = re.compile(r'(?<=[\s"])([ab]/)(?:host|machine)/')
RENAME_FOLDER = re.compile(r"^(rename (?:from|to) )(?:host|machine)/")
DIFF_HEADERS = ("diff --git ", "--- ", "+++ ", "rename from ", "rename to ", "Binary files ")
SPECIAL_FILES = (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE)
SHOWN_NAMES = 10


class ProjectError(Exception):
    """A diff or export that cannot be done as asked; the message is for the agent."""


def cache_dir() -> Path:
    """``$XDG_CACHE_HOME/chronobox`` (``~/.cache/chronobox``)."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "chronobox"


def default_warm_dir() -> Path:
    """``<cache>/warm``: each warm snapshot is in ``<project id>/<warm key>`` under it."""
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
    """The machine a project runs on: its image and the ``[machine]`` sizes."""
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
    """A tar archive of the project's files as they go into the machine (excludes left out)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for relative in project_entries(config):
            tar.add(config.path / relative, arcname=str(relative), recursive=False)
    return buffer.getvalue()


def warm_key(config: ProjectConfig, vm_dir: Path) -> str:
    """Changes whenever the config, a project file (by size and modification time) or a VM
    file changes (Firecracker, kernel, tools drive, image), or the warm format does, so a warm
    snapshot is only reused for the same starting point and a VM that can load it."""
    digest = hashlib.sha256(f"{WARM_FORMAT}\n".encode() + config.file.read_bytes())
    for relative in project_entries(config):
        stat = (config.path / relative).lstat()
        digest.update(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    vm_files = (vm_dir / "firecracker", vm_dir / "vmlinux", vm_dir / TOOLS_DISK)
    for vm_file in (*vm_files, image_disk(vm_dir, config.image.name)):
        stat = vm_file.stat()
        digest.update(f"{vm_file}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
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

    A warm start's snapshot is also the workspace's start checkpoint; while the session runs,
    it holds that snapshot in use, so a newer warm snapshot of the project (after its files or
    config changed) never deletes it.

    Args:
        backend: The backend the machine belongs to.
        config: The project's config.
        warm_dir: Where warm snapshots are kept; ``None`` disables them.
    """

    def __init__(self, backend: FirecrackerBackend, config: ProjectConfig, warm_dir: Path | None) -> None:
        """Set up the boot; nothing starts until it is called."""
        self.backend = backend
        self.config = config
        self.warm_dir = warm_dir
        self.was_warm = False
        self.seconds = 0.0
        self.baseline = backend.root / BASELINE
        self.copied = backend.root / COPIED

    async def __call__(self) -> Booted:
        """Start the main machine, warm if a matching warm snapshot exists; ``seconds`` records
        how long it took."""
        started = time.monotonic()
        warm = self._warm_path()
        booted = await self._from_warm(warm) if warm is not None else None
        if booted is None:
            booted = await self._fresh(warm)
        self.seconds = time.monotonic() - started
        return booted

    async def _from_warm(self, warm: Path) -> Booted | None:
        """Restore the warm snapshot at ``warm``, or ``None`` to boot fresh instead.

        The snapshot stays borrowed (held in use) as the start checkpoint. One this VM cannot load is
        deleted, so the next session does not fail on it too.
        """
        if not (warm / WARM_COMPLETE).exists():
            return None
        try:
            snapshot = self.backend.borrow(warm)
        except VMError as error:  # deleted since, or broken: boot fresh
            log.warning("not using warm start %s: %s", warm, error)
            return None
        try:
            machine = await self.backend.restore(snapshot)
        except (VMError, OSError) as error:
            # A snapshot this VM cannot load would fail every session: drop it, boot fresh.
            log.warning("warm start %s failed, booting fresh: %s", warm, error)
            await self.backend.release(snapshot)
            shutil.rmtree(warm, ignore_errors=True)
            return None
        self.was_warm = True
        self.baseline = warm / BASELINE
        self.copied = warm / COPIED
        return Booted(machine, snapshot)

    async def _fresh(self, warm: Path | None) -> Booted:
        """Boot a new machine and prepare the project in it, then save it as ``warm`` if given.

        Raises:
            VMError: If the machine does not boot or a prepare, service or setup step fails (the
                machine is stopped).
        """
        machine = await self.backend.start(machine_spec(self.config))
        try:
            await self._prepare(machine)
        except VMError:
            await self.backend.stop(machine)
            raise
        if warm is not None:
            await self._save_warm(machine, warm)
        return Booted(machine)

    async def _prepare(self, machine: Machine) -> None:
        """Copy the project in, run ``prepare``, start each service and wait until it is ready, run
        ``setup``, then start the ``after_setup`` services the same way.

        The project folder as it is then becomes the diff baseline, so files the project's own
        steps write there (fixtures copied into a media folder, build output) are not reported as
        changes.

        Raises:
            VMError: If any step fails.
        """
        config = self.config
        archive = await asyncio.to_thread(pack_project, config)
        self.copied.write_bytes(archive)
        await machine.put(config.target, archive)
        await _check(machine, f"cd {shlex.quote(config.target)}", "enter the project")
        for command in config.prepare:
            await _check(machine, command, "prepare")
        await _start_services(machine, config, after_setup=False)
        for command in config.setup:
            await _check(machine, command, "setup")
        await _start_services(machine, config, after_setup=True)
        await _check(machine, f"cd {shlex.quote(config.target)}", "enter the project")
        self.baseline.write_bytes(await machine.get(config.target, exclude=config.exclude))

    def _warm_path(self) -> Path | None:
        """This project's warm snapshot folder for its current files and VM; ``None`` without
        warm starts."""
        if self.warm_dir is None:
            return None
        return self.warm_dir / project_id(self.config) / warm_key(self.config, self.backend.vm_dir)

    async def _save_warm(self, machine: Machine, warm: Path) -> None:
        """Save the prepared machine for later sessions; a failure only costs them the warm start.

        Each session writes its own partial folder and renames it into place, so a crash never
        leaves a half snapshot that looks complete and concurrent sessions never mix files.
        """
        partial = warm.with_name(f"{warm.name}.{uuid.uuid4().hex[:8]}{PARTIAL_SUFFIX}")
        try:
            await self.backend.save(machine, partial)
            shutil.copyfile(self.baseline, partial / BASELINE)
            shutil.copyfile(self.copied, partial / COPIED)
            (partial / WARM_COMPLETE).touch()
            partial.rename(warm)
        except (OSError, VMError) as error:
            # Includes another session having saved the same key first: theirs is as good.
            shutil.rmtree(partial, ignore_errors=True)
            if not (warm / WARM_COMPLETE).exists():
                log.warning("could not save the warm start %s: %s", warm, error)
            return
        _prune_warm(warm)


def _prune_warm(keep: Path) -> None:
    """Delete the project's other warm snapshots, except those a session still uses and
    partial ones another session may still be writing."""
    for entry in keep.parent.iterdir():
        if entry == keep:
            continue
        try:
            _prune_one(entry)
        except OSError as error:  # e.g. another session deleted it first
            log.warning("could not prune warm start %s: %s", entry, error)


def _prune_one(entry: Path) -> None:
    """Delete a warm snapshot no session borrows, or a partial one old enough to be a crash's leftover."""
    if entry.name.endswith(PARTIAL_SUFFIX):
        if time.time() - entry.stat().st_mtime > STALE_PARTIAL_S:
            shutil.rmtree(entry, ignore_errors=True)
        return
    lock = try_lock_saved(entry)
    if lock is None:
        return
    with lock:
        shutil.rmtree(entry, ignore_errors=True)


async def project_diff(machine: Machine, config: ProjectConfig, baseline: bytes) -> str:
    """A patch from the project as it was when the machine was ready (``baseline``, an archive of
    the project folder) to the project as the machine has it now; ``git apply`` in the local copy
    applies it.

    Comparing with the machine's own starting point, not with the local files, keeps edits made
    locally since then, and files the project's setup wrote, out of the patch.

    Raises:
        VMError: If the project folder cannot be read in the machine.
        ProjectError: If ``git`` (used for the diff) is missing or fails on the host.
    """
    archive = await machine.get(config.target, exclude=config.exclude)
    with tempfile.TemporaryDirectory(prefix="chronobox-diff-") as work_dir:
        work = Path(work_dir)
        _unpack_for_diff(archive, work / "machine")
        _unpack_for_diff(baseline, work / "host")
        return await asyncio.to_thread(_git_diff, work)


async def export(machine: Machine, config: ProjectConfig, path: str, where: str) -> Path:
    """Copy ``path`` from the machine into ``<project>/.chronobox/exports/<where>/``.

    Raises:
        ProjectError: If ``path`` is not absolute or climbs with ``..``.
        VMError: If the path does not exist in the machine.
    """
    exports = config.path / EXPORTS_DIR / where
    destination = _export_destination(exports, path)
    archive = await machine.get(path)
    if destination.is_dir() and not destination.is_symlink():
        shutil.rmtree(destination)
    elif destination.is_symlink() or destination.exists():
        destination.unlink()
    # A file comes under its own name, so it goes into its parent folder.
    _unpack(archive, destination if _holds_a_folder(archive) else destination.parent)
    return destination


def _export_destination(exports: Path, path: str) -> Path:
    """Where ``path`` of the machine goes under ``exports``; it must stay there, since what is
    there is deleted first.

    Raises:
        ProjectError: If ``path`` is not absolute, climbs with ``..``, or leaves ``exports``.
    """
    machine_path = PurePosixPath(path)
    if not machine_path.is_absolute() or ".." in machine_path.parts:
        raise ProjectError(f"export needs an absolute path without '..' in the machine, not {path!r}")
    destination = exports.joinpath(*machine_path.parts[1:])
    root = exports.resolve()
    if destination != exports and not destination.parent.resolve().is_relative_to(root):
        raise ProjectError(f"{path!r} would be exported outside {exports}")
    return destination


def local_edits(copied: bytes, current: bytes) -> tuple[bytes, list[str], list[str]]:
    """What changed in the local project since it was copied in.

    Args:
        copied: The project as copied into the machine (a ``pack_project`` archive).
        current: The project as it is locally now (another ``pack_project`` archive).

    Returns:
        An archive of the files and links that are new or different (with their folders), their
        names, and the names of files and links deleted locally.
    """
    before = _members(copied)
    now = _members(current)
    changed = sorted(
        name
        for name, (info, data) in now.items()
        if not info.isdir() and (name not in before or _identity(*before[name]) != _identity(info, data))
    )
    deleted = sorted(name for name, (info, _) in before.items() if not info.isdir() and name not in now)
    folders = {str(parent) for name in changed for parent in PurePosixPath(name).parents} - {"."}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in sorted(folders & now.keys()):
            tar.addfile(now[name][0])
        for name in changed:
            info, data = now[name]
            tar.addfile(info, io.BytesIO(data) if info.isfile() else None)
    return buffer.getvalue(), changed, deleted


async def sync_local(machine: Machine, config: ProjectConfig, copied: bytes) -> tuple[list[str], list[str]]:
    """Copy the local project's edits since it was copied in (new, changed and deleted files) into
    the machine's project folder; everything else there stays as the machine has it.

    Returns:
        The names copied and the names deleted.

    Raises:
        VMError: If the files cannot be written or deleted in the machine.
    """
    current = await asyncio.to_thread(pack_project, config)
    archive, changed, deleted = local_edits(copied, current)
    if deleted:
        names = " ".join(shlex.quote(name) for name in deleted)
        await _check(machine, f"cd {shlex.quote(config.target)} && rm -f -- {names}", "sync deletions")
    if changed:
        await machine.put(config.target, archive)
    return changed, deleted


def project_tools(workspace: Workspace, boot: ProjectBoot) -> dict[str, Method]:
    """The project's tool methods (``diff``, ``export``, ``sync``), for ``WorkspaceService.extra_methods``."""
    config = boot.config

    async def diff(where: str = MAIN) -> str:
        """The ``diff`` tool: a patch of the project on ``where``; failures come back as ``error: ...``."""
        try:
            baseline = await asyncio.to_thread(boot.baseline.read_bytes)
            patch = await project_diff(workspace.machine(where), config, baseline)
        except (VMError, ProjectError, tarfile.TarError, OSError) as error:
            return f"error: {error}"
        return patch or f"no changes: {config.target} on {where} matches the project as it started"

    async def export_path(path: str, where: str = MAIN) -> str:
        """The ``export`` tool: copy ``path`` from ``where`` into the exports folder; failures
        come back as ``error: ...``."""
        try:
            destination = await export(workspace.machine(where), config, path, where)
        except (VMError, ProjectError, tarfile.TarError, OSError) as error:
            return f"error: {error}"
        return f"copied {path} from {where} to {destination}"

    async def sync(where: str = MAIN) -> str:
        """The ``sync`` tool: copy local edits into ``where``; failures come back as ``error: ...``."""
        try:
            copied = await asyncio.to_thread(boot.copied.read_bytes)
            changed, deleted = await sync_local(workspace.machine(where), config, copied)
        except (VMError, tarfile.TarError, OSError) as error:
            return f"error: {error}"
        if not changed and not deleted:
            return f"no local edits to sync: the local files of {config.path.name} are as they were copied in"
        parts = [f"copied {len(changed)} file(s): {_names(changed)}"] if changed else []
        parts += [f"deleted {len(deleted)} file(s): {_names(deleted)}"] if deleted else []
        return f"{'; '.join(parts)} (in {config.target} on {where})"

    return {"diff": diff, "export": export_path, "sync": sync}


def project_note(config: ProjectConfig, *, with_handoff: bool = False) -> str:
    """What the agent is told about the project, next to the profile's instructions; ``with_handoff``
    adds ``verify_script``, which only the chronobox profile (with forks) has."""
    services = ", ".join(service.name for service in config.services) or "none"
    note = (
        f"The project {config.path.name} is copied at {config.target} in the machine (commands start "
        f"there). Its services ({services}) are running, restarted if they exit; logs in {LOG_DIR}. "
        "`diff` shows what changed in the project on the main machine or a fork since the machine "
        "started, as a patch for the local copy; `export` copies files out of the machine. The "
        "project's services and dependencies run only in the machine: if you edit the project's "
        "files locally, `sync` copies your edits into main or a fork so you can run and test them "
        "there."
    )
    if not with_handoff:
        return note
    return (
        f"{note} When a change needs actions beyond file edits (a migration, restarts, settings "
        "outside the project), write them as a script and check it with `verify_script`, which "
        "replays it on a fresh fork and saves it with the patch for the user."
    )


async def _start_services(machine: Machine, config: ProjectConfig, *, after_setup: bool) -> None:
    """Start the services of one phase (before or after ``setup``) in order, each ready before the next.

    Raises:
        VMError: If a service does not start or does not get ready.
    """
    for service in config.services:
        if service.after_setup == after_setup:
            await _check(machine, service_command(service, config.target), f"start {service.name}")
            await _wait_ready(machine, service)


async def _check(machine: Machine, command: str, step: str) -> str:
    """Run ``command`` on the machine and return its output.

    Raises:
        VMError: If it exits non-zero; the message names ``step`` and ends with the output.
    """
    code, output = await machine.run(command)
    if code != 0:
        raise VMError(f"{step} failed ({code}): {command}\n{output[-1500:]}")
    return output


async def _wait_ready(machine: Machine, service: Service) -> None:
    """Poll the service's ``ready`` command (isolated) until it succeeds.

    Raises:
        VMError: If it is not ready within its ``ready_timeout``; the message ends with the tail of its log.
    """
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
    """Unpack an archive from the machine into the project: nothing may land outside
    ``destination`` or link out of it (tar's ``data`` filter)."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")


def _unpack_for_diff(archive: bytes, destination: Path) -> None:
    """Unpack into a private scratch folder for git to compare: links are kept as they are (an
    absolute one, e.g. in a virtualenv, is a change like any other), special files skipped."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter=_keep_links_skip_special)


def _keep_links_skip_special(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
    """Tar filter for diffs: device files and FIFOs are skipped; the rest goes through tar's ``tar``
    filter, which keeps every member inside the destination but leaves links as they are."""
    if member.type in SPECIAL_FILES:
        return None
    return tarfile.tar_filter(member, destination)


def _members(archive: bytes) -> dict[str, tuple[tarfile.TarInfo, bytes]]:
    """Each member of a project archive by its normalized name, with its content (empty for
    folders and links)."""
    members: dict[str, tuple[tarfile.TarInfo, bytes]] = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for info in tar.getmembers():
            data = tar.extractfile(info).read() if info.isfile() else b""
            members[str(PurePosixPath(info.name))] = (info, data)
    return members


def _identity(info: tarfile.TarInfo, data: bytes) -> tuple[bytes, int, str, bytes]:
    """What makes a file the same for ``local_edits``: type, mode, link target and content (not
    its time, so touching a file is not an edit)."""
    return info.type, info.mode, info.linkname, data


def _names(names: list[str]) -> str:
    """The first few names, comma-separated, with a count of the rest."""
    shown = ", ".join(names[:SHOWN_NAMES])
    return shown if len(names) <= SHOWN_NAMES else f"{shown} and {len(names) - SHOWN_NAMES} more"


def _holds_a_folder(archive: bytes) -> bool:
    """Whether the archive is a folder's content (``./...``) rather than one file under its name."""
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return any(member.name == "." or member.name.startswith("./") for member in tar.getmembers())


def _git_diff(work: Path) -> str:
    """The patch from ``work/host`` to ``work/machine``, named by project paths.

    Raises:
        ProjectError: If git is missing or fails.
    """
    if shutil.which("git") is None:
        raise ProjectError("git is needed on the host for diffs")
    done = subprocess.run(DIFF_COMMAND, cwd=work, capture_output=True, text=True)
    if done.returncode not in (NO_DIFFERENCES, DIFFERENCES):
        raise ProjectError(f"git diff failed: {done.stderr.strip()}")
    return "".join(_project_paths(line) for line in done.stdout.splitlines(keepends=True))


def _project_paths(line: str) -> str:
    """Name files in header lines by their project path instead of the scratch folder."""
    if not line.startswith(DIFF_HEADERS):
        return line
    return RENAME_FOLDER.sub(r"\1", SIDE_FOLDER.sub(r"\1", line))
