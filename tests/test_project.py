"""Project files, warm-start keys, service commands and patches (no VM)."""

import fcntl
import io
import os
import subprocess
import tarfile
import time
from pathlib import Path, PurePosixPath

import pytest

from chronobox.config import Service, load_config
from chronobox.images import TOOLS_DISK, image_disk
from chronobox.project import (
    PARTIAL_SUFFIX,
    ProjectError,
    _export_destination,
    _git_diff,
    _project_paths,
    _prune_warm,
    is_excluded,
    local_edits,
    pack_project,
    project_entries,
    service_command,
    warm_key,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with source files, a git folder and excluded folders (``node_modules``, ``data/cache``)."""
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
    """Stand-in VM files: Firecracker, kernel, tools drive and default image (contents irrelevant)."""
    vm = tmp_path / "vm"
    image_disk(vm, "default").parent.mkdir(parents=True)
    image_disk(vm, "default").write_bytes(b"image")
    for name in ("firecracker", "vmlinux", TOOLS_DISK):
        (vm / name).write_bytes(name.encode())
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
    """A pattern without a slash matches any path component, one with a slash a path and what is under it."""
    patterns = ("node_modules", "data/cache", "*.pyc")
    assert is_excluded(PurePosixPath(relative), patterns) is excluded


def test_project_entries_skip_excluded_folders_and_git(project: Path) -> None:
    """What is copied in leaves out excluded folders and ``.git``, but keeps the rest of ``data``."""
    entries = {str(entry) for entry in project_entries(load_config(project))}
    assert entries == {"chronobox.toml", "main.py", "lib", "lib/util.py", "data", "data/seed.sql"}


def test_the_project_archive_holds_exactly_those_entries(project: Path) -> None:
    """The archive copied into the machine holds exactly the project entries, nothing more."""
    with tarfile.open(fileobj=io.BytesIO(pack_project(load_config(project)))) as tar:
        names = set(tar.getnames())
    assert names == {"chronobox.toml", "main.py", "lib", "lib/util.py", "data", "data/seed.sql"}


def test_warm_key_follows_project_files_but_not_excluded_ones(project: Path, vm_dir: Path) -> None:
    """Editing a copied file changes the warm key; editing excluded files or exporting does not, so those
    never cost a fresh boot."""
    config = load_config(project)
    first = warm_key(config, vm_dir)
    (project / "node_modules" / "x" / "index.js").write_text("changed")
    (project / ".chronobox" / "exports").mkdir(parents=True)
    assert warm_key(config, vm_dir) == first
    (project / "main.py").write_text("changed")
    assert warm_key(config, vm_dir) != first


@pytest.mark.parametrize("vm_file", ["firecracker", "vmlinux", TOOLS_DISK, "images/default/rootfs.ext4"])
def test_warm_key_follows_every_vm_file(project: Path, vm_dir: Path, vm_file: str) -> None:
    """A change to any VM file (Firecracker, kernel, tools drive, image) changes the warm key, so a snapshot
    is never restored on files it was not made with."""
    config = load_config(project)
    first = warm_key(config, vm_dir)
    os.utime(vm_dir / vm_file, ns=(0, 1))
    assert warm_key(config, vm_dir) != first


def test_a_service_loop_never_shows_the_service_command_in_its_own_command_line() -> None:
    """The restart loop runs from a script, so ``pkill -f`` on the service command kills the service and not
    the loop that restarts it."""
    service = Service(name="app", start="python3 app.py", env={"PORT": "8000"})
    command = service_command(service, "/src")
    assert command.endswith(
        "nohup sh /var/lib/chronobox/services/app.sh >> /var/log/chronobox/app.log 2>&1 &"
    )
    assert "while :" in command and "PORT=8000" in command and "cd /src" in command


def test_a_service_without_restart_runs_once() -> None:
    """``restart = false`` starts the service once, in its own workdir."""
    command = service_command(Service(name="job", start="./run", restart=False, workdir="/opt"), "/src")
    assert "while" not in command and "cd /opt" in command


@pytest.mark.parametrize(
    ("line", "renamed"),
    [
        ("diff --git a/host/app.py b/machine/app.py\n", "diff --git a/app.py b/app.py\n"),
        ("diff --git a/machine/new.py b/machine/new.py\n", "diff --git a/new.py b/new.py\n"),
        ("diff --git a/host/old.py b/host/old.py\n", "diff --git a/old.py b/old.py\n"),
        ("--- a/host/app.py\n", "--- a/app.py\n"),
        ("+++ b/machine/app.py\n", "+++ b/app.py\n"),
        ("+++ b/machine/host/x.py\n", "+++ b/host/x.py\n"),
        ("Binary files a/host/x.png and b/machine/x.png differ", "Binary files a/x.png and b/x.png differ"),
        ("+ path = 'a/host/x'\n", "+ path = 'a/host/x'\n"),
    ],
)
def test_patches_name_project_paths_but_leave_content_alone(line: str, renamed: str) -> None:
    """Patch headers name project paths instead of the scratch folders, while content lines and real folders
    called host are left alone."""
    assert _project_paths(line) == renamed


@pytest.mark.parametrize("path", ["/../../etc", "/src/../../x", "src/app.py", "../x"])
def test_export_refuses_paths_that_would_leave_the_exports_folder(tmp_path: Path, path: str) -> None:
    """Relative paths and paths with ``..`` are refused, so an export can never delete or overwrite files
    outside the exports folder."""
    with pytest.raises(ProjectError):
        _export_destination(tmp_path / "exports" / "main", path)


def test_export_places_machine_paths_under_the_exports_folder(tmp_path: Path) -> None:
    """A machine path lands at the same path under the exports folder."""
    exports = tmp_path / "exports" / "main"
    assert _export_destination(exports, "/src/app.py") == exports / "src" / "app.py"


def test_a_patch_with_binary_files_odd_names_and_links_applies_with_git(tmp_path: Path) -> None:
    """A patch with a binary file, non-ASCII and spaced names, and a symlink applies with ``git apply`` and
    reproduces the machine's files exactly."""
    host, machine = tmp_path / "host", tmp_path / "machine"
    for side in (host, machine):
        (side / "my host").mkdir(parents=True)
        (side / "café.txt").write_text("v1\n")
    (machine / "café.txt").write_text("v2\n")
    (machine / "my host" / "f.txt").write_text("spaces\n")
    (machine / "logo.bin").write_bytes(bytes(range(256)))
    (machine / "python").symlink_to("/usr/bin/python3")
    patch = _git_diff(tmp_path)
    subprocess.run(["git", "apply", "-"], cwd=host, input=patch, text=True, check=True)
    assert (host / "café.txt").read_text() == "v2\n"
    assert (host / "my host" / "f.txt").read_text() == "spaces\n"
    assert (host / "logo.bin").read_bytes() == bytes(range(256))
    assert os.readlink(host / "python") == "/usr/bin/python3"


def test_pruning_keeps_warm_starts_in_use_and_fresh_partial_ones(tmp_path: Path) -> None:
    """Pruning deletes unused warm starts and stale partial ones, but never one a session borrows or a partial
    one still being written."""
    project = tmp_path / "warm" / "project"
    keep, unused, in_use = project / "new", project / "old", project / "older"
    fresh_partial, stale_partial = project / f"a{PARTIAL_SUFFIX}", project / f"b{PARTIAL_SUFFIX}"
    for folder in (keep, unused, in_use, fresh_partial, stale_partial):
        folder.mkdir(parents=True)
    os.utime(stale_partial, (time.time() - 7200, time.time() - 7200))
    with (in_use / "in-use.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)  # what a session borrowing it holds
        _prune_warm(keep)
    assert sorted(entry.name for entry in project.iterdir()) == sorted(["new", "older", fresh_partial.name])


def test_a_patch_with_added_changed_and_deleted_files_applies_with_git(tmp_path: Path) -> None:
    """A patch that adds, changes and deletes files applies with ``git apply``."""
    host, machine = tmp_path / "host", tmp_path / "machine"
    for side in (host, machine):
        (side / "lib").mkdir(parents=True)
        (side / "keep.py").write_text("x = 1\n")
    (host / "old.py").write_text("gone\n")
    (host / "lib" / "util.py").write_text("a = 1\n")
    (machine / "lib" / "util.py").write_text("a = 2\n")
    (machine / "lib" / "new.py").write_text("new\n")
    patch = _git_diff(tmp_path)
    subprocess.run(["git", "apply", "-"], cwd=host, input=patch, text=True, check=True)
    assert not (host / "old.py").exists()
    assert (host / "lib" / "util.py").read_text() == "a = 2\n"
    assert (host / "lib" / "new.py").read_text() == "new\n"


def test_local_edits_are_new_changed_or_deleted_files_not_touched_ones(project: Path) -> None:
    """Between two archives of the project, a touched file is not an edit, a changed, new or
    deleted one is; the archive of edits carries the changed files with their folders."""
    config = load_config(project)
    copied = pack_project(config)
    os.utime(project / "main.py")
    (project / "src").mkdir(exist_ok=True)
    (project / "src" / "new.py").write_text("new")
    (project / "main.py").write_text("changed")
    (project / "data" / "seed.sql").unlink()
    archive, changed, deleted = local_edits(copied, pack_project(config))
    assert (changed, deleted) == (["main.py", "src/new.py"], ["data/seed.sql"])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        assert sorted(tar.getnames()) == ["main.py", "src", "src/new.py"]
    assert local_edits(copied, copied)[1:] == ([], [])
