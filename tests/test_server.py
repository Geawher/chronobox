"""The MCP server over real stdio, in each of its modes."""

import asyncio
import inspect
import os
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from chronobox.handoff import handoff_tools
from chronobox.server import build_server
from chronobox.service import WorkspaceService, request
from chronobox.workspace import Profile

SERVER = [sys.executable, "-m", "chronobox.server"]
# The MCP client starts servers with a minimal environment; keep CHRONOBOX_VM_DIR and the rest.
SERVER_ENV = dict(os.environ)


async def text(session: ClientSession, tool: str, **arguments: object) -> str:
    """The text of a tool call's result, as the agent would read it."""
    result = await session.call_tool(tool, arguments)
    return "".join(getattr(part, "text", "") for part in result.content)


async def tool_names(session: ClientSession) -> set[str]:
    """The names of the tools the server offers."""
    return {tool.name for tool in (await session.list_tools()).tools}


async def test_profiles_expose_only_their_tools() -> None:
    """Each profile offers exactly its tools: the profile is enforced by what exists, not only described in
    the instructions."""

    async def call(method: str, *args: object) -> str:
        """Stand-in for the workspace: tool listing never calls it."""
        return method

    expected = {
        Profile.ALONE: {"run", "wait"},
        Profile.ROLLBACK: {"run", "wait", "checkpoint", "list_checkpoints", "rollback"},
        Profile.CHRONOBOX: {
            "run", "wait", "checkpoint", "list_checkpoints", "rollback",
            "fork", "compare", "changes", "promote", "discard",
        },
    }  # fmt: skip
    for profile, names in expected.items():
        server = build_server(profile, call)
        assert {tool.name for tool in await server.list_tools()} == names
    with_handoff = build_server(Profile.CHRONOBOX, call, handoff=True)
    handoff_tools = {tool.name for tool in await with_handoff.list_tools()}
    assert handoff_tools == expected[Profile.CHRONOBOX] | {"verify_script", "verify_result"}
    assert "verify_script" in (with_handoff.instructions or "")
    alone = build_server(Profile.ALONE, call, handoff=True)
    assert {tool.name for tool in await alone.list_tools()} == expected[Profile.ALONE]


async def test_verify_script_forwards_every_argument_in_order(tmp_path: Path) -> None:
    """The MCP tool passes its arguments positionally to the hand-off method, which takes them in the
    same order: a new argument in one place only would shift the others."""
    calls: list[tuple[object, ...]] = []

    async def call(method: str, *args: object) -> str:
        """Record the forwarded call."""
        calls.append((method, *args))
        return "ok"

    server = build_server(Profile.CHRONOBOX, call, handoff=True)
    await server.call_tool("verify_script", {"script": "s", "check": "c", "load": "l", "background": True})
    await server.call_tool("verify_result", {"wait": 5})
    assert calls == [
        ("verify_script", "s", "c", "", "", "", "l", "", "", True, True, "main", "start", 900),
        ("verify_result", "", 5),
    ]
    methods = handoff_tools(None, None, exports=tmp_path)  # type: ignore[arg-type]
    for name in ("verify_script", "verify_result"):
        tool = server._tool_manager.get_tool(name)
        assert list(inspect.signature(methods[name]).parameters) == list(tool.parameters["properties"])


@pytest.mark.vm
async def test_local_mode_forks_and_promotes(tmp_path: Path) -> None:
    """Over real stdio in local mode, an agent can fork, change the fork, compare and promote it, and main
    then has the fork's state."""
    params = StdioServerParameters(
        command=SERVER[0], args=[*SERVER[1:], "--root", str(tmp_path / "vms")], env=SERVER_ENV
    )
    async with stdio_client(params) as streams, ClientSession(*streams) as session:
        await session.initialize()
        assert "v1" in await text(session, "run", command="echo v1 | tee /root/f")
        assert not (await text(session, "fork", name="try")).startswith("error")
        await text(session, "run", command="echo v2 | tee /root/f", where="try")
        compared = await text(session, "compare", command="cat /root/f")
        assert "v1" in compared and "v2" in compared
        await text(session, "promote", name="try")
        assert (await text(session, "run", command="cat /root/f")).startswith("v2")


@pytest.mark.vm
async def test_served_machine_outlives_its_front_end(tmp_path: Path) -> None:
    """A ``--serve`` machine keeps its state after a ``--connect`` front end exits, and the front end offers
    only its profile's tools (no fork under rollback)."""
    socket = tmp_path / "machine.sock"
    serving = await asyncio.create_subprocess_exec(
        *SERVER, "--serve", str(socket), "--profile", "rollback", "--root", str(tmp_path / "vms")
    )
    try:
        while not socket.exists():
            await asyncio.sleep(0.2)
        assert (await request(socket, {"op": "status"}))["error"] is None
        params = StdioServerParameters(
            command=SERVER[0],
            args=[*SERVER[1:], "--connect", str(socket), "--profile", "rollback"],
            env=SERVER_ENV,
        )
        async with stdio_client(params) as streams, ClientSession(*streams) as session:
            await session.initialize()
            assert "fork" not in await tool_names(session)
            await text(session, "run", command="echo kept > /root/f")
            await text(session, "checkpoint", label="before")
            await text(session, "run", command="rm /root/f")
            await text(session, "rollback", label="before", lesson="keep /root/f")
        reply = await request(socket, {"op": "call", "method": "run", "args": ["cat /root/f"]})
        assert reply["result"].startswith("kept")
    finally:
        await request(socket, {"op": "shutdown"})
        await asyncio.wait_for(serving.wait(), 60)


@pytest.mark.vm
async def test_setup_script_prepares_the_machine(tmp_path: Path) -> None:
    """A ``--setup`` script runs in the machine before the agent's first command."""
    setup = tmp_path / "setup.sh"
    setup.write_text("echo ready > /root/prepared\n")
    params = StdioServerParameters(
        command=SERVER[0],
        args=[*SERVER[1:], "--profile", "alone", "--setup", str(setup), "--root", str(tmp_path / "vms")],
        env=SERVER_ENV,
    )
    async with stdio_client(params) as streams, ClientSession(*streams) as session:
        await session.initialize()
        assert (await text(session, "run", command="cat /root/prepared")).startswith("ready")


async def test_service_rejects_unknown_tools() -> None:
    """Calling a method that is not a tool returns an error instead of reaching the workspace."""
    service = WorkspaceService(workspace=None)  # type: ignore[arg-type]
    assert (await service.call("rm_everything")).startswith("error: unknown tool")
