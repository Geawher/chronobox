"""Script hand-off: a change script replayed on a fresh fork, saved with the patch when it works."""

import io
import shutil
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from chronobox.config import ProjectConfig, load_config
from chronobox.handoff import (
    HANDOFF_DIR,
    PATCH_FILE,
    REPORT_FILE,
    SCRIPT_FILE,
    _file_names,
    _script_command,
    handoff_tools,
    save_handoff,
)
from chronobox.project import ProjectBoot, machine_spec
from chronobox.vm import FirecrackerBackend
from chronobox.workspace import MAIN, Workspace

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "notes"
PSQL = "psql -h 127.0.0.1 -U postgres -tAc"
TITLE_COLUMNS = (
    f'{PSQL} "select count(*) from information_schema.columns '
    "where table_name = 'notes' and column_name = 'title'\""
)


def test_a_script_with_a_shebang_runs_with_its_interpreter_else_with_sh() -> None:
    """``#!/bin/bash`` scripts may use bash features, so they run by their own line; others with sh."""
    assert _script_command("#!/bin/bash\necho hi\n").endswith(f"/{SCRIPT_FILE}")
    assert _script_command("echo hi\n").startswith("sh ")


def test_file_names_are_relative_to_the_project_whatever_the_archive_prefix() -> None:
    """Archives from the machine name files ``./x`` and the baseline ``x``: both read the same, and
    folders are left out (only files can be deleted on a replay)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in ("./lib", "./lib/a.py", "b.py"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE if name == "./lib" else tarfile.REGTYPE
            tar.addfile(member, io.BytesIO(b""))
    assert _file_names(buffer.getvalue()) == {"lib/a.py", "b.py"}


def test_a_new_handoff_replaces_the_previous_one(tmp_path: Path) -> None:
    """The hand-off folder always holds one verified hand-off: the script (executable), the patch and
    the report; files of an older hand-off do not linger."""
    folder = save_handoff(tmp_path, "echo one", "diff --git a/x b/x\n", "report one")
    (folder / "stale.txt").write_text("old")
    folder = save_handoff(tmp_path, "echo two\n", "", "report two")
    assert folder == tmp_path / HANDOFF_DIR
    assert sorted(path.name for path in folder.iterdir()) == [SCRIPT_FILE, PATCH_FILE, REPORT_FILE]
    assert (folder / SCRIPT_FILE).read_text() == "echo two\n"
    assert (folder / SCRIPT_FILE).stat().st_mode & 0o111


@pytest.fixture
def project(tmp_path: Path) -> ProjectConfig:
    """A copy of the notes example, so the test can read its hand-off folder."""
    shutil.copytree(EXAMPLE, tmp_path / "notes")
    return load_config(tmp_path / "notes")


@pytest.fixture
async def started(project: ProjectConfig, tmp_path: Path) -> AsyncIterator[tuple[Workspace, ProjectBoot]]:
    """The notes project up on a fresh (not warm) machine, with forks."""
    backend = FirecrackerBackend(tmp_path / "vms")
    boot = ProjectBoot(backend, project, None)
    workspace = Workspace(backend, machine_spec(project), boot=boot)
    await workspace.start()
    yield workspace, boot
    await workspace.close()
    await backend.cleanup()


@pytest.mark.vm
async def test_a_verified_script_is_saved_with_the_patch_and_main_is_untouched(
    started: tuple[Workspace, ProjectBoot], project: ProjectConfig
) -> None:
    """The replay starts from the start checkpoint, gets main's project files (a new file, a deleted
    one), runs the script and the check; the hand-off then holds the script, a patch of those file
    changes and the report, while main's database never saw the script."""
    workspace, boot = started
    await workspace.run("echo '# notes' > NOTES.md && rm migrate.py")
    script = f"{PSQL} 'alter table notes add column title text' && test -f NOTES.md && ! test -e migrate.py\n"
    verify = handoff_tools(workspace, boot)["verify_script"]
    reply = await verify(script, check=f"{TITLE_COLUMNS} | grep -qx 1")
    assert "exit 0" in reply and "hand-off saved" in reply, reply
    folder = project.path / HANDOFF_DIR
    assert (folder / SCRIPT_FILE).read_text() == script
    patch = (folder / PATCH_FILE).read_text()
    assert "b/NOTES.md" in patch and "a/migrate.py" in patch
    assert "replay-1" in (folder / REPORT_FILE).read_text()
    _, main_columns = await workspace.machine(MAIN).run(TITLE_COLUMNS)
    assert main_columns.strip() == "0"
    assert "replay-1" in workspace.forks


@pytest.mark.vm
async def test_a_failing_script_or_check_is_reported_and_not_saved(
    started: tuple[Workspace, ProjectBoot], project: ProjectConfig
) -> None:
    """A script that fails, or one whose check fails, comes back with its exit code and output and
    leaves no hand-off, so the user never gets an unverified script."""
    workspace, boot = started
    verify = handoff_tools(workspace, boot)["verify_script"]
    failed = await verify("echo half done; exit 3")
    assert "exit 3" in failed and "half done" in failed and "not saved" in failed
    unchecked = await verify("true", check="test -f /nonexistent")
    assert "check `test -f /nonexistent`: exit 1" in unchecked and "not saved" in unchecked
    assert not (project.path / HANDOFF_DIR).exists()
    assert await verify("  ") == "error: the script is empty"
