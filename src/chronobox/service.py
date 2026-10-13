"""Serve one workspace on a unix socket, so the machine can outlive any agent session.

Front ends (the MCP server in ``--connect`` mode, benchmark harnesses) talk to the
service with one JSON line per request and get one JSON line back:

- ``{"op": "call", "method": "run", "args": [...]}`` → ``{"result": "..."}``
- ``{"op": "status"}`` → ``{"ready": true, "error": null}`` once the machine booted
- ``{"op": "shutdown"}`` → stops serving; the owner then closes the workspace

Harnesses can register extra operations (e.g. a benchmark's hidden checker), and extra
tool methods (e.g. a project's ``diff``) callable like the workspace's own.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from chronobox.vm import VMError
from chronobox.workspace import Workspace, WorkspaceError

STREAM_LIMIT = 2**24
TOOL_METHODS = frozenset({
    "run", "wait", "checkpoint", "list_checkpoints", "rollback",
    "fork", "compare", "changes", "promote", "discard",
})  # fmt: skip

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Method = Callable[..., Awaitable[str]]


class Op(StrEnum):
    CALL = "call"
    STATUS = "status"
    SHUTDOWN = "shutdown"


class WorkspaceService:
    """Boots a workspace in the background and answers requests about it."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.ready = asyncio.Event()
        self.stopped = asyncio.Event()
        self.error: str | None = None
        self.extra_ops: dict[str, Handler] = {}
        self.extra_methods: dict[str, Method] = {}

    async def boot(self, then: Callable[[], Awaitable[None]] | None = None) -> None:
        """Start the machine; failures are kept and reported to every caller."""
        try:
            await self.workspace.start()
            if then is not None:
                await then()
        except (VMError, OSError, RuntimeError) as error:
            self.error = repr(error)
        finally:
            self.ready.set()

    async def call(self, method: str, *args: Any) -> str:
        """Run one workspace tool; agent mistakes come back as ``error: ...`` text."""
        if method not in TOOL_METHODS and method not in self.extra_methods:
            return f"error: unknown tool {method!r}"
        await self.ready.wait()
        if self.error:
            return f"error: machine failed to start: {self.error}"
        try:
            if method in self.extra_methods:
                return await self.extra_methods[method](*args)
            result = getattr(self.workspace, method)(*args)
            return await result if asyncio.iscoroutine(result) else result
        except WorkspaceError as error:
            return f"error: {error}"

    async def serve(self, socket_path: Path) -> asyncio.Server:
        socket_path.unlink(missing_ok=True)
        return await asyncio.start_unix_server(self._handle, path=str(socket_path), limit=STREAM_LIMIT)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            reply = await self._answer(json.loads(await reader.readline()))
        except (ValueError, KeyError, TypeError) as error:
            reply = {"error": f"bad request: {error!r}"}
        writer.write((json.dumps(reply, default=str) + "\n").encode())
        await writer.drain()
        writer.close()

    async def _answer(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request["op"]
        if op == Op.CALL:
            return {"result": await self.call(request["method"], *request.get("args", []))}
        if op == Op.STATUS:
            await self.ready.wait()
            return {"ready": True, "error": self.error}
        if op == Op.SHUTDOWN:
            self.stopped.set()
            return {"ok": True}
        if op in self.extra_ops:
            return await self.extra_ops[op](request)
        return {"error": f"unknown op {op!r}"}


async def request(socket_path: Path | str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one request to a running service and return its reply."""
    reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=STREAM_LIMIT)
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()
    reply = json.loads(await reader.readline())
    writer.close()
    return reply


async def call_remote(socket_path: Path | str, method: str, *args: Any) -> str:
    try:
        reply = await request(socket_path, {"op": Op.CALL, "method": method, "args": list(args)})
    except OSError as error:
        return f"error: the machine is not reachable ({error})"
    return reply.get("result") or f"error: {reply.get('error')}"
