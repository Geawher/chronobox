"""Machines of other images on real Firecracker VMs: they boot, keep the image's environment,
and checkpoint, roll back and fork with their services running.

Build the images first (each test is skipped otherwise):

    chronobox build --image postgres:16
    chronobox build --image node:22-alpine
    chronobox build --image ruby:3.3
"""

import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from chronobox.images import DEFAULT_IMAGE, TOOLS_DISK, image_disk
from chronobox.vm import FirecrackerBackend, MachineSpec, default_vm_dir
from chronobox.workspace import MAIN, Profile, Workspace

pytestmark = pytest.mark.vm

POSTGRES = "postgres-16"
NODE = "node-22-alpine"
RUBY = "ruby-3.3"
PSQL = "psql -h 127.0.0.1 -U postgres -tAc"
POSTGRES_START = (
    "POSTGRES_HOST_AUTH_METHOD=trust nohup docker-entrypoint.sh postgres >/tmp/postgres.log 2>&1 &"
    " for _ in $(seq 120); do pg_isready -q -h 127.0.0.1 && break; sleep 0.5; done; pg_isready -h 127.0.0.1"
)
ORDER_COLUMNS = (
    f"{PSQL} \"select string_agg(column_name, ',' order by column_name)"
    " from information_schema.columns where table_name = 'orders'\""
)
NODE_SERVER = "require('http').createServer((q, r) => r.end('{reply} ' + process.version)).listen(8080)"
NODE_START = (
    'nohup node -e "{server}" >/tmp/node.log 2>&1 &'
    " for _ in $(seq 60); do wget -qO- 127.0.0.1:8080 >/dev/null 2>&1 && break; sleep 0.25; done"
)
NODE_GET = "wget -qO- 127.0.0.1:8080"


def requires_image(name: str) -> pytest.MarkDecorator:
    """Skip a test unless the named image is built in the VM files."""
    built = image_disk(default_vm_dir(), name).exists()
    return pytest.mark.skipif(not built, reason=f"image {name} not built (chronobox build --image ...)")


@pytest.fixture
async def backend(tmp_path: Path) -> AsyncIterator[FirecrackerBackend]:
    """A backend whose VMs and snapshots are deleted after the test."""
    backend = FirecrackerBackend(tmp_path / "vms")
    yield backend
    await backend.cleanup()


async def start(backend: FirecrackerBackend, image: str) -> Workspace:
    """A started workspace on a machine of ``image``, with every chronobox tool."""
    workspace = Workspace(backend, MachineSpec(image=image, mem_mib=512), profile=Profile.CHRONOBOX)
    await workspace.start()
    return workspace


async def output(workspace: Workspace, command: str, where: str = MAIN) -> str:
    """A read-only command's output, straight from the machine (no workspace markers)."""
    machine = workspace.main if where == MAIN else workspace.forks[where].machine
    assert machine is not None
    _, text = await machine.run(command)
    return text.strip()


def start_node(reply: str) -> str:
    """The command that starts the Node test server answering ``reply`` and waits until it serves."""
    return NODE_START.format(server=NODE_SERVER.format(reply=reply))


@requires_image(POSTGRES)
async def test_postgres_image_rolls_back_a_running_database(backend: FirecrackerBackend) -> None:
    """On the postgres image, a rollback undoes a delete and the database server is back running, without a
    restart."""
    workspace = await start(backend, POSTGRES)
    assert "accepting connections" in await workspace.run(POSTGRES_START)
    await workspace.run(f"{PSQL} \"create table orders (item text); insert into orders values ('kept')\"")
    await workspace.checkpoint("before-cleanup")
    await workspace.run(f'{PSQL} "delete from orders"')
    assert await output(workspace, f'{PSQL} "select count(*) from orders"') == "0"
    await workspace.rollback("before-cleanup")
    # The server process came back with the machine: no restart, it answers at once.
    assert await output(workspace, f'{PSQL} "select item from orders"') == "kept"


@requires_image(POSTGRES)
async def test_postgres_fork_changes_only_the_copy(backend: FirecrackerBackend) -> None:
    """A schema change on a fork of a running database leaves main's schema as it was."""
    workspace = await start(backend, POSTGRES)
    await workspace.run(POSTGRES_START)
    await workspace.run(f"{PSQL} \"create table orders (item text); insert into orders values ('a')\"")
    await workspace.fork("migration")
    await workspace.run(f'{PSQL} "alter table orders add column paid boolean"', where="migration")
    assert await output(workspace, ORDER_COLUMNS, where="migration") == "item,paid"
    assert await output(workspace, ORDER_COLUMNS) == "item"


@requires_image(NODE)
async def test_alpine_image_without_bash_or_python_runs_commands_in_sh(backend: FirecrackerBackend) -> None:
    """An image with neither bash nor Python runs commands in its busybox sh, with its own environment;
    ``cd``/``export`` persist, and a multi-line command never runs again from the saved state."""
    workspace = await start(backend, NODE)
    assert await output(workspace, "ls /bin/bash /usr/bin/python3 2>&1 | grep -c 'No such file'") == "2"
    assert await output(workspace, "readlink /proc/$$/exe") == "/bin/busybox"
    await workspace.run("cd /srv && export GREETING=hello")
    assert await output(workspace, 'echo "$(pwd) $GREETING"') == "/srv hello"
    assert await output(workspace, "node --version") == "v" + await output(workspace, "echo $NODE_VERSION")
    # sh prints a multi-line value over several lines: the saved state must still hold only
    # exports, so nothing of this command runs again and later commands keep working.
    await workspace.run("echo once >> /tmp/runs\nexport LINES_VAR='a\nb'")
    state = await output(workspace, 'cat /tmp/runs; echo "$LINES_VAR" | wc -l; echo $GREETING')
    assert state == "once\n2\nhello"


@requires_image(NODE)
async def test_node_fork_serves_a_different_app_while_main_keeps_serving(backend: FirecrackerBackend) -> None:
    """A fork can replace the running app while main keeps serving the old one."""
    workspace = await start(backend, NODE)
    await workspace.run(start_node("v1"))
    await workspace.fork("next")
    await workspace.run(f"pkill node; {start_node('v2')}", where="next")
    assert (await output(workspace, NODE_GET)).startswith("v1 ")
    assert (await output(workspace, NODE_GET, where="next")).startswith("v2 ")


@requires_image(NODE)
async def test_isolated_commands_work_in_an_image_without_bash(backend: FirecrackerBackend) -> None:
    """Isolated probes run in sh images too, and their mounts stay private."""
    workspace = await start(backend, NODE)
    assert workspace.main is not None
    code, text = await workspace.main.run("mount -t tmpfs tmpfs /mnt && echo private", isolated=True)
    assert (code, text.strip()) == (0, "private")
    # The probe's mount stayed in its own namespace.
    assert "on /mnt" not in await output(workspace, "mount")


@requires_image(RUBY)
async def test_ruby_image_keeps_its_environment_and_rolls_back(backend: FirecrackerBackend) -> None:
    """The image's ``ENV`` (``GEM_HOME``) applies, and a rollback restores the app's file."""
    workspace = await start(backend, RUBY)
    assert (await output(workspace, "ruby -e 'puts RUBY_VERSION'")).startswith("3.3")
    assert await output(workspace, "echo $GEM_HOME") == "/usr/local/bundle"
    await workspace.run("echo 'puts :before' > /root/app.rb")
    await workspace.checkpoint("app-v1")
    await workspace.run("echo 'puts :after' > /root/app.rb")
    await workspace.rollback("app-v1")
    assert await output(workspace, "ruby /root/app.rb") == "before"


async def test_a_rebuilt_image_does_not_change_the_disk_under_a_saved_machine(tmp_path: Path) -> None:
    """A checkpoint restores onto the disk it was taken on, even after the image was rebuilt and its file
    deleted (machines and snapshots hold hard links to their disks)."""
    # VM files and machines of our own, on the filesystem of the real VM files: disks are kept
    # by hard links, which only work there (elsewhere machines fall back to symlinks).
    real = default_vm_dir()
    vm_dir = real.parent / f".test-vm-{tmp_path.name}"
    (vm_dir / "images" / "copy").mkdir(parents=True)
    try:
        for name in ("firecracker", "vmlinux", TOOLS_DISK):
            (vm_dir / name).symlink_to(real / name)
        os.link(image_disk(real, DEFAULT_IMAGE), image_disk(vm_dir, "copy"))
        backend = FirecrackerBackend(vm_dir / "machines", vm_dir=vm_dir)
        try:
            workspace = Workspace(backend, MachineSpec(image="copy", mem_mib=512))
            await workspace.start()
            before = await output(workspace, "cat /etc/os-release | head -1")
            await workspace.checkpoint("saved")
            # Rebuilt (`chronobox build` replaces the file in one step), then even deleted: the
            # saved machine still has the disk it was taken on.
            os.link(image_disk(real, DEFAULT_IMAGE), vm_dir / "rebuilt.ext4")
            (vm_dir / "rebuilt.ext4").replace(image_disk(vm_dir, "copy"))
            image_disk(vm_dir, "copy").unlink()
            await workspace.rollback("saved")
            assert await output(workspace, "cat /etc/os-release | head -1") == before
        finally:
            await backend.cleanup()
    finally:
        shutil.rmtree(vm_dir)
