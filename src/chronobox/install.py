"""``chronobox setup`` and ``chronobox doctor``: get the VM files, then check a machine boots.

``setup`` downloads Firecracker and a guest kernel, then builds the tools drive and the default
image with Docker, into the VM folder (``default_vm_dir()``). Steps already done are skipped.
``doctor`` checks each requirement in turn, boots one machine and says what to fix.
"""

from __future__ import annotations

import asyncio
import http.client
import os
import platform
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from chronobox.images import DEFAULT_IMAGE, TOOLS_DISK, build_image, build_tools, bundled_vm_files, image_disk
from chronobox.vm import FirecrackerBackend, MachineSpec, VMError

FIRECRACKER_VERSION = "v1.17.0"
FIRECRACKER_URL = (
    "https://github.com/firecracker-microvm/firecracker/releases/download/"
    "{version}/firecracker-{version}-{arch}.tgz"
)
# The kernel Firecracker's own CI boots (6.1 series, vsock built in): the newest in the listing.
KERNEL_BUCKET = "https://s3.amazonaws.com/spec.ccfc.min"
KERNEL_PREFIX = "firecracker-ci/v1.15/{arch}/vmlinux-6.1"
SUPPORTED_ARCH = "x86_64"
DOWNLOAD_TIMEOUT_S = 120
CHUNK_BYTES = 1 << 20
BYTES_PER_MIB = 1 << 20
KVM = Path("/dev/kvm")
BOOT_ERROR_LINES = 8
FILE_A_BUG = "please report it: https://github.com/Geawher/chronobox/issues (with this output)"
DOCKER_TIMEOUT_S = 20


class SetupError(RuntimeError):
    """A download or build step of ``chronobox setup`` failed."""


@dataclass(frozen=True)
class Check:
    """One line of ``chronobox doctor``.

    Attributes:
        name: What was checked.
        ok: Whether it passed.
        detail: What was found.
        fix: What to do when it failed.
        required: Whether a failure stops machines from running (else it is a warning).
    """

    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True


def setup(vm_dir: Path, *, say: Callable[[str], None] = print) -> None:
    """Download Firecracker and the kernel, then build the tools drive and the default image.

    Each step is skipped if its file is already there; delete the file to redo it.

    Raises:
        SetupError: If this is not an x86_64 Linux machine, or a download fails.
        ImageError: If Docker is missing or a build fails.
    """
    arch = platform.machine()
    if platform.system() != "Linux" or arch != SUPPORTED_ARCH:
        raise SetupError(f"chronobox runs on x86_64 Linux with KVM; this is {platform.system()} {arch}")
    vm_dir.mkdir(parents=True, exist_ok=True)
    say(f"VM files go to {vm_dir}")
    say("about 700 MB, plus Docker's build cache; the first run takes 5-10 minutes")
    steps = (
        ("firecracker", "Firecracker " + FIRECRACKER_VERSION, lambda: fetch_firecracker(vm_dir, arch)),
        ("vmlinux", "the guest kernel", lambda: fetch_kernel(vm_dir, arch)),
        (TOOLS_DISK, "the tools drive (Docker)", lambda: build_tools(vm_dir)),
        (
            str(image_disk(vm_dir, DEFAULT_IMAGE).relative_to(vm_dir)),
            "the default image: Debian with Python, PostgreSQL, nginx (Docker)",
            lambda: build_default_image(vm_dir),
        ),
    )
    for file, what, step in steps:
        if (vm_dir / file).exists():
            say(f"  have {what}")
            continue
        say(f"  getting {what}...")
        started = time.monotonic()
        step()
        say(f"  got {what} in {time.monotonic() - started:.0f} s")
    say("ready: `chronobox doctor` checks that a machine boots")


def build_default_image(vm_dir: Path) -> None:
    """Build the default image from the Dockerfile shipped with chronobox."""
    dockerfile = bundled_vm_files() / "images" / "default" / "Dockerfile"
    build_image(vm_dir, dockerfile=dockerfile, name=DEFAULT_IMAGE)


def fetch_firecracker(vm_dir: Path, arch: str) -> None:
    """Download the Firecracker release and keep its ``firecracker`` binary.

    Raises:
        SetupError: If the download fails or the archive has no binary.
    """
    url = FIRECRACKER_URL.format(version=FIRECRACKER_VERSION, arch=arch)
    archive = vm_dir / "firecracker.tgz.part"
    download(url, archive)
    member = f"release-{FIRECRACKER_VERSION}-{arch}/firecracker-{FIRECRACKER_VERSION}-{arch}"
    try:
        with tarfile.open(archive) as release:
            binary = release.extractfile(member)
            if binary is None:
                raise KeyError(member)
            partial = vm_dir / "firecracker.part"
            partial.write_bytes(binary.read())
    except (KeyError, EOFError, tarfile.TarError) as error:
        raise SetupError(f"no Firecracker binary in {url}: {error}") from error
    finally:
        archive.unlink(missing_ok=True)
    partial.chmod(0o755)
    partial.rename(vm_dir / "firecracker")


def fetch_kernel(vm_dir: Path, arch: str) -> None:
    """Download the newest 6.1 kernel from Firecracker's CI bucket.

    Raises:
        SetupError: If the listing has no kernel or a download fails.
    """
    prefix = KERNEL_PREFIX.format(arch=arch)
    listing = read_url(f"{KERNEL_BUCKET}/?prefix={prefix}&list-type=2").decode()
    keys = re.findall(rf"<Key>({re.escape(prefix)}\.(\d+))</Key>", listing)
    if not keys:
        raise SetupError(f"no kernel under {KERNEL_BUCKET}/{prefix}")
    key = max(keys, key=lambda found: int(found[1]))[0]
    partial = vm_dir / "vmlinux.part"
    download(f"{KERNEL_BUCKET}/{key}", partial)
    partial.rename(vm_dir / "vmlinux")


def read_url(url: str) -> bytes:
    """The body of a small download.

    Raises:
        SetupError: If the request fails.
    """
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as response:
            return response.read()
    except (OSError, http.client.HTTPException) as error:
        raise SetupError(f"could not download {url}: {error}") from error


def download(url: str, target: Path) -> None:
    """Stream a download into ``target``.

    Raises:
        SetupError: If the request fails (``target`` is then removed).
    """
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as response, target.open("wb") as out:
            shutil.copyfileobj(response, out, CHUNK_BYTES)
    except (OSError, http.client.HTTPException) as error:
        target.unlink(missing_ok=True)
        raise SetupError(f"could not download {url}: {error}") from error


def doctor(vm_dir: Path) -> list[Check]:
    """Check this host can run chronobox machines, ending with a real boot when the rest passed."""
    checks = [check_platform(), check_kvm(), check_docker(), *check_vm_files(vm_dir)]
    if all(check.ok for check in checks if check.required):
        checks.append(asyncio.run(check_boot(vm_dir)))
    return checks


def check_platform() -> Check:
    """x86_64 Linux: Firecracker needs KVM, and chronobox's VM files are built for x86_64."""
    found = f"{platform.system()} {platform.machine()}"
    ok = platform.system() == "Linux" and platform.machine() == SUPPORTED_ARCH
    return Check(
        "x86_64 Linux",
        ok,
        found,
        "use an x86_64 Linux machine, or a Linux VM with nested virtualization",
    )


def check_kvm() -> Check:
    """Read and write access to ``/dev/kvm``, without root."""
    if not KVM.exists():
        return Check(
            "KVM",
            False,
            "no /dev/kvm",
            "enable virtualization (VT-x / AMD-V) in the BIOS, or nested virtualization for a VM; "
            "then load the kvm_intel or kvm_amd module",
        )
    if not os.access(KVM, os.R_OK | os.W_OK):
        return Check(
            "KVM",
            False,
            "/dev/kvm exists but this user cannot open it",
            "sudo usermod -aG kvm $USER, then log out and back in",
        )
    return Check("KVM", True, "/dev/kvm is usable")


def check_docker() -> Check:
    """Docker answers this user (only needed to build images)."""
    fix = "install Docker, then: sudo usermod -aG docker $USER, and log out and back in"
    if shutil.which("docker") is None:
        return Check("Docker", False, "not installed (needed to build images)", fix, required=False)
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=DOCKER_TIMEOUT_S)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return Check("Docker", False, "installed, but does not answer this user", fix, required=False)
    return Check("Docker", True, "answers (used to build images)", required=False)


def check_vm_files(vm_dir: Path) -> list[Check]:
    """Firecracker, the kernel, the tools drive and the default image are in the VM folder."""
    files = {
        "Firecracker": vm_dir / "firecracker",
        "guest kernel": vm_dir / "vmlinux",
        "tools drive": vm_dir / TOOLS_DISK,
        "default image": image_disk(vm_dir, DEFAULT_IMAGE),
    }
    return [
        Check(name, path.exists(), str(path) if path.exists() else f"missing in {vm_dir}", "chronobox setup")
        for name, path in files.items()
    ]


async def check_boot(vm_dir: Path) -> Check:
    """Boot a machine on the default image, run one command in it, and stop it."""
    started = time.monotonic()
    try:
        backend = FirecrackerBackend(vm_dir=vm_dir)
        try:
            machine = await backend.start(MachineSpec())
            code, output = await machine.run("uname -sr", 30)
        finally:
            await backend.cleanup()
    except (VMError, OSError) as error:
        lines = str(error).strip().splitlines() or [type(error).__name__]
        details = "\n      ".join(lines[1:][-BOOT_ERROR_LINES:])
        return Check("a machine boots", False, lines[0], details or FILE_A_BUG)
    if code != 0:
        return Check("a machine boots", False, f"`uname -sr` failed ({code}): {output.strip()}", FILE_A_BUG)
    seconds = time.monotonic() - started
    return Check("a machine boots", True, f"{output.strip()} in {seconds:.1f} s")
