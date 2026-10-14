"""Firecracker microVMs whose snapshots capture the whole machine.

Each VM boots the shared read-only tools drive (``vda``: init and guest agent), mounts
its image (``vdb``, the root filesystem of a Docker image, read-only, see
``chronobox.images``) under a small writable disk (``vdc``) with an overlay, and runs
shell commands sent over vsock inside that image. A snapshot pauses the VM, saves its memory and
device state plus a sparse copy of the writable disk, and resumes it, so running
processes, open connections, page cache and in-memory data all survive a
restore. Restoring the same snapshot twice gives two independent machines.

Rootless: only read/write access to ``/dev/kvm`` is needed. VMs have no network
interface; everything runs inside the VM (loopback works).
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from chronobox.images import DEFAULT_IMAGE, IMAGE_NAME, TOOLS_DISK, image_disk

GUEST_PORT = 5000
GUEST_CID = 3
BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off quiet init=/sbin/chronobox-init"
VM_FILES = ("firecracker", "vmlinux", TOOLS_DISK)
IMAGE_LINK = "image.ext4"
LINK_REFUSED = (errno.EXDEV, errno.EPERM, errno.EACCES)
# Read-only disks every machine shares with the VM files; snapshots keep links to them.
SHARED_DISKS = (TOOLS_DISK, IMAGE_LINK)
DATA_DISK = "data.ext4"
# (drive id, file in the VM's directory, root device, read-only): the guest sees them as vda, vdb
# and vdc in this order, which the tools drive's init relies on.
DRIVES = (
    ("tools", TOOLS_DISK, True, True),
    ("image", IMAGE_LINK, False, True),
    ("data", DATA_DISK, False, False),
)
SETUP_TIMEOUT_S = 300.0
GUEST_CONNECT_TIMEOUT_S = 5.0
GUEST_REPLY_MARGIN_S = 30.0
GUEST_REPLY_LIMIT = 2**26  # one JSON line holds the whole output; callers shorten it
READY_TIMEOUT_S = 30.0
API_SOCKET_WAIT_S = 2.0
CONTENT_LENGTH = "content-length"
_PR_SET_PDEATHSIG = 1
_SIGKILL = 9


class VMError(RuntimeError):
    """A VM could not be started, snapshotted, restored or reached."""


def default_vm_dir() -> Path:
    """Where the Firecracker binary, kernel, tools drive and images live.

    ``CHRONOBOX_VM_DIR`` wins; then ``vm/build`` of a source checkout, if built; then
    ``$XDG_DATA_HOME/chronobox/vm`` (``~/.local/share/chronobox/vm``), where an installed
    package looks.
    """
    configured = os.environ.get("CHRONOBOX_VM_DIR")
    if configured:
        return Path(configured).expanduser()
    checkout = Path(__file__).resolve().parents[2] / "vm" / "build"
    if checkout.is_dir():
        return checkout
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "chronobox" / "vm"


@dataclass(frozen=True)
class MachineSpec:
    """How to start a machine.

    Attributes:
        setup: Shell commands run once after boot, in order (start services, load data).
        image: Name of the machine's image (``chronobox build``); snapshots keep it.
        mem_mib: Guest memory; allocated lazily by the host.
        vcpus: Virtual CPUs.
        data_mib: Size of the sparse writable disk.
        command_timeout: Default timeout for one command, in seconds.
    """

    setup: tuple[str, ...] = ()
    image: str = DEFAULT_IMAGE
    mem_mib: int = 1024
    vcpus: int = 2
    data_mib: int = 2048
    command_timeout: float = 300.0


@dataclass(frozen=True)
class Snapshot:
    """Handle to a saved machine; restore it with ``FirecrackerBackend.restore``."""

    key: str


@dataclass
class BackendStats:
    boots: int = 0
    snapshots: int = 0
    restores: int = 0
    snapshot_seconds: float = 0.0
    restore_seconds: float = 0.0


class Machine:
    """A running VM; run shell commands in it as root."""

    def __init__(self, workdir: Path, spec: MachineSpec) -> None:
        self.workdir = workdir
        self.spec = spec
        self.process: subprocess.Popen[bytes] | None = None
        # Unix socket paths are limited to ~108 bytes; reach this VM's sockets through a
        # short link so the run directory can live anywhere.
        self.link = Path(tempfile.gettempdir()) / f"cbx-{uuid.uuid4().hex[:12]}"
        self.link.symlink_to(workdir)

    @property
    def api_socket(self) -> Path:
        return self.link / "api.sock"

    async def run(
        self, command: str, timeout: float | None = None, *, isolated: bool = False
    ) -> tuple[int, str]:
        """Run a bash command; returns (exit code, combined stdout/stderr).

        The working directory and exported variables persist between commands, so
        commands run one at a time. ``isolated`` commands are for observers (probes,
        checkers): they run alongside the others in a private mount namespace, without
        that shared state, and their script is not visible to other processes.
        """
        timeout = timeout or self.spec.command_timeout
        request = {"op": "run", "command": command, "timeout": timeout, "isolated": isolated}
        reply = await self._guest(request, timeout)
        return int(reply.get("code", -1)), str(reply.get("output", ""))

    async def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            await asyncio.to_thread(self.process.wait)
        self.link.unlink(missing_ok=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    async def wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise VMError(f"firecracker exited: {self.log_tail()}")
            try:
                await self._guest({"op": "ping"}, 2)
                return
            except (OSError, VMError, TimeoutError, json.JSONDecodeError):
                await asyncio.sleep(0.05)
        raise VMError(f"guest agent not ready after {READY_TIMEOUT_S:g}s: {self.log_tail()}")

    def log_tail(self) -> str:
        log = self.workdir / "firecracker.log"
        return log.read_text(errors="replace")[-1500:] if log.exists() else ""

    async def _guest(self, request: dict[str, object], timeout: float) -> dict[str, object]:
        connect = asyncio.open_unix_connection(str(self.link / "v.sock"), limit=GUEST_REPLY_LIMIT)
        reader, writer = await asyncio.wait_for(connect, GUEST_CONNECT_TIMEOUT_S)
        try:
            writer.write(f"CONNECT {GUEST_PORT}\n".encode())
            await writer.drain()
            ack = await asyncio.wait_for(reader.readline(), GUEST_CONNECT_TIMEOUT_S)
            if not ack.startswith(b"OK"):
                raise VMError(f"vsock connect failed: {ack!r}")
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout + GUEST_REPLY_MARGIN_S)
            return json.loads(line)
        finally:
            writer.close()


class FirecrackerBackend:
    """Starts, snapshots and restores machines under ``root`` (deleted by ``cleanup``)."""

    def __init__(self, root: Path | None = None, vm_dir: Path | None = None) -> None:
        self.vm_dir = vm_dir or default_vm_dir()
        missing = [name for name in VM_FILES if not (self.vm_dir / name).exists()]
        if missing:
            raise VMError(f"missing {missing} in {self.vm_dir}; run vm/fetch.sh and vm/build_rootfs.sh")
        self.root = (root or Path("runs/vms") / uuid.uuid4().hex[:10]).resolve()
        (self.root / "vms").mkdir(parents=True, exist_ok=True)
        (self.root / "snapshots").mkdir(parents=True, exist_ok=True)
        self.stats = BackendStats()
        self._ids = itertools.count()
        self._specs: dict[str, MachineSpec] = {}
        self._live: set[Machine] = set()

    async def start(self, spec: MachineSpec) -> Machine:
        """Boot a fresh machine and run its setup commands.

        Raises:
            VMError: If the VM does not boot or a setup command fails.
        """
        if not IMAGE_NAME.fullmatch(spec.image) or not image_disk(self.vm_dir, spec.image).exists():
            raise VMError(f"image {spec.image!r} is not built in {self.vm_dir}: run `chronobox build`")
        machine = self._new_machine(spec)
        await asyncio.to_thread(self._make_data_disk, machine.workdir / DATA_DISK, spec.data_mib)
        await self._launch(machine)
        await self._configure_and_boot(machine)
        await machine.wait_ready()
        self.stats.boots += 1
        for command in spec.setup:
            code, output = await machine.run(command, max(spec.command_timeout, SETUP_TIMEOUT_S))
            if code != 0:
                await self._close(machine)
                raise VMError(f"setup failed ({code}): {command}\n{output[-1500:]}")
        return machine

    async def snapshot(self, machine: Machine) -> Snapshot:
        """Save the whole machine; it is paused for the duration (typically < 1 s)."""
        started = time.monotonic()
        key = str(next(self._ids))
        store = self.root / "snapshots" / key
        store.mkdir()
        await _api(machine.api_socket, "PATCH", "/vm", {"state": "Paused"})
        try:
            await _api(machine.api_socket, "PUT", "/snapshot/create", {
                "snapshot_type": "Full",
                "snapshot_path": str(store / "vm.snap"),
                "mem_file_path": str(store / "vm.mem"),
            })  # fmt: skip
            await asyncio.to_thread(_sparse_copy, machine.workdir / DATA_DISK, store / DATA_DISK)
            for disk in SHARED_DISKS:
                _link_disk(machine.workdir / disk, store / disk)
        finally:
            await _api(machine.api_socket, "PATCH", "/vm", {"state": "Resumed"})
        self._specs[key] = machine.spec
        self.stats.snapshots += 1
        self.stats.snapshot_seconds += time.monotonic() - started
        return Snapshot(key)

    async def restore(self, snapshot: Snapshot) -> Machine:
        """Start a new, independent machine from a snapshot."""
        started = time.monotonic()
        store = self.root / "snapshots" / snapshot.key
        machine = self._new_machine(self._specs[snapshot.key], store)
        await asyncio.to_thread(_sparse_copy, store / DATA_DISK, machine.workdir / DATA_DISK)
        await self._launch(machine)
        await _api(machine.api_socket, "PUT", "/snapshot/load", {
            "snapshot_path": str(store / "vm.snap"),
            "mem_backend": {"backend_type": "File", "backend_path": str(store / "vm.mem")},
            "resume_vm": True,
        })  # fmt: skip
        await machine.wait_ready()
        self.stats.restores += 1
        self.stats.restore_seconds += time.monotonic() - started
        return machine

    async def release(self, snapshot: Snapshot) -> None:
        self._specs.pop(snapshot.key, None)
        shutil.rmtree(self.root / "snapshots" / snapshot.key, ignore_errors=True)

    async def stop(self, machine: Machine) -> None:
        await self._close(machine)

    async def cleanup(self) -> None:
        """Stop every machine and delete this backend's VMs and snapshots."""
        for machine in list(self._live):
            await self._close(machine)
        shutil.rmtree(self.root, ignore_errors=True)

    async def _close(self, machine: Machine) -> None:
        self._live.discard(machine)
        await machine.close()

    async def _configure_and_boot(self, machine: Machine) -> None:
        spec = machine.spec
        api = machine.api_socket
        await _api(api, "PUT", "/boot-source", {"kernel_image_path": "vmlinux", "boot_args": BOOT_ARGS})
        for drive_id, path, is_root, is_read_only in DRIVES:
            await _api(api, "PUT", f"/drives/{drive_id}", {
                "drive_id": drive_id, "path_on_host": path,
                "is_root_device": is_root, "is_read_only": is_read_only,
            })  # fmt: skip
        await _api(api, "PUT", "/machine-config", {"vcpu_count": spec.vcpus, "mem_size_mib": spec.mem_mib})
        await _api(api, "PUT", "/vsock", {"guest_cid": GUEST_CID, "uds_path": "v.sock"})
        await _api(api, "PUT", "/actions", {"action_type": "InstanceStart"})

    def _new_machine(self, spec: MachineSpec, store: Path | None = None) -> Machine:
        """A machine folder; a restored machine gets the exact disks its snapshot was taken on."""
        workdir = self.root / "vms" / f"vm{next(self._ids)}"
        workdir.mkdir(parents=True)
        (workdir / "vmlinux").symlink_to(self.vm_dir / "vmlinux")
        current = {TOOLS_DISK: self.vm_dir / TOOLS_DISK, IMAGE_LINK: image_disk(self.vm_dir, spec.image)}
        for disk in SHARED_DISKS:
            saved = store / disk if store is not None else None
            # Snapshots from before disks were linked into them use the current disks.
            _link_disk(saved if saved is not None and saved.exists() else current[disk], workdir / disk)
        machine = Machine(workdir, spec)
        self._live.add(machine)
        return machine

    async def _launch(self, machine: Machine) -> None:
        # Launch from the event-loop thread: the death signal is tied to the launching
        # thread, and this one lives as long as the process.
        with open(machine.workdir / "firecracker.log", "wb") as log:
            machine.process = subprocess.Popen(
                [str(self.vm_dir / "firecracker"), "--api-sock", "api.sock"],
                cwd=machine.workdir,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                preexec_fn=_die_with_parent,
            )
        deadline = time.monotonic() + API_SOCKET_WAIT_S
        while not machine.api_socket.exists():
            if time.monotonic() > deadline:
                raise VMError(f"firecracker API socket did not appear: {machine.log_tail()}")
            await asyncio.sleep(0.01)

    def _make_data_disk(self, path: Path, size_mib: int) -> None:
        template = self.root / f"data-template-{size_mib}.ext4"
        if not template.exists():
            with open(template, "wb") as disk:
                disk.truncate(size_mib * 2**20)
            subprocess.run(["mkfs.ext4", "-q", "-F", str(template)], check=True)
        _sparse_copy(template, path)


async def _api(socket_path: Path, method: str, path: str, body: dict[str, object] | None = None) -> str:
    """One request to the Firecracker API over its unix socket; returns the body.

    Raises:
        VMError: On any non-2xx status.
    """
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    payload = json.dumps(body).encode() if body is not None else b""
    head = (
        f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nAccept: application/json\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
    )
    writer.write(head.encode() + payload)
    await writer.drain()
    status = (await reader.readline()).decode()
    length = 0
    while (line := await reader.readline()) not in (b"\r\n", b""):
        name, _, value = line.decode().partition(":")
        if name.lower() == CONTENT_LENGTH:
            length = int(value)
    text = (await reader.readexactly(length)).decode() if length else ""
    writer.close()
    parts = status.split()
    code = parts[1] if len(parts) > 1 else ""
    if not code.startswith("2"):
        raise VMError(f"{method} {path}: {status.strip()} {text}")
    return text


def _die_with_parent() -> None:
    """Make the kernel kill the VM if the process that launched it dies, even by SIGKILL."""
    ctypes.CDLL(None).prctl(_PR_SET_PDEATHSIG, _SIGKILL)


def _link_disk(disk: Path, link: Path) -> None:
    """Hard link, so the file stays the one a machine booted even after ``chronobox build``
    replaces it (a VM's memory holds caches of that exact disk); a symlink where hard links are
    impossible (another filesystem, or VM files owned by another user)."""
    try:
        os.link(disk, link)
    except OSError as error:
        if error.errno not in LINK_REFUSED:
            raise
        link.symlink_to(disk.resolve())


def _sparse_copy(source: Path, target: Path) -> None:
    subprocess.run(["cp", "--sparse=always", str(source), str(target)], check=True)
