"""Project files, warm-start keys, service commands and patches (no VM)."""

import io
import os
import tarfile
from pathlib import Path, PurePosixPath

import pytest

from chronobox.config import Service, load_config
from chronobox.images import TOOLS_DISK, image_disk
from chronobox.project import (
    _project_paths,
    is_excluded,
    pack_project,
    project_entries,
    service_command,
    warm_key,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    for relative in ("chronobox.toml", "main.py", "lib/util.py", "node_modules/x/index.js", ".git/HEAD"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(relative)
    (root / "chronobox.toml").write_text('[project]\nexclude = ["node_modules", "data/cache"]\n')
    (root / "data" / "cache").mkdir(parents=True)
    (root / "data" / "cache" / "big.bin").write_text("x")
    (root / "data" / "seed.sql").write_text("select 1;")
    return root


@pytest.fixture
def vm_dir(tmp_path: Path) -> Path:
    vm = tmp_path / "vm"
    image_disk(vm, "default").parent.mkdir(parents=True)
    image_disk(vm, "default").write_bytes(b"image")
    (vm / TOOLS_DISK).write_bytes(b"tools")
    return vm


@pytest.mark.parametrize(
    ("relative", "excluded"),
    [
        ("node_modules", True),
        ("web/node_modules/react/index.js", True),
        ("data/cache/big.bin", True),
        ("data/cache", True),
        ("data/seed.sql", False),
        ("cache/x", False),
        ("main.pyc", True),
        ("main.py", False),
    ],
)
def test_exclusion_patterns(relative: str, excluded: bool) -> None:
    patterns = ("node_modules", "data/cache", "*.pyc")
    assert is_excluded(PurePosixPath(relative), patterns) is excluded


def test_project_entries_skip_excluded_folders_and_git(project: Path) -> None:
    entries = {str(entry) for entry in project_entries(load_config(project))}
    assert entries == {"chronobox.toml", "main.py", "lib", "lib/util.py", "data", "data/seed.sql"}


def test_the_project_archive_holds_exactly_those_entries(project: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(pack_project(load_config(project)))) as tar:
        names = set(tar.getnames())
    assert names == {"chronobox.toml", "main.py", "lib", "lib/util.py", "data", "data/seed.sql"}


def test_warm_key_follows_project_files_but_not_excluded_ones(project: Path, vm_dir: Path) -> None:
    config = load_config(project)
    first = warm_key(config, vm_dir)
    (project / "node_modules" / "x" / "index.js").write_text("changed")
    (project / ".chronobox" / "exports").mkdir(parents=True)
    assert warm_key(config, vm_dir) == first
    (project / "main.py").write_text("changed")
    assert warm_key(config, vm_dir) != first


def test_warm_key_follows_the_image_and_the_tools_drive(project: Path, vm_dir: Path) -> None:
    config = load_config(project)
    first = warm_key(config, vm_dir)
    os.utime(vm_dir / TOOLS_DISK, ns=(0, 1))
    assert warm_key(config, vm_dir) != first


def test_a_service_loop_never_shows_the_service_command_in_its_own_command_line() -> None:
    service = Service(name="app", start="python3 app.py", env={"PORT": "8000"})
    command = service_command(service, "/src")
    assert command.endswith(
        "nohup sh /var/lib/chronobox/services/app.sh >> /var/log/chronobox/app.log 2>&1 &"
    )
    assert "while :" in command and "PORT=8000" in command and "cd /src" in command


def test_a_service_without_restart_runs_once() -> None:
    command = service_command(Service(name="job", start="./run", restart=False, workdir="/opt"), "/src")
    assert "while" not in command and "cd /opt" in command


def test_patches_name_project_paths_but_leave_content_alone() -> None:
    assert _project_paths("diff --git a/host/app.py b/machine/app.py\n") == "diff --git a/app.py b/app.py\n"
    assert _project_paths("--- a/host/app.py\n") == "--- a/app.py\n"
    assert _project_paths("+++ b/machine/app.py\n") == "+++ b/app.py\n"
    assert _project_paths("+ path = 'a/host/x'\n") == "+ path = 'a/host/x'\n"
