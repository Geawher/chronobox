"""Script hand-off: a change script replayed on a fresh fork with the project's checks, saved with the
patch when everything passes, and the earlier hand-off withdrawn when something does not."""

import io
import re
import shutil
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from chronobox import handoff
from chronobox.config import HandoffSettings, ProjectConfig, load_config
from chronobox.handoff import (
    ASSUME_FILE,
    HANDOFF_DIR,
    PATCH_FILE,
    PRE_FILE,
    REPORT_FILE,
    ROLLBACK_FILE,
    SCRIPT_FILE,
    WITHDRAWN_DIR,
    Options,
    Replay,
    Scripts,
    Step,
    _file_names,
    _slow_hint,
    bash_features,
    difference,
    handoff_tools,
    how_to_run,
    runnable,
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


def test_a_script_runs_by_its_own_shebang_and_gets_sh_without_one() -> None:
    """Scripts are saved and run as ``./apply.sh``: a ``#!`` line is kept, a script without one gets
    ``#!/bin/sh``, so the replay and the user run it with the same shell."""
    assert runnable("#!/bin/bash\necho hi") == "#!/bin/bash\necho hi\n"
    assert runnable("\necho hi\n") == "#!/bin/sh\necho hi\n"


def test_bash_features_are_flagged_only_in_scripts_that_run_with_sh() -> None:
    """``{1..3}`` does not expand in dash: a sh script using it is flagged, a bash script is not."""
    loop = "for i in {1..3}; do echo $i; done\n"
    assert bash_features(loop) == ["{1..N} ranges"]
    assert bash_features(f"#!/usr/bin/env sh\n[[ -f x ]] && {loop}") == ["[[ ... ]]", "{1..N} ranges"]
    assert bash_features(f"#!/bin/bash\n{loop}") == []
    assert bash_features("echo plain\n") == []


def test_a_difference_says_which_side_lacks_what() -> None:
    """A comparison that differs names what only the agent's machine has (the script misses it) and
    what only the replay has (the agent's machine lacks it), with what to do about each."""
    text = difference("pg_dump -s", MAIN, "a\nb\ntitle text\n", "a\nb\ncreated_at\n")
    assert "only main has" in text and "title text" in text and "add it to the script" in text
    assert "only the replay has" in text and "created_at" in text and "on main too" in text
    assert "only the replay" not in difference("x", MAIN, "a\nb", "a")


def test_how_to_run_lists_the_scripts_in_order() -> None:
    """The user runs assume, pre, the patch, then the script; rollback is how to undo."""
    scripts = Scripts("echo go", pre="echo pre", rollback="echo undo", assume="true")
    text = how_to_run(scripts, True, "/src")
    order = [text.index(part) for part in (ASSUME_FILE, PRE_FILE, PATCH_FILE, f"./{SCRIPT_FILE}")]
    assert order == sorted(order) and "/src" in text and ROLLBACK_FILE in text
    assert PATCH_FILE not in how_to_run(Scripts("echo go"), False, "/root")


def test_a_passed_replay_starts_with_what_it_showed() -> None:
    """The reviewer reads first what the replay showed: runs, retries, traffic and checks passed, and
    what was not checked; a failed replay has no such summary."""
    replay = Replay("replay-1", "start", MAIN)
    replay.steps = [
        Step("load `bin/traffic` (started)", 0, "", 0.1),
        Step(SCRIPT_FILE, 0, "", 12.0),
        Step(f"{SCRIPT_FILE} (second run)", 0, "", 0.4),
        Step(f"[killed 9 s in and run again] {SCRIPT_FILE} (run again)", 0, "", 5.0),
        Step("check `curl -sf /new`", 0, "", 0.2),
        Step("load_check `bin/traffic --report`", 0, "", 0.1),
    ]
    summary = replay.report().split("--- what this replay showed\n")[1].split("\n---")[0]
    assert summary.splitlines() == [
        "- traffic during the script: load `bin/traffic`",
        f"- ./{SCRIPT_FILE} finished in 12.0 s",
        "- run again right after, it finished in 0.4 s",
        "- killed 9 s in and run again: it then finished the job",
        "- passed: check `curl -sf /new`",
        "- passed: load_check `bin/traffic --report`",
    ], summary
    replay.steps = replay.steps[1:3]
    assert "not checked: what users saw while the script ran" in replay.report()
    replay.problems.append("your check failed")
    assert "what this replay showed" not in replay.report()


def test_a_slow_failed_replay_suggests_checking_the_logic_on_a_sample() -> None:
    """Only a full-data replay whose script ran for minutes gets the hint."""
    options = Options("true", "", "", "", True, 900.0)
    slow, quick = Replay("replay-1", "start", MAIN), Replay("replay-2", "start", MAIN)
    slow.steps, quick.steps = [Step(SCRIPT_FILE, 1, "", 300.0)], [Step(SCRIPT_FILE, 1, "", 5.0)]
    assert "check the logic on a sample first" in _slow_hint(slow, options)
    assert _slow_hint(quick, options) == ""
    slow.sampled = True
    assert _slow_hint(slow, options) == ""


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
    assert (folder / SCRIPT_FILE).read_text() == "#!/bin/sh\necho two\n"
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
    assert (withdrawn / SCRIPT_FILE).read_text() == "#!/bin/sh\necho two\n"


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
    assert (folder / SCRIPT_FILE).read_text() == runnable(script)
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
    assert (plain.path / WITHDRAWN_DIR / SCRIPT_FILE).read_text() == runnable(ADD_TITLE)
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
    assert "differs between main and the replay" in missed and "only main has" in missed, missed
    assert "title" in missed
    reply = await verify(ADD_TITLE, check=HAS_TITLE)
    assert "passed" in reply and "load_check `python3 traffic.py --report`: exit 0" in reply, reply


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


@pytest.fixture
async def bare(tmp_path: Path) -> AsyncIterator[tuple[Workspace, dict[str, Method]]]:
    """A machine without a project (``/etc/app.conf`` says old), with its hand-off tools."""
    backend = FirecrackerBackend(tmp_path / "vms")
    workspace = Workspace(backend, MachineSpec(setup=("echo old > /etc/app.conf",)))
    await workspace.start()
    yield workspace, handoff_tools(workspace, None, settings=HandoffSettings(), exports=tmp_path / "out")
    await workspace.close()
    await backend.cleanup()


@pytest.mark.vm
async def test_a_machine_without_a_project_hands_over_the_script_alone(
    bare: tuple[Workspace, dict[str, Method]], tmp_path: Path
) -> None:
    """Without a project nothing is deployed and there is no patch: the replay runs the script and the
    check on a fork of the start, and the hand-off holds the script and the report. Nothing watched
    users during the script, which the report says; a sh script using bash features is flagged."""
    _, tools = bare
    script = "sed -i s/old/new/ /etc/app.conf; for i in {1..2}; do :; done"
    reply = await tools["verify_script"](script, check="grep -qx new /etc/app.conf")
    assert "passed" in reply and "(no project changes)" not in reply, reply
    assert "nothing checked what users saw" in reply and "{1..N} ranges" in reply, reply
    assert "too soon to be killed midway" in reply
    saved = sorted(path.name for path in (tmp_path / "out" / "handoff").iterdir())
    assert saved == [SCRIPT_FILE, REPORT_FILE]


# Finishes in ~14 s; once started it refuses to run again unless it finished (not resumable).
FRAGILE = """[ -e /root/done ] && exit 0
[ -e /root/started ] && { echo "found a half-done job"; exit 5; }
touch /root/started; sleep 14; touch /root/done
"""
RESUMABLE = "[ -e /root/done ] && exit 0\ntouch /root/started; sleep 14; touch /root/done\n"


@pytest.mark.vm
async def test_a_script_killed_midway_must_finish_the_job_when_run_again(
    bare: tuple[Workspace, dict[str, Method]],
) -> None:
    """Copies of the replay taken while the script runs (at about 3 s and 10 s) kill it and run it
    again: a script that cannot pick up a half-done job fails there, although it passed when run
    again after it finished; the failing copy stays open. A script that resumes passes."""
    workspace, tools = bare
    fragile = await tools["verify_script"](FRAGILE, check="test -e /root/done")
    assert "(second run): exit 0" in fragile and "not saved" in fragile, fragile
    assert re.search(r"killed \d s in and run again \(on fork 'replay-1-crash-1', kept", fragile), fragile
    assert "found a half-done job" in fragile and "replay-1-crash-1" in workspace.forks
    resumable = await tools["verify_script"](RESUMABLE, check="test -e /root/done")
    retried = re.findall(r"\[killed \d+ s in and run again\] apply.sh \(run again\): exit 0", resumable)
    assert "passed" in resumable and len(retried) == 2, resumable
    assert not any("replay-2-crash" in fork for fork in workspace.forks)


@pytest.mark.vm
async def test_a_second_run_that_redoes_the_whole_job_fails(
    bare: tuple[Workspace, dict[str, Method]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run again once it ended, a script must be quick (here: within half its first run, at least
    2 s): one that redoes everything is stopped and told to skip finished work."""
    monkeypatch.setattr(handoff, "SECOND_RUN_MIN_S", 2.0)
    _, tools = bare
    reply = await tools["verify_script"]("sleep 6", check="true")
    assert "was still going after 3 s" in reply and "skip what is already done" in reply, reply


@pytest.mark.vm
async def test_assumptions_traffic_and_samples_are_part_of_the_replay(
    bare: tuple[Workspace, dict[str, Method]], tmp_path: Path
) -> None:
    """``assume`` runs first and is handed over (the user stops if it fails); ``load`` runs during the
    replay and ``load_check`` judges it after; ``sample`` runs before anything and is reported."""
    _, tools = bare
    verify = tools["verify_script"]
    load = "while true; do echo w >> /root/writes; sleep 0.2; done"
    reply = await verify(
        "sed -i s/old/new/ /etc/app.conf", check="grep -qx new /etc/app.conf",
        assume="grep -qx old /etc/app.conf || grep -qx new /etc/app.conf", load=load,
        load_check="test $(wc -l < /root/writes) -gt 3", sample="touch /root/sampled",
    )  # fmt: skip
    assert "passed" in reply and "ON A SAMPLE" in reply and "nothing checked" not in reply, reply
    assert f"./{ASSUME_FILE}" in reply.split("\n")[0]
    assert ASSUME_FILE in {path.name for path in (tmp_path / "out" / "handoff").iterdir()}
    unmet = await verify("true", check="true", assume="test -e /etc/production-only")
    assert f"{ASSUME_FILE} failed on the replay" in unmet, unmet
    lost = await verify("true", check="true", load=load, load_check="false")
    assert "your traffic saw a problem" in lost, lost
    own = await verify("true", check="true", load_check="false")
    assert "the machine's traffic saw a problem" in own, own


@pytest.mark.vm
async def test_a_replay_in_the_background_is_collected_with_verify_result(
    bare: tuple[Workspace, dict[str, Method]],
) -> None:
    """In the background the call returns at once; ``verify_result`` says what the replay is doing,
    then gives its result once, when it ended."""
    _, tools = bare
    started = await tools["verify_script"]("sleep 4", check="true", twice=False, background=True)
    assert "in the background on fork 'replay-1'" in started, started
    assert "still replaying on fork 'replay-1'" in await tools["verify_result"]()
    done = await tools["verify_result"]("replay-1", 120)
    assert "passed" in done and "hand-off saved" in done, done
    assert "no replay is waiting" in await tools["verify_result"]()


@pytest.mark.vm
async def test_a_long_replay_goes_on_in_the_background_after_the_foreground_wait(
    bare: tuple[Workspace, dict[str, Method]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify_script stops waiting after ``FOREGROUND_WAIT_S`` (1 s here) and says how to collect the
    replay, which goes on and is then given by ``verify_result``."""
    monkeypatch.setattr(handoff, "FOREGROUND_WAIT_S", 1.0)
    _, tools = bare
    reply = await tools["verify_script"]("sleep 4", check="true", twice=False)
    assert "still replaying after 1 s" in reply and "verify_result(fork='replay-1'" in reply, reply
    done = await tools["verify_result"]("replay-1", 120)
    assert "passed" in done and "hand-off saved" in done, done


@pytest.mark.vm
async def test_a_passed_handoff_is_reviewed_and_the_gaps_are_shown(tmp_path: Path) -> None:
    """The project's reviewer gets the goal, the scripts and the check on stdin; a gap it reports is
    shown and kept in replay.txt (the hand-off is saved anyway), an OK says no gap was found."""
    prompt = tmp_path / "prompt.txt"
    gaps = f"cat > {prompt}; echo 'new rows written after the script get no title'"
    backend = FirecrackerBackend(tmp_path / "vms")
    workspace = Workspace(backend, MachineSpec(setup=("echo old > /etc/app.conf",)))
    await workspace.start()
    try:
        for review, expected in (
            (gaps, "the reviewer thinks the check may miss"),
            ("cat >/dev/null; echo OK", "OK: no gap found"),
        ):
            settings = HandoffSettings(review=review)
            tools = handoff_tools(workspace, None, settings=settings, exports=tmp_path / "out")
            reply = await tools["verify_script"](
                "sed -i s/old/new/ /etc/app.conf", check="grep -qx new /etc/app.conf", twice=False,
                goal="switch the app to new",
            )  # fmt: skip
            assert expected in reply and "hand-off saved" in reply, reply
            assert "pass goal" in await tools["verify_script"]("true", check="true")
        asked = prompt.read_text()
        assert "switch the app to new" in asked and "grep -qx new /etc/app.conf" in asked
        assert "sed -i s/old/new/ /etc/app.conf" in asked
        report = (tmp_path / "out" / "handoff" / REPORT_FILE).read_text()
        assert "--- review (by `cat >/dev/null; echo OK`)" in report
    finally:
        await workspace.close()
        await backend.cleanup()
