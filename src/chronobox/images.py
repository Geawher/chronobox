"""Machine images: any Docker image or Dockerfile as the system of a chronobox machine.

A machine boots from two read-only disks: the **tools drive** (``tools.ext4``, built once
from ``vm/tools``: the init and the guest agent) and an **image disk**, the root filesystem
of a Docker image. The image needs nothing from chronobox: the guest agent runs from the
tools drive and enters the image with chroot.

Images live under ``<vm dir>/images/<name>/``: ``rootfs.ext4`` (the disk) and ``image.json``
(its Docker config: environment, working directory, entrypoint and command). The same
config is written inside the disk at ``/.chronobox/image.json`` for the guest agent.

Building is rootless: unpacking an image while keeping its file owners, and making the ext4
disk, happen inside a container of the tools image.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_IMAGE = "default"
TOOLS_DISK = "tools.ext4"
IMAGE_DISK = "rootfs.ext4"
IMAGE_CONFIG = "image.json"
IMAGES_DIR = "images"
TOOLS_TAG = "chronobox-tools:latest"
IMAGE_TAG_PREFIX = "chronobox-image-"
EXPORT_COMMAND = "chronobox-export"
# Free room on top of the files: ext4 metadata and journal (the image disk is read-only;
# writes go to the machine's data disk).
DISK_MARGIN_PERCENT = 20
DISK_MARGIN_MIB = 128
INODE_MARGIN = 4096
_NAME_ALLOWED = re.compile(r"[^a-z0-9._-]+")

# Runs in a tools container with the work directory mounted at /work: unpack as root so file
# owners survive, size the disk to fit, write it, and hand it back to the host user.
_MAKE_DISK = """
set -e
mkdir /tmp/root
tar -xf /work/rootfs.tar -C /tmp/root --numeric-owner
if [ -f /work/image.json ]; then
  mkdir -p /tmp/root/.chronobox && cp /work/image.json /tmp/root/.chronobox/image.json
fi
size_mib=$(du -sm /tmp/root | cut -f1)
files=$(find /tmp/root | wc -l)
mkfs.ext4 -q -L chronobox -N $((files + {inode_margin})) -d /tmp/root /work/disk.ext4 \
  $((size_mib + size_mib * {margin_percent} / 100 + {margin_mib}))M
chown {owner} /work/disk.ext4
chmod 644 /work/disk.ext4
"""


class ImageError(RuntimeError):
    """An image could not be built or found."""


@dataclass(frozen=True)
class ImageConfig:
    """What the guest agent takes from an image's Docker config.

    Attributes:
        source: The image reference or Dockerfile the image was built from.
        env: Environment variables every command gets.
        workdir: Working directory of the first command (``/root`` when empty).
        entrypoint: The image's entrypoint, kept for starting its services.
        cmd: The image's default command, kept for starting its services.
    """

    source: str
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = ""
    entrypoint: tuple[str, ...] = ()
    cmd: tuple[str, ...] = ()

    @classmethod
    def from_docker(cls, source: str, config: dict) -> ImageConfig:
        """From the ``Config`` object of ``docker image inspect``."""
        pairs = (item.partition("=") for item in config.get("Env") or ())
        env = {key: value for key, _, value in pairs}
        return cls(
            source=source,
            env=env,
            workdir=config.get("WorkingDir") or "",
            entrypoint=tuple(config.get("Entrypoint") or ()),
            cmd=tuple(config.get("Cmd") or ()),
        )

    @classmethod
    def load(cls, path: Path) -> ImageConfig:
        fields = json.loads(path.read_text())
        return cls(**{**fields, "entrypoint": tuple(fields["entrypoint"]), "cmd": tuple(fields["cmd"])})

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")


@dataclass(frozen=True)
class Image:
    name: str
    config: ImageConfig
    size_bytes: int


def image_name(reference: str) -> str:
    """A file-safe image name from a reference: ``postgres:16`` -> ``postgres-16``,
    ``ghcr.io/org/app:1.2`` -> ``app-1.2``."""
    last = reference.rsplit("/", 1)[-1].lower()
    return _NAME_ALLOWED.sub("-", last.replace(":", "-").replace("@", "-")).strip("-.")


def image_dir(vm_dir: Path, name: str) -> Path:
    return vm_dir / IMAGES_DIR / name


def image_disk(vm_dir: Path, name: str) -> Path:
    return image_dir(vm_dir, name) / IMAGE_DISK


def list_images(vm_dir: Path) -> list[Image]:
    images = []
    for config in sorted((vm_dir / IMAGES_DIR).glob(f"*/{IMAGE_CONFIG}")):
        disk = config.with_name(IMAGE_DISK)
        if disk.exists():
            images.append(Image(config.parent.name, ImageConfig.load(config), disk.stat().st_size))
    return images


def bundled_vm_files() -> Path:
    """The ``vm`` folder (tools and default image sources): in the package, or in a checkout."""
    packaged = Path(__file__).parent / "vm"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "vm"


def build_tools(vm_dir: Path) -> Path:
    """Build the tools drive from ``vm/tools`` into ``<vm dir>/tools.ext4``.

    Raises:
        ImageError: If Docker fails.
    """
    _docker("build", "-q", "-t", TOOLS_TAG, str(bundled_vm_files() / "tools"))
    vm_dir.mkdir(parents=True, exist_ok=True)
    target = vm_dir / TOOLS_DISK
    _export_disk(TOOLS_TAG, target, config=None)
    return target


def build_image(
    vm_dir: Path,
    *,
    reference: str | None = None,
    dockerfile: Path | None = None,
    context: Path | None = None,
    name: str | None = None,
) -> Image:
    """Make an image disk from a Docker image reference or from a Dockerfile.

    Args:
        vm_dir: Where chronobox's VM files live (the tools drive must be built).
        reference: An image to use as is (pulled if missing), e.g. ``postgres:16``.
        dockerfile: A Dockerfile to build instead; ``context`` defaults to its folder.
        context: Build context for ``dockerfile``.
        name: Image name; defaults to one derived from the reference or the context folder.

    Returns:
        The built image.

    Raises:
        ImageError: If neither or both sources are given, the tools drive is missing,
            or Docker fails.
    """
    if (reference is None) == (dockerfile is None):
        raise ImageError("give either an image reference or a Dockerfile")
    if not (vm_dir / TOOLS_DISK).exists():
        raise ImageError(f"no tools drive in {vm_dir}: run `chronobox build --tools` first")
    if dockerfile is not None:
        context = context or dockerfile.parent
        name = name or image_name(context.resolve().name)
        reference = f"{IMAGE_TAG_PREFIX}{name}:latest"
        _docker("build", "-q", "-f", str(dockerfile), "-t", reference, str(context))
        source = str(dockerfile)
    else:
        name = name or image_name(reference)
        _pull_if_missing(reference)
        source = reference
    inspected = json.loads(_docker("image", "inspect", "--format", "{{json .Config}}", reference))
    config = ImageConfig.from_docker(source, inspected)
    target = image_dir(vm_dir, name)
    target.mkdir(parents=True, exist_ok=True)
    _export_disk(reference, target / IMAGE_DISK, config=config)
    config.save(target / IMAGE_CONFIG)
    return Image(name, config, (target / IMAGE_DISK).stat().st_size)


def _export_disk(reference: str, target: Path, *, config: ImageConfig | None) -> None:
    """Write the root filesystem of a Docker image to an ext4 disk at ``target``."""
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".build-") as work_dir:
        work = Path(work_dir)
        # The container is never started; the command only satisfies images that define none.
        container = _docker("create", reference, EXPORT_COMMAND).strip()
        try:
            _docker("export", container, "-o", str(work / "rootfs.tar"))
        finally:
            _docker("rm", container)
        if config is not None:
            config.save(work / IMAGE_CONFIG)
        script = _MAKE_DISK.format(
            inode_margin=INODE_MARGIN,
            margin_percent=DISK_MARGIN_PERCENT,
            margin_mib=DISK_MARGIN_MIB,
            owner=f"{os.getuid()}:{os.getgid()}",
        )
        _docker("run", "--rm", "-v", f"{work}:/work", "--entrypoint", "sh", TOOLS_TAG, "-c", script)
        # Replace in one step, so a machine never boots a half-written disk.
        (work / "disk.ext4").replace(target)


def _pull_if_missing(reference: str) -> None:
    try:
        _docker("image", "inspect", reference)
    except ImageError:
        _docker("pull", "-q", reference)


def _docker(*args: str) -> str:
    if shutil.which("docker") is None:
        raise ImageError("docker is needed to build images")
    try:
        done = subprocess.run(["docker", *args], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise ImageError(f"docker {args[0]} failed: {error.stderr.strip()[-1500:]}") from error
    return done.stdout
