"""chronobox MCP server: a Linux machine with undo, checkpoints and forks for coding agents.

Three ways to run it:

- **local** (default): owns a fresh VM and serves MCP over stdio — what you add to
  Claude Code::

      claude mcp add chronobox -- uv run --project /path/to/chronobox chronobox-mcp

  or, in a project with a ``chronobox.toml``, ``chronobox mcp --project .``;

- ``--serve SOCKET``: owns the VM and serves it on a unix socket, with no MCP of its
  own, so the machine outlives any agent session (benchmarks, long jobs);
- ``--connect SOCKET``: a thin MCP front end for a ``--serve`` process, over stdio or,
  with ``--http HOST:PORT``, over streamable HTTP (e.g. for an agent in a container).

The profile decides which tools exist (enforced here, not only described).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from chronobox.config import ConfigError, ProjectConfig, load_config
from chronobox.handoff import handoff_tools
from chronobox.images import DEFAULT_IMAGE
from chronobox.project import (
    ProjectBoot,
    default_warm_dir,
    project_note,
    project_tools,
    session_root,
    up_socket,
)
from chronobox.project import machine_spec as project_machine_spec
from chronobox.service import WorkspaceService, call_remote, is_serving
from chronobox.vm import FirecrackerBackend, MachineSpec
from chronobox.workspace import MAIN, Profile, Workspace

INSTRUCTIONS = {
    Profile.ALONE: (
        "You work on a Linux machine through the `run` tool (one shell command per call, as "
        "root). `wait` lets time pass while services keep running."
    ),
    Profile.ROLLBACK: (
        "You work on a Linux machine through `run`. You can save the whole machine (disk, "
        "memory, running processes) with `checkpoint` and restore it exactly with `rollback` "
        "if something goes wrong — use them before risky changes. A rollback also undoes what "
        "the machine's users did since the checkpoint (writes they were told had succeeded "
        "are lost), so roll back soon after a mistake, or fix forward instead."
    ),
    Profile.CHRONOBOX: (
        "You work on a Linux machine through `run`. Before a change whose effect you are not sure "
        "of (a config or tuning value, a migration, a cleanup, an upgrade, a restart), `fork` the "
        "whole machine (disk, memory, running processes, live traffic) into an independent copy and "
        "make the change there (run/wait with where=<fork>). Let time pass and check that it really "
        "works and that no data was lost or changed; several forks can try options side by side. "
        "Then make the verified change on the main machine and `discard` the forks. A rehearsal "
        "takes as long as the real thing: rehearse what you are unsure of, not what you know, and "
        "for a long operation try it on a sample or try only its risky steps. If users are failing "
        "now, or will be before a full rehearsal could end (an outage, a limit about to run out), "
        "first take a small step on the main machine that you know is safe and that stops or delays "
        "the damage, then rehearse the real fix. `checkpoint` / `rollback` restore the main machine "
        "exactly, and risky commands are checkpointed automatically, but a rollback also undoes "
        "what the machine's users did since the checkpoint (writes they were told had succeeded "
        "are lost): on a machine that takes writes, prefer fixing forward with a change verified "
        "on a fork. Discarding a fork returns a report of what it did; lessons you record are "
        "shown again whenever you fork or roll back."
    ),
}

# For a machine without a project, when hand-offs are on (--handoff-dir / --handoff).
HANDOFF_NOTE = (
    "When the user needs the change as a script to run on their real machine, check it with "
    "`verify_script`, which replays it on a fresh fork of the machine as it was before your work; "
    "what it saved is the hand-off."
)

Call = Callable[..., Awaitable[str]]


def build_server(
    profile: Profile,
    call: Call,
    *,
    guard: bool = False,
    project_note: str | None = None,
    handoff: bool = False,
) -> MCPServer:
    """MCP tools for a profile; every tool forwards to ``call(method, *args)``.

    With ``project_note`` (a project from ``chronobox.toml``), the agent is also told about the
    project and gets its ``diff``, ``export`` and ``sync`` tools (and ``verify_script`` with forks).
    ``handoff`` offers ``verify_script`` on a machine without a project (with forks).
    """
    instructions = INSTRUCTIONS[profile] + (f" {project_note}" if project_note else "")
    if handoff and not project_note and profile is Profile.CHRONOBOX:
        instructions += f" {HANDOFF_NOTE}"
    server = MCPServer("chronobox", instructions=instructions)

    async def run(command: str, where: str = MAIN) -> str:
        """Run one shell command (bash, as root) on the main machine or on a fork. The
        working directory and exported variables persist between calls."""
        return await call("run", command, where)

    async def run_guarded(command: str, where: str = MAIN, force: bool = False) -> str:
        """Run one shell command (bash, as root) on the main machine or on a fork. Risky
        commands on the main machine must first succeed on a fork (or set force=true)."""
        return await call("run", command, where, None, force)

    async def wait(seconds: float, where: str = MAIN) -> str:
        """Let time pass (1-600 s) on the main machine or a fork; services and traffic keep
        running, so you can see whether a change really holds."""
        return await call("wait", seconds, where)

    async def checkpoint(label: str, note: str = "") -> str:
        """Save the whole main machine (disk, memory, running processes) under a label."""
        return await call("checkpoint", label, note)

    async def list_checkpoints() -> str:
        """List checkpoints (with lessons attached to them) and forks."""
        return await call("list_checkpoints")

    async def rollback(label: str, lesson: str = "") -> str:
        """Restore the main machine exactly to a checkpoint; everything after it is undone,
        including work that services did since then (e.g. writes clients were told had
        succeeded, which are then lost). On a machine that keeps taking writes, the longer
        since the checkpoint, the more is lost: prefer fixing forward unless losing that
        work is acceptable. Record a lesson about what went wrong."""
        return await call("rollback", label, lesson)

    async def fork(name: str, from_checkpoint: str | None = None) -> str:
        """Create an independent, exact copy of the main machine (or of a checkpoint), live
        traffic included, to try something risky and watch its effect over time without
        touching the main machine. Fork before any change whose effect you are not sure of."""
        return await call("fork", name, from_checkpoint)

    async def compare(command: str, forks: list[str] | None = None) -> str:
        """Run the same read-only command on the main machine and on forks; show outputs."""
        return await call("compare", command, forks)

    async def changes(name: str) -> str:
        """Report what a fork did since it was created: state-changing commands, failures
        and files changed. Use it before deciding to apply, promote or discard."""
        return await call("changes", name)

    async def promote(name: str) -> str:
        """Make a fork the new main machine. Everything about the fork carries over,
        including anything your experiments broke or lost on it, and work done on the
        main machine since the fork is discarded. Often better: apply the change you
        verified on the fork to the main machine, then discard the fork."""
        return await call("promote", name)

    async def discard(name: str, lesson: str = "") -> str:
        """Delete a fork, optionally recording what you learned from it."""
        return await call("discard", name, lesson)

    async def diff(where: str = MAIN) -> str:
        """Show what changed in the project folder on the main machine or a fork, as a patch
        against the local copy of the project. Apply it locally (e.g. `git apply`) to keep a
        change you verified."""
        return await call("diff", where)

    async def export(path: str, where: str = MAIN) -> str:
        """Copy a file or folder from the main machine or a fork into the local project's
        .chronobox/exports/<where>/ folder (results, dumps, logs)."""
        return await call("export", path, where)

    async def sync(where: str = MAIN) -> str:
        """Copy your edits of the project's local files into the project folder on the main machine
        or a fork, so you can run and test them there (the project's services and dependencies
        run only in the machine). Only files you created, changed or deleted locally since the
        session started are synced; everything else on the machine stays as it is."""
        return await call("sync", where)

    async def verify_script(
        script: str,
        check: str,
        pre: str = "",
        rollback: str = "",
        twice: bool = True,
        code_from: str = MAIN,
        checkpoint: str = "start",
        timeout: float = 900,
    ) -> str:
        """Check a change script the way the user will run it on the real machine, and hand it over
        with the patch: what it saves in .chronobox/exports/handoff/ is what the user runs. Use it when
        the change needs actions and not only file edits: a migration or a backfill, restarting
        services, a setting outside the project.

        The replay: a new fork of `checkpoint` (default: the machine as it was before your work, like
        the real one); `pre` if given (before the new files, with the old code still running); the
        project files of `code_from` (a machine, or "local" for the files you edited locally); the
        script, twice unless twice=false (deployments get retried); then `check`, the project's own
        checks and traffic, the services' readiness, and a comparison with the machine you tested on.
        `rollback`, if given, must undo the change once the old files are back.

        `check` is required and decides whether you are done: a command that fails unless the change
        works the way the user asked, used as users would (call the new endpoint, insert a new row and
        read it back), not only that the services are up. On failure nothing is saved and the
        hand-off saved before is withdrawn. Pass the scripts' text, not paths: the replay only has
        the project files. Write them for the real machine, which has more data and live users: no
        values you only saw in this session, no long locks, and wait for what you restart to be
        ready. `timeout` is the script's limit in seconds."""
        return await call(
            "verify_script", script, check, pre, rollback, twice, code_from, checkpoint, timeout
        )

    tools: list[Call] = [run_guarded if guard else run, wait]
    if profile in (Profile.ROLLBACK, Profile.CHRONOBOX):
        tools += [checkpoint, list_checkpoints, rollback]
    if profile is Profile.CHRONOBOX:
        tools += [fork, compare, changes, promote, discard]
    if project_note:
        tools += [diff, export, sync]
    if (project_note or handoff) and profile is Profile.CHRONOBOX:
        tools.append(verify_script)
    for tool in tools:
        server.add_tool(tool, name=tool.__name__.removesuffix("_guarded"))
    return server


def load_project(args: argparse.Namespace) -> ProjectConfig | None:
    """The project's config when ``--project`` is given, else ``None``.

    Raises:
        ConfigError: If the project's ``chronobox.toml`` is missing or invalid.
    """
    return load_config(args.project) if args.project else None


def machine_spec(args: argparse.Namespace) -> MachineSpec:
    """The machine described by the command-line options (no project): image, setup script, size."""
    setup = (args.setup.read_text(),) if args.setup else ()
    return MachineSpec(
        setup=setup,
        image=args.image,
        mem_mib=args.mem,
        data_mib=args.disk,
        command_timeout=args.command_timeout,
    )


async def run_local(args: argparse.Namespace) -> None:
    """Own a machine and serve it to one agent over stdio MCP; the machine stops when the agent leaves."""
    async with _owned_service(args) as service:
        server = build_server(
            args.profile, service.call, guard=args.guard, project_note=_note(args), handoff=_handoff(args)
        )
        await server.run_stdio_async()


async def run_serving(args: argparse.Namespace) -> None:
    """Own a machine and serve it on ``--serve``'s unix socket until a shutdown request.

    For a project (``chronobox up``), also say on stderr whether it came up; a machine that failed
    to start exits instead of serving errors. The socket file is removed on the way out.

    Raises:
        SystemExit: If a project's machine failed to start.
    """
    async with _owned_service(args) as service:
        await service.serve(args.serve)
        try:
            if args.project:
                await service.ready.wait()
                print(_up_message(args.project, service), file=sys.stderr, flush=True)
                if service.error:
                    # Agents would connect to it and get only errors; better none is up.
                    raise SystemExit(1)
            await service.stopped.wait()
        finally:
            args.serve.unlink(missing_ok=True)


def _up_message(project: Path, service: WorkspaceService) -> str:
    """What ``chronobox up`` prints once the boot is over: the failure, or how to use the machine."""
    if service.error:
        return f"chronobox: the machine failed to start: {service.error}"
    folder = load_config(project).path
    return (
        f"chronobox: {folder} is up. Agents started with `chronobox mcp --project` use this machine; "
        "`chronobox cp` copies files in and out; Ctrl-C or `chronobox down` stops it."
    )


async def run_connected(args: argparse.Namespace) -> None:
    """Serve MCP (stdio, or HTTP with ``--http``) for the machine a ``--serve`` process owns."""

    async def call(method: str, *call_args: Any) -> str:
        """Forward one tool call to the serving process."""
        return await call_remote(args.connect, method, *call_args)

    server = build_server(
        args.profile, call, guard=args.guard, project_note=_note(args), handoff=_handoff(args)
    )
    if not args.http:
        await server.run_stdio_async()
        return
    host, port = args.http.rsplit(":", 1)
    await server.run_streamable_http_async(
        host=host,
        port=int(port),
        streamable_http_path=f"/mcp/{args.http_token}",
        transport_security=TransportSecuritySettings(allowed_hosts=[f"{host}:{port}"], allowed_origins=[]),
    )


@contextlib.asynccontextmanager
async def _owned_service(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    """A booting workspace service for the options (a project's or a plain machine), stopped and
    cleaned up on exit.

    The boot runs in the background, so the server answers at once; tool calls wait for it.
    """
    project = load_project(args)
    # A project's server runs from the project's folder: keep its VMs out of it.
    backend = FirecrackerBackend(args.root or (session_root() if project else None))
    if project is None:
        workspace = Workspace(backend, machine_spec(args), profile=args.profile, guard=args.guard)
        service = WorkspaceService(workspace)
        if args.handoff_dir and args.profile is Profile.CHRONOBOX:
            service.extra_methods.update(handoff_tools(workspace, None, exports=args.handoff_dir.resolve()))
    else:
        boot = ProjectBoot(backend, project, None if args.no_warm else default_warm_dir())
        spec = project_machine_spec(project)
        workspace = Workspace(backend, spec, profile=args.profile, guard=args.guard, boot=boot)
        service = WorkspaceService(workspace)
        service.extra_methods.update(project_tools(workspace, boot))
        if args.profile is Profile.CHRONOBOX:
            service.extra_methods.update(handoff_tools(workspace, boot))
    booting = asyncio.create_task(service.boot())
    try:
        yield service
    finally:
        booting.cancel()
        await workspace.close()
        await backend.cleanup()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse ``chronobox-mcp`` options; ``--http`` needs ``--connect``, ``--setup`` excludes ``--project``."""
    parser = argparse.ArgumentParser(prog="chronobox-mcp", description=__doc__.splitlines()[0])
    parser.add_argument("--profile", type=Profile, choices=list(Profile), default=Profile.CHRONOBOX)
    parser.add_argument(
        "--guard", action="store_true", help="refuse unrehearsed destructive commands on main"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", type=Path, metavar="SOCKET", help="own the VM and serve it on a unix socket")
    mode.add_argument("--connect", type=Path, metavar="SOCKET", help="MCP front end for a --serve process")
    parser.add_argument("--http", metavar="HOST:PORT", help="with --connect: serve MCP over HTTP")
    parser.add_argument("--http-token", default="mcp", help="secret URL path segment for --http")
    parser.add_argument(
        "--project", type=Path, help="a project folder with chronobox.toml (sets image, setup, size)"
    )
    parser.add_argument("--no-warm", action="store_true", help="with --project: always boot fresh")
    parser.add_argument(
        "--handoff-dir",
        type=Path,
        help="without --project: offer verify_script, saving hand-offs under this folder",
    )
    parser.add_argument(
        "--handoff", action="store_true", help="with --connect: offer verify_script (the --serve side has it)"
    )
    parser.add_argument("--setup", type=Path, help="shell script run once in the VM after boot")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="the machine's image (see `chronobox images`)")
    parser.add_argument("--mem", type=int, default=1024, help="VM memory in MiB")
    parser.add_argument("--disk", type=int, default=2048, help="writable disk in MiB")
    parser.add_argument("--command-timeout", type=float, default=300.0)
    parser.add_argument("--root", type=Path, help="directory for VMs and snapshots")
    args = parser.parse_args(argv)
    if args.http and not args.connect:
        parser.error("--http needs --connect")
    if args.project and args.handoff_dir:
        parser.error("--handoff-dir does not go with --project (hand-offs go to its .chronobox/exports)")
    if args.project and args.setup:
        parser.error("--setup does not go with --project (use prepare/setup in chronobox.toml)")
    return args


def _handoff(args: argparse.Namespace) -> bool:
    """Whether a machine without a project offers ``verify_script``."""
    return bool(args.handoff or args.handoff_dir)


def _note(args: argparse.Namespace) -> str | None:
    """The project note for the agent's instructions, if serving a project."""
    project = load_project(args)
    return project_note(project, with_handoff=args.profile is Profile.CHRONOBOX) if project else None


def main(argv: list[str] | None = None) -> None:
    """Run ``chronobox-mcp``: join the project's ``up`` machine if one is serving, else own one.

    Args:
        argv: Options; the process's own when ``None`` (``chronobox up`` and ``chronobox mcp`` pass theirs).
    """
    args = parse_args(argv)
    try:
        project = load_project(args)
    except ConfigError as error:
        sys.exit(f"chronobox-mcp: {error}")
    if project is not None and not (args.serve or args.connect):
        # A machine `chronobox up` keeps running for this project is the one to work on.
        socket_path = up_socket(project)
        if asyncio.run(is_serving(socket_path)):
            args.connect = socket_path
    if args.connect:
        asyncio.run(run_connected(args))
    elif args.serve:
        asyncio.run(run_serving(args))
    else:
        asyncio.run(run_local(args))


if __name__ == "__main__":
    main()
