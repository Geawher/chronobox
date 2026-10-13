"""Projects on real Firecracker VMs: the example notes app (web app + PostgreSQL on the default
image) comes up from its chronobox.toml, starts warm the second time, survives a killed service,
and hands its changes back as a patch; files move in and out of an image without tar."""

import io
import os
import shutil
import sys
import tarfile
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from chronobox.config import ProjectConfig, load_config
from chronobox.images import image_disk
from chronobox.project import ProjectBoot, export, machine_spec, project_diff
from chronobox.vm import FirecrackerBackend, MachineSpec, VMError, default_vm_dir
from chronobox.workspace import MAIN, START, Workspace

pytestmark = pytest.mark.vm

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "notes"
NOTES = "curl -s 127.0.0.1:8000/notes"
WARM_START_LIMIT_S = 3.0
CLOCK_TOLERANCE_S = 5
NODE = "node-22-alpine"


@pytest.fixture
def project(tmp_path: Path) -> ProjectConfig:
    shutil.copytree(EXAMPLE, tmp_path / "notes")
    return load_config(tmp_path / "notes")


@pytest.fixture
async def backend(tmp_path: Path) -> AsyncIterator[FirecrackerBackend]:
    backend = FirecrackerBackend(tmp_path / "vms")
    yield backend
    await backend.cleanup()


async def output(workspace: Workspace, command: str, where: str = MAIN) -> str:
    _, text = await workspace.machine(where).run(command)
    return text.strip()


async def start(
    backend: FirecrackerBackend, project: ProjectConfig, warm_dir: Path | None
) -> tuple[Workspace, ProjectBoot]:
    boot = ProjectBoot(backend, project, warm_dir)
    workspace = Workspace(backend, machine_spec(project), boot=boot)
    await workspace.start()
    return workspace, boot


async def test_project_comes_up_then_starts_warm(project: ProjectConfig, tmp_path: Path) -> None:
    for session in ("first", "second"):
        backend = FirecrackerBackend(tmp_path / f"vms-{session}")
        try:
            started = time.monotonic()
            workspace, boot = await start(backend, project, tmp_path / "warm")
            seconds = time.monotonic() - started
            assert await output(workspace, "pwd") == "/src"
            assert "ship chronobox" in await output(workspace, NOTES)
            guest_clock = int(await output(workspace, "date +%s"))
            assert abs(guest_clock - time.time()) < CLOCK_TOLERANCE_S
            if session == "second":
                # The start checkpoint of a warm start is the warm snapshot (possibly days old):
                # rolling back to it brings the prepared project back, with the host's time.
                await workspace.run("rm -rf /src/*")
                await workspace.rollback(START)
                assert "ship chronobox" in await output(workspace, NOTES)
                assert abs(int(await output(workspace, "date +%s")) - time.time()) < CLOCK_TOLERANCE_S
        finally:
            await backend.cleanup()
        assert boot.was_warm is (session == "second")
    assert seconds < WARM_START_LIMIT_S


async def test_a_change_on_a_fork_comes_back_as_a_patch(
    project: ProjectConfig, backend: FirecrackerBackend, tmp_path: Path
) -> None:
    workspace, _ = await start(backend, project, tmp_path / "warm")
    await workspace.fork("newest-first")
    await workspace.run("sed -i 's/order by id\"/order by id desc\"/' app.py", where="newest-first")
    patch = await project_diff(workspace.machine("newest-first"), project)
    changed = [line for line in patch.splitlines() if line.startswith("+") and "order by" in line]
    assert "--- a/app.py" in patch and len(changed) == 1 and "order by id desc" in changed[0]
    assert await project_diff(workspace.machine(), project) == ""
    exported = await export(workspace.machine("newest-first"), project, "/src/app.py", "newest-first")
    assert exported == project.path / ".chronobox" / "exports" / "newest-first" / "src" / "app.py"
    assert "order by id desc" in exported.read_text()


async def test_a_killed_service_is_restarted_by_its_loop(
    project: ProjectConfig, backend: FirecrackerBackend, tmp_path: Path
) -> None:
    workspace, _ = await start(backend, project, None)
    await workspace.run("pkill -f 'python3 app.py'; sleep 3")
    assert "buy milk" in await output(workspace, NOTES)
    assert "app exited" in await output(workspace, "cat /var/log/chronobox/app.log")


async def test_a_service_that_never_gets_ready_fails_the_boot_with_its_log(
    project: ProjectConfig, backend: FirecrackerBackend
) -> None:
    db, app = project.services
    never_ready = replace(project, services=(db, replace(app, ready="false", ready_timeout=3.0)))
    with pytest.raises(VMError, match="service app not ready"):
        await ProjectBoot(backend, never_ready, None)()


@pytest.mark.skipif(not image_disk(default_vm_dir(), NODE).exists(), reason=f"image {NODE} not built")
async def test_files_move_in_and_out_of_an_image_without_tar(backend: FirecrackerBackend) -> None:
    machine = await backend.start(MachineSpec(image=NODE, mem_mib=256))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("hello.txt")
        info.size = 5
        tar.addfile(info, io.BytesIO(b"hello"))
    await machine.put("/data/in", buffer.getvalue())
    assert (await machine.run("command -v tar || echo none; cat /data/in/hello.txt"))[1].endswith("hello")
    # A link to an absolute path resolves inside the image, not on the tools drive.
    await machine.run("ln -s /etc /data/in/etc-link")
    with tarfile.open(fileobj=io.BytesIO(await machine.get("/data/in/etc-link/alpine-release"))) as tar:
        assert tar.getnames() == ["alpine-release"]
    with pytest.raises(VMError, match="No such file|FileNotFoundError"):
        await machine.get("/nowhere")


async def test_the_mcp_server_serves_a_project_with_diff_and_export(
    project: ProjectConfig, tmp_path: Path
) -> None:
    server = [sys.executable, "-m", "chronobox.server", "--project", str(project.path), "--no-warm"]
    params = StdioServerParameters(
        command=server[0], args=[*server[1:], "--root", str(tmp_path / "vms")], env=dict(os.environ)
    )
    async with stdio_client(params) as streams, ClientSession(*streams) as session:
        initialized = await session.initialize()
        assert "/src" in (initialized.instructions or "")
        tools = {tool.name for tool in (await session.list_tools()).tools}
        assert {"diff", "export"} <= tools
        result = await session.call_tool("diff", {})
        assert "no changes" in "".join(getattr(part, "text", "") for part in result.content)


POSTGRES = "postgres-16"
POSTGRES_PROJECT = """
image = "postgres-16"
setup = ["psql -h 127.0.0.1 -U postgres -f seed.sql"]

[[service]]
name = "db"
start = "docker-entrypoint.sh postgres"
ready = "pg_isready -q -h 127.0.0.1 -U postgres"
env = { POSTGRES_HOST_AUTH_METHOD = "trust" }
"""


@pytest.mark.skipif(not image_disk(default_vm_dir(), POSTGRES).exists(), reason=f"image {POSTGRES} not built")
async def test_a_project_on_the_postgres_image_starts_its_entrypoint_and_starts_warm(tmp_path: Path) -> None:
    folder = tmp_path / "orders-db"
    folder.mkdir()
    (folder / "chronobox.toml").write_text(POSTGRES_PROJECT)
    (folder / "seed.sql").write_text("create table orders (item text); insert into orders values ('kept');\n")
    project = load_config(folder)
    for session in ("first", "second"):
        backend = FirecrackerBackend(tmp_path / f"vms-{session}")
        try:
            workspace, boot = await start(backend, project, tmp_path / "warm")
            assert boot.was_warm is (session == "second")
            assert (
                await output(workspace, "psql -h 127.0.0.1 -U postgres -tAc 'select item from orders'")
                == "kept"
            )
        finally:
            await backend.cleanup()
