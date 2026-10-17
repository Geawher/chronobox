"""Script hand-off: a change script replayed on a fresh fork with the project's checks, saved with the
patch when everything passes, and the earlier hand-off withdrawn when something does not."""

import io
import shutil
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from chronobox.config import HandoffSettings, ProjectConfig, load_config
from chronobox.handoff import (
    HANDOFF_DIR,
    PATCH_FILE,
    PRE_FILE,
    REPORT_FILE,
    ROLLBACK_FILE,
    SCRIPT_FILE,
    WITHDRAWN_DIR,
    Scripts,
    _file_names,
    _script_command,
    handoff_tools,
    save_handoff,
    withdraw_handoff,
)
from chronobox.project import ProjectBoot, machine_spec
from chronobox.service import Method
from chronobox.vm import FirecrackerBackend, MachineSpec
from chronobox.workspace import MAIN, Workspace

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "notes"
PSQL = "psql -h 127.0.0.1 -U postgres -tAc"
ADD_TITLE = f"{PSQL} 'alter table notes add column if not exists title text'"
ADD_TITLE_ONCE = f"{PSQL} 'alter table notes add column title text'"
HAS_TITLE = (
    f'{PSQL} "select count(*) from information_schema.columns '
    "where table_name = 'notes' and column_name = 'title'\" | grep -qx 1"
)
HEALTH = "curl -sf 127.0.0.1:8000/health"
RESTART_APP = f"pkill -f '^python3 app.py' && sleep 3 && {HEALTH}"


def test_a_script_with_a_shebang_runs_with_its_interpreter_else_with_sh() -> None:
    """``#!/bin/bash`` scripts may use bash features, so they run by their own line; others with sh."""
    assert _script_command("#!/bin/bash\necho hi\n").endswith(f"/{SCRIPT_FILE}")
    assert _script_command("echo hi\n", PRE_FILE) == f"sh /root/.chronobox/{PRE_FILE}"


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
    """The hand-off folder always holds one verified hand-off: the scripts (executable), the patch if
    there is one, and the report; files of an older hand-off do not linger."""
    folder = tmp_path / HANDOFF_DIR
    scripts = Scripts("echo one", pre="echo before", rollback="echo undo")
    save_handoff(folder, scripts, "diff --git a/x b/x\n", "report one")
    assert {PRE_FILE, ROLLBACK_FILE, PATCH_FILE} <= {path.name for path in folder.iterdir()}
    (folder / "stale.txt").write_text("old")
    save_handoff(folder, Scripts("echo two\n"), "", "report two")
    assert sorted(path.name for path in folder.iterdir()) == [SCRIPT_FILE, REPORT_FILE]
    assert (folder / SCRIPT_FILE).read_text() == "echo two\n"
    assert (folder / SCRIPT_FILE).stat().st_mode & 0o111


def test_a_withdrawn_handoff_moves_aside_and_replaces_the_last_withdrawn_one(tmp_path: Path) -> None:
    """After a failed replay the saved hand-off is moved to ``handoff-withdrawn`` (the user never takes
    it by mistake, but it is not lost); with nothing saved there is nothing to withdraw."""
    folder, withdrawn = tmp_path / HANDOFF_DIR, tmp_path / WITHDRAWN_DIR
    assert not withdraw_handoff(folder, withdrawn)
    save_handoff(folder, Scripts("echo one"), "", "report one")
    assert withdraw_handoff(folder, withdrawn)
    save_handoff(folder, Scripts("echo two"), "", "report two")
    assert withdraw_handoff(folder, withdrawn)
    assert not folder.exists()
    assert (withdrawn / SCRIPT_FILE).read_text() == "echo two\n"


@pytest.mark.vm
def test_without_a_project_the_handoff_needs_an_exports_folder(tmp_path: Path) -> None:
    """A machine without a project has nowhere to save hand-offs unless one is given."""
    workspace = Workspace(FirecrackerBackend(tmp_path / "vms"), MachineSpec())
    with pytest.raises(ValueError, match="exports folder"):
        handoff_tools(workspace, None)


def _copy(tmp_path: Path, *, handoff: bool) -> ProjectConfig:
    """A copy of the notes example, with or without its ``[handoff]`` table."""
    folder = tmp_path / "notes"
    shutil.copytree(EXAMPLE, folder)
    if not handoff:
        config = folder / "chronobox.toml"
        config.write_text(config.read_text().split("\n# What `verify_script` adds")[0] + "\n")
    return load_config(folder)


@pytest.fixture
def plain(tmp_path: Path) -> ProjectConfig:
    """The notes example without a ``[handoff]`` table: only the agent's check applies."""
    return _copy(tmp_path, handoff=False)


@pytest.fixture
def checked(tmp_path: Path) -> ProjectConfig:
    """The notes example with its ``[handoff]`` table: traffic and comparisons on every replay."""
    return _copy(tmp_path, handoff=True)


async def _start(project: ProjectConfig, tmp_path: Path) -> AsyncIterator[tuple[Workspace, Method]]:
    """The project up on a fresh (not warm) machine, with forks, and its ``verify_script``."""
    backend = FirecrackerBackend(tmp_path / "vms")
    boot = ProjectBoot(backend, project, None)
    workspace = Workspace(backend, machine_spec(project), boot=boot)
    await workspace.start()
    yield workspace, handoff_tools(workspace, boot)["verify_script"]
    await workspace.close()
    await backend.cleanup()


@pytest.fixture
async def plain_session(plain: ProjectConfig, tmp_path: Path) -> AsyncIterator[tuple[Workspace, Method]]:
    """``plain`` up, with its ``verify_script``."""
    async for session in _start(plain, tmp_path):
        yield session


@pytest.fixture
async def checked_session(checked: ProjectConfig, tmp_path: Path) -> AsyncIterator[tuple[Workspace, Method]]:
    """``checked`` up, with its ``verify_script``."""
    async for session in _start(checked, tmp_path):
        yield session


@pytest.mark.vm
async def test_a_verified_script_is_saved_with_the_patch_and_main_is_untouched(
    plain_session: tuple[Workspace, Method], plain: ProjectConfig
) -> None:
    """The replay starts from the start checkpoint, gets main's project files (a new file, a deleted
    one), runs the script twice and the check; the hand-off then holds the script, a patch of those
    file changes and the report, while main's database never saw the script."""
    workspace, verify = plain_session
    await workspace.run("echo '# notes' > NOTES.md && rm migrate.py")
    script = f"{ADD_TITLE} && test -f NOTES.md && ! test -e migrate.py\n"
    reply = await verify(script, check=HAS_TITLE)
    assert "passed" in reply and "(second run): exit 0" in reply and "hand-off saved" in reply, reply
    folder = plain.path / HANDOFF_DIR
    assert (folder / SCRIPT_FILE).read_text() == script
    patch = (folder / PATCH_FILE).read_text()
    assert "b/NOTES.md" in patch and "a/migrate.py" in patch
    assert "replay-1" in (folder / REPORT_FILE).read_text()
    code, _ = await workspace.machine(MAIN).run(HAS_TITLE)
    assert code != 0 and "replay-1" in workspace.forks


@pytest.mark.vm
async def test_failures_are_reported_not_saved_and_withdraw_the_saved_handoff(
    plain_session: tuple[Workspace, Method], plain: ProjectConfig
) -> None:
    """A failing script, a failing check, or a script that fails when run again comes back with its
    output and leaves no hand-off; a hand-off saved earlier is withdrawn. A check is required."""
    _, verify = plain_session
    assert "passed" in await verify(ADD_TITLE, check=HAS_TITLE)
    failed = await verify("echo half done; exit 3", check="true")
    assert "exit 3" in failed and "half done" in failed and "not saved" in failed, failed
    assert "withdrawn" in failed and not (plain.path / HANDOFF_DIR).exists()
    assert (plain.path / WITHDRAWN_DIR / SCRIPT_FILE).read_text().strip() == ADD_TITLE
    unchecked = await verify("true", check="test -f /nonexistent")
    assert "your check failed" in unchecked and "not saved" in unchecked
    assert "failed when run a second time" in await verify(ADD_TITLE_ONCE, check=HAS_TITLE)
    assert "second run" not in await verify(ADD_TITLE_ONCE, check=HAS_TITLE, twice=False)
    assert "a check is required" in await verify("true", check=" ")
    assert await verify("  ", check="true") == "error: the script is empty"


@pytest.mark.vm
async def test_the_project_traffic_and_comparisons_hold_the_replay_to_what_was_tested(
    checked_session: tuple[Workspace, Method],
) -> None:
    """With ``[handoff]``: traffic runs during the replay and must lose nothing; the schema and the
    API's fields must match the machine the agent tested on. A script that does what the agent did
    passes; one that misses a column fails on the comparison, with the difference shown."""
    workspace, verify = checked_session
    await workspace.run(ADD_TITLE)
    missed = await verify("true", check=HEALTH)
    assert "differs between main and the replay" in missed and "title" in missed, missed
    reply = await verify(ADD_TITLE, check=HAS_TITLE)
    assert "passed" in reply and "load_check: exit 0" in reply and "0 of them missing" in reply, reply


@pytest.mark.vm
async def test_a_service_restarted_on_main_must_be_restarted_by_the_script(
    plain_session: tuple[Workspace, Method],
) -> None:
    """The agent restarted the app on main (to load new code); a script that does not restart it
    leaves the old process serving on the replay, which fails; one that restarts it passes."""
    workspace, verify = plain_session
    await workspace.run(RESTART_APP)
    stale = await verify("true", check=HEALTH)
    assert "port 8000 is still served by the process started before" in stale, stale
    assert "passed" in await verify(RESTART_APP, check=HEALTH)


@pytest.mark.vm
async def test_local_edits_are_deployed_and_handed_over_with_code_from_local(
    plain_session: tuple[Workspace, Method], plain: ProjectConfig
) -> None:
    """``code_from='local'`` deploys the files the agent edited on the host (not synced to any machine)
    and hands over their patch."""
    _, verify = plain_session
    (plain.path / "NOTES.md").write_text("# local notes\n")
    reply = await verify("grep -q 'local notes' NOTES.md", check="true", code_from="local")
    assert "passed" in reply and "project files from local" in reply, reply
    assert "b/NOTES.md" in (plain.path / HANDOFF_DIR / PATCH_FILE).read_text()


@pytest.mark.vm
async def test_pre_and_rollback_scripts_are_replayed_in_their_place(
    plain_session: tuple[Workspace, Method], plain: ProjectConfig
) -> None:
    """``pre`` runs before the new files are deployed, ``rollback`` once the old ones are back; both
    are saved with the hand-off, and a rollback that fails fails the replay."""
    workspace, verify = plain_session
    await workspace.run("echo new > NEW.txt")
    pre = f"! test -e NEW.txt && {ADD_TITLE}"
    rollback = f"! test -e NEW.txt && {PSQL} 'alter table notes drop column if exists title'"
    reply = await verify("test -e NEW.txt", check=HAS_TITLE, pre=pre, rollback=rollback)
    assert "passed" in reply and f"{ROLLBACK_FILE}: exit 0" in reply, reply
    saved = {path.name for path in (plain.path / HANDOFF_DIR).iterdir()}
    assert {PRE_FILE, ROLLBACK_FILE, SCRIPT_FILE} <= saved
    assert f"{ROLLBACK_FILE} failed" in await verify("test -e NEW.txt", check="true", rollback="exit 4")


@pytest.mark.vm
async def test_a_machine_without_a_project_hands_over_the_script_alone(tmp_path: Path) -> None:
    """Without a project nothing is deployed and there is no patch: the replay runs the script and the
    check on a fork of the start, and the hand-off holds the script and the report."""
    backend = FirecrackerBackend(tmp_path / "vms")
    workspace = Workspace(backend, MachineSpec(setup=("echo old > /etc/app.conf",)))
    await workspace.start()
    try:
        tools = handoff_tools(workspace, None, settings=HandoffSettings(), exports=tmp_path / "out")
        reply = await tools["verify_script"](
            "sed -i s/old/new/ /etc/app.conf", check="grep -qx new /etc/app.conf"
        )
        assert "passed" in reply and "(no project changes)" not in reply, reply
        saved = sorted(path.name for path in (tmp_path / "out" / "handoff").iterdir())
        assert saved == [SCRIPT_FILE, REPORT_FILE]
    finally:
        await workspace.close()
        await backend.cleanup()
