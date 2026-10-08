"""Workspace behaviour on real Firecracker VMs."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from chronobox.vm import FirecrackerBackend, MachineSpec
from chronobox.workspace import Profile, Workspace, WorkspaceError

pytestmark = pytest.mark.vm


@pytest.fixture
async def backend(tmp_path: Path) -> AsyncIterator[FirecrackerBackend]:
    backend = FirecrackerBackend(tmp_path / "vms")
    yield backend
    await backend.cleanup()


async def start(backend: FirecrackerBackend, profile: Profile, *, guard: bool = False) -> Workspace:
    workspace = Workspace(backend, MachineSpec(mem_mib=256), profile=profile, guard=guard)
    await workspace.start()
    return workspace


async def test_rollback_restores_files_and_running_processes(backend: FirecrackerBackend) -> None:
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


async def test_alone_profile_has_no_checkpoints(backend: FirecrackerBackend) -> None:
    workspace = await start(backend, Profile.ALONE)
    with pytest.raises(WorkspaceError):
        await workspace.checkpoint("x")


async def test_discard_reports_to_the_parent_and_lessons_come_back(backend: FirecrackerBackend) -> None:
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
    workspace = await start(backend, Profile.ROLLBACK)
    seen: list[str] = []

    async def before() -> None:
        seen.append("before")

    async def after() -> None:
        seen.append("after")

    workspace.before_main_switch.append(before)
    workspace.after_main_switch.append(after)
    await workspace.checkpoint("c")
    await workspace.rollback("c")
    assert seen == ["before", "after"]
