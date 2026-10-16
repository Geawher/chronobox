"""Workspace behaviour on real Firecracker VMs."""

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from chronobox.vm import FirecrackerBackend, MachineSpec
from chronobox.workspace import Profile, Workspace, WorkspaceError

pytestmark = pytest.mark.vm


@pytest.fixture
async def backend(tmp_path: Path) -> AsyncIterator[FirecrackerBackend]:
    """A backend whose VMs and snapshots are deleted after the test."""
    backend = FirecrackerBackend(tmp_path / "vms")
    yield backend
    await backend.cleanup()


async def start(backend: FirecrackerBackend, profile: Profile, *, guard: bool = False) -> Workspace:
    """A started workspace on a small machine of the default image."""
    workspace = Workspace(backend, MachineSpec(mem_mib=256), profile=profile, guard=guard)
    await workspace.start()
    return workspace


async def test_rollback_restores_files_and_running_processes(backend: FirecrackerBackend) -> None:
    """A rollback brings back deleted files and killed processes, and the lesson given with it shows in the
    checkpoint list."""
    workspace = await start(backend, Profile.ROLLBACK)
    await workspace.run("echo important > /root/data.txt; nohup sleep 900 >/dev/null 2>&1 &")
    await workspace.checkpoint("safe")
    await workspace.run("rm /root/data.txt; pkill sleep")
    assert "No such file" in await workspace.run("cat /root/data.txt")
    await workspace.rollback("safe", lesson="do not delete data.txt")
    assert "important" in await workspace.run("cat /root/data.txt")
    assert "1" in await workspace.run("pgrep -c sleep")
    assert "do not delete data.txt" in workspace.list_checkpoints()


async def test_fork_compare_promote_and_auto_checkpoint(backend: FirecrackerBackend) -> None:
    """A fork's change shows next to main's in ``compare``, promoting makes it main, and a risky command on
    main is checkpointed automatically first."""
    workspace = await start(backend, Profile.CHRONOBOX)
    await workspace.run("echo v1 > /root/version")
    await workspace.fork("try")
    await workspace.run("echo v2 > /root/version", where="try")
    compared = await workspace.compare("cat /root/version")
    assert "v1" in compared and "v2" in compared
    await workspace.promote("try")
    assert (await workspace.run("cat /root/version")).startswith("v2")
    output = await workspace.run("rm -f /root/version")
    assert "saved automatically" in output
    assert any(label.startswith("auto-") for label in workspace.checkpoints)


async def test_large_output_comes_back_shortened(backend: FirecrackerBackend) -> None:
    """Huge output is shortened in the middle, keeping its end and the exit code."""
    workspace = await start(backend, Profile.ALONE)
    output = await workspace.run("head -c 200000 /dev/zero | tr '\\0' a; echo; echo end")
    assert "characters omitted" in output and output.endswith("end\n[exit code 0]")


async def test_alone_profile_has_no_checkpoints(backend: FirecrackerBackend) -> None:
    """The alone profile refuses checkpoints, so it is a true no-undo baseline."""
    workspace = await start(backend, Profile.ALONE)
    with pytest.raises(WorkspaceError):
        await workspace.checkpoint("x")


async def test_discard_reports_to_the_parent_and_lessons_come_back(backend: FirecrackerBackend) -> None:
    """Discarding a fork reports the files and state-changing commands it ran, its failures, and that main is
    untouched; its lesson (or an automatic one) comes back on a later fork from the same point."""
    workspace = await start(backend, Profile.CHRONOBOX)
    await workspace.checkpoint("base")
    await workspace.fork("try", from_checkpoint="base")
    await workspace.run("mkdir -p /srv && echo v2 > /srv/app.conf", where="try")
    await workspace.run("cat /srv/app.conf", where="try")
    await workspace.run("false", where="try")
    report = await workspace.discard("try", lesson="v2 config needs a restart")
    assert "/srv/app.conf" in report
    assert "echo v2 > /srv/app.conf" in report
    assert "cat /srv/app.conf" not in report
    assert "main machine is unchanged" in report
    assert "1 command(s) failed" in report
    assert "v2 config needs a restart" in workspace.list_checkpoints()
    again = await workspace.fork("try2", from_checkpoint="base")
    assert "from this state" in again and "v2 config needs a restart" in again
    await workspace.run("false", where="try2")
    await workspace.discard("try2")
    assert any(lesson.text.startswith("(automatic)") for lesson in workspace.lessons)


async def test_guard_requires_a_rehearsal_on_a_fork(backend: FirecrackerBackend) -> None:
    """Under the guard, a destructive command on main is refused until it succeeded on a fork (or forced)."""
    workspace = await start(backend, Profile.CHRONOBOX, guard=True)
    await workspace.run("echo keep > /root/f")
    assert "[chronobox guard] not run" in await workspace.run("rm /root/f")
    assert "keep" in await workspace.run("cat /root/f")
    await workspace.fork("rehearse")
    await workspace.run("rm /root/f", where="rehearse")
    await workspace.discard("rehearse")
    await workspace.run("rm /root/f")
    assert "No such file" in await workspace.run("cat /root/f")
    await workspace.run("echo x > /root/g")
    assert "guard" not in await workspace.run("rm /root/g", force=True)


async def test_main_switch_listeners_fire_around_rollback(backend: FirecrackerBackend) -> None:
    """Harnesses are told before and after a rollback replaces the main machine (e.g. to move traffic)."""
    workspace = await start(backend, Profile.ROLLBACK)
    seen: list[str] = []

    async def before() -> None:
        """Records that the main machine is about to be replaced."""
        seen.append("before")

    async def after() -> None:
        """Records that the main machine was replaced."""
        seen.append("after")

    workspace.before_main_switch.append(before)
    workspace.after_main_switch.append(after)
    await workspace.checkpoint("c")
    await workspace.rollback("c")
    assert seen == ["before", "after"]


async def test_machines_work_under_a_long_root_directory(tmp_path: Path) -> None:
    """Machines and forks work under a deep folder, despite the length limit of the unix sockets inside it."""
    deep = tmp_path / ("very-long-directory-name-" * 4) / ("another-long-segment-" * 3)
    backend = FirecrackerBackend(deep)
    try:
        workspace = Workspace(backend, MachineSpec(mem_mib=256), profile=Profile.CHRONOBOX)
        await workspace.start()
        await workspace.fork("copy")
        assert (await workspace.run("echo hi", where="copy")).startswith("hi")
    finally:
        await backend.cleanup()


async def test_isolated_commands_run_alongside_and_stay_private(backend: FirecrackerBackend) -> None:
    """An isolated command runs while a shared one is busy, without the shared working folder or environment,
    and its mounts stay invisible to the shared shell."""
    machine = await backend.start(MachineSpec(mem_mib=256))
    await machine.run("cd /tmp && export SHARED=1")
    busy = asyncio.create_task(machine.run("sleep 3"))
    await asyncio.sleep(0.5)
    mount = "mkdir -p /srv/p && mount -t tmpfs none /srv/p && echo secret > /srv/p/f && cat /srv/p/f && pwd"
    started = time.monotonic()
    code, output = await machine.run(mount, 30, isolated=True)
    assert code == 0 and output.split() == ["secret", "/root"]
    assert time.monotonic() - started < 2, "isolated command waited for the busy one"
    await busy
    assert (await machine.run("ls -A /srv/p; pwd; echo ${SHARED:-unset}"))[1].split() == ["/tmp", "1"]
