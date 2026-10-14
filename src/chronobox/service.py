"""Serve one workspace on a unix socket, so the machine can outlive any agent session.

Front ends (the MCP server in ``--connect`` mode, benchmark harnesses) talk to the
service with one JSON line per request and get one JSON line back:

- ``{"op": "ping"}`` → ``{"ok": true}`` at once, even while the machine boots
- ``{"op": "call", "method": "run", "args": [...]}`` → ``{"result": "..."}``
- ``{"op": "status"}`` → ``{"ready": true, "error": null}`` once the machine booted
- ``{"op": "shutdown"}`` → stops serving; the owner then closes the workspace
- ``{"op": "put", "where": "main", "path": "/src", "size": N}`` + N bytes of tar → ``{"ok": true}``
- ``{"op": "get", "where": "main", "path": "/src/app.py"}`` → ``{"size": N}`` + N bytes of tar

Failures come back as ``{"error": "..."}``.

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

from chronobox.vm import Machine, VMError
from chronobox.workspace import MAIN, Workspace, WorkspaceError

STREAM_LIMIT = 2**24
TOOL_METHODS = frozenset({
    "run", "wait", "checkpoint", "list_checkpoints", "rollback",
    "fork", "compare", "changes", "promote", "discard",
})  # fmt: skip

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Method = Callable[..., Awaitable[str]]


class Op(StrEnum):
    PING = "ping"
    CALL = "call"
    STATUS = "status"
    SHUTDOWN = "shutdown"
    PUT = "put"
    GET = "get"


class ServiceError(Exception):
    """The service refused a request or could not be reached."""


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
        payload = b""
        try:
            request = json.loads(await reader.readline())
            if request["op"] == Op.PUT:
                reply = await self._put(request, await reader.readexactly(int(request["size"])))
            elif request["op"] == Op.GET:
                reply, payload = await self._get(request)
            else:
                reply = await self._answer(request)
        except (ValueError, KeyError, TypeError, asyncio.IncompleteReadError) as error:
            reply = {"error": f"bad request: {error!r}"}
        writer.write((json.dumps(reply, default=str) + "\n").encode() + payload)
        await writer.drain()
        writer.close()

    async def _put(self, request: dict[str, Any], archive: bytes) -> dict[str, Any]:
        try:
            await (await self._machine(request)).put(request["path"], archive)
        except (WorkspaceError, VMError) as error:
            return {"error": str(error)}
        return {"ok": True}

    async def _get(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        try:
            archive = await (await self._machine(request)).get(request["path"])
        except (WorkspaceError, VMError) as error:
            return {"error": str(error)}, b""
        return {"size": len(archive)}, archive

    async def _machine(self, request: dict[str, Any]) -> Machine:
        """The machine a transfer names (main by default), once the workspace has booted.

        Raises:
            WorkspaceError: If the machine failed to start or the fork does not exist.
        """
        await self.ready.wait()
        if self.error:
            raise WorkspaceError(f"machine failed to start: {self.error}")
        return self.workspace.machine(request.get("where", MAIN))

    async def _answer(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request["op"]
        if op == Op.CALL:
            return {"result": await self.call(request["method"], *request.get("args", []))}
        if op == Op.PING:
            return {"ok": True}
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


async def put_remote(socket_path: Path | str, where: str, path: str, archive: bytes) -> None:
    """Unpack a tar archive at ``path`` on a served machine (main or a fork).

    Raises:
        ServiceError: If the service is not reachable or the copy failed.
    """
    request = {"op": Op.PUT, "where": where, "path": path, "size": len(archive)}
    await _transfer(socket_path, request, archive)


async def get_remote(socket_path: Path | str, where: str, path: str) -> bytes:
    """A tar archive of ``path`` on a served machine (a folder's content, or one file).

    Raises:
        ServiceError: If the service is not reachable or the path does not exist.
    """
    return await _transfer(socket_path, {"op": Op.GET, "where": where, "path": path}, b"")


async def _transfer(socket_path: Path | str, request: dict[str, Any], archive: bytes) -> bytes:
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=STREAM_LIMIT)
        writer.write((json.dumps(request) + "\n").encode() + archive)
        await writer.drain()
        reply = json.loads(await reader.readline())
        data = await reader.readexactly(int(reply.get("size", 0)))
        writer.close()
    except (OSError, asyncio.IncompleteReadError) as error:
        raise ServiceError(f"the machine is not reachable ({error})") from error
    if "error" in reply:
        raise ServiceError(reply["error"])
    return data


async def is_serving(socket_path: Path) -> bool:
    """Whether a service answers on ``socket_path`` (a leftover socket file does not)."""
    try:
        reply = await request(socket_path, {"op": Op.PING})
    except (OSError, ValueError):
        return False
    return reply.get("ok") is True


async def call_remote(socket_path: Path | str, method: str, *args: Any) -> str:
    try:
        reply = await request(socket_path, {"op": Op.CALL, "method": method, "args": list(args)})
    except OSError as error:
        return f"error: the machine is not reachable ({error})"
    return reply.get("result") or f"error: {reply.get('error')}"
