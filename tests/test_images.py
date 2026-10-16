"""Image names, Docker configs and build preconditions (no VM, no Docker needed)."""

from pathlib import Path

import pytest

from chronobox.images import (
    DEFAULT_IMAGE,
    IMAGE_CONFIG,
    IMAGE_DISK,
    TOOLS_DISK,
    ImageConfig,
    ImageError,
    build_image,
    image_dir,
    image_name,
    list_images,
)
from chronobox.vm import VM_FILES, FirecrackerBackend, MachineSpec, VMError


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("postgres:16", "postgres-16"),
        ("node:22-alpine", "node-22-alpine"),
        ("ghcr.io/Org/App:1.2", "app-1.2"),
        ("registry:5000/team/api@sha256:abc", "api-sha256-abc"),
        ("my app", "my-app"),
    ],
)
def test_image_names_are_file_safe(reference: str, expected: str) -> None:
    """Image references become names usable as a folder and a Docker tag."""
    assert image_name(reference) == expected


def test_config_keeps_environment_workdir_and_commands(tmp_path: Path) -> None:
    """The Docker config keeps ``ENV`` (values may contain ``=``), ``WORKDIR``, entrypoint and command, and
    survives a save and load."""
    docker_config = {
        "Env": ["PATH=/usr/local/bin:/usr/bin", "GREETING=a=b"],
        "WorkingDir": "/app",
        "Entrypoint": ["docker-entrypoint.sh"],
        "Cmd": None,
    }
    config = ImageConfig.from_docker("postgres:16", docker_config)
    assert config.env == {"PATH": "/usr/local/bin:/usr/bin", "GREETING": "a=b"}
    assert (config.workdir, config.entrypoint, config.cmd) == ("/app", ("docker-entrypoint.sh",), ())
    config.save(tmp_path / IMAGE_CONFIG)
    assert ImageConfig.load(tmp_path / IMAGE_CONFIG) == config


def test_listing_shows_only_complete_images(tmp_path: Path) -> None:
    """An image whose disk is not written yet is not listed, so it cannot be picked."""
    for name in ("ready", "half-built"):
        image_dir(tmp_path, name).mkdir(parents=True)
        ImageConfig(source=f"{name}:1").save(image_dir(tmp_path, name) / IMAGE_CONFIG)
    (image_dir(tmp_path, "ready") / IMAGE_DISK).write_bytes(b"disk")
    assert [image.name for image in list_images(tmp_path)] == ["ready"]


def test_build_needs_exactly_one_source_and_the_tools_drive(tmp_path: Path) -> None:
    """A build needs exactly one of a reference or a Dockerfile, and fails early without the tools drive."""
    with pytest.raises(ImageError, match="either"):
        build_image(tmp_path)
    with pytest.raises(ImageError, match="either"):
        build_image(tmp_path, reference="postgres:16", dockerfile=tmp_path / "Dockerfile")
    with pytest.raises(ImageError, match="tools drive"):
        build_image(tmp_path, reference="postgres:16")


@pytest.mark.parametrize("name", ["../escape", "a/b", "MyApp", "-x"])
def test_bad_image_names_are_refused_before_anything_is_built(tmp_path: Path, name: str) -> None:
    """Names that escape the images folder or are not valid Docker tags are refused before Docker or the disk
    are touched."""
    (tmp_path / TOOLS_DISK).touch()
    with pytest.raises(ImageError, match="bad image name"):
        build_image(tmp_path, reference="postgres:16", name=name)
    assert not (tmp_path / "images").exists()


async def test_starting_a_machine_of_an_unbuilt_image_says_how_to_build_it(tmp_path: Path) -> None:
    """Starting a machine of a missing image, or of a name that climbs out of the images folder, fails with a
    hint to run ``chronobox build``."""
    for name in VM_FILES:
        (tmp_path / name).touch()
    backend = FirecrackerBackend(tmp_path / "vms", vm_dir=tmp_path)
    with pytest.raises(VMError, match="chronobox build"):
        await backend.start(MachineSpec(image="postgres-16"))
    with pytest.raises(VMError, match="chronobox build"):
        await backend.start(MachineSpec(image="../images/postgres-16"))
    await backend.cleanup()


def test_machines_use_the_default_image_unless_told_otherwise() -> None:
    """Without an image, machines use the default one, and the tools drive is a required VM file."""
    assert MachineSpec().image == DEFAULT_IMAGE
    assert TOOLS_DISK in VM_FILES
