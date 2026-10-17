"""Build chronobox machine images, prepare projects and serve them to agents.

    chronobox build --tools                          # the tools drive, once
    chronobox build --image postgres:16              # any Docker image, as is
    chronobox build --dockerfile ./Dockerfile        # or one built from a Dockerfile
    chronobox build --project .                      # the image a chronobox.toml declares
    chronobox images                                 # what is built
    chronobox init                                   # a starter chronobox.toml (and AGENTS.md guide) here
    chronobox guide                                  # write or refresh the chronobox guide in AGENTS.md
    chronobox warm                                   # boot the project here once, save its warm start
    chronobox up                                     # keep the project's machine running
    chronobox cp ./fixtures main:/src/fixtures       # copy into it (or out: main:/src/app.py ./out)
    chronobox down                                   # stop it
    chronobox mcp --project .                        # the MCP server (same options as chronobox-mcp)

In a project: ``claude mcp add chronobox -- chronobox mcp --project .``. While ``chronobox
up`` runs, that server works on the machine it keeps (so you and the agent share it);
otherwise each agent session starts its own.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import sys
from pathlib import Path

from chronobox import server
from chronobox.config import (
    CONFIG_FILE,
    ConfigError,
    ImageSource,
    ProjectConfig,
    load_config,
    starter_config,
)
from chronobox.copying import CopyError, check_direction, pack_local, parse_location, unpack_local
from chronobox.guide import CLAUDE_FILE, CLAUDE_IMPORT, write_guide
from chronobox.images import Image, ImageError, build_image, build_tools, list_images
from chronobox.project import ProjectBoot, default_warm_dir, session_root, up_socket
from chronobox.service import Op, ServiceError, get_remote, is_serving, put_remote, request
from chronobox.vm import FirecrackerBackend, VMError, default_vm_dir

BYTES_PER_MIB = 1024 * 1024
MAX_SOCKET_PATH = 107  # bytes in sockaddr_un.sun_path, without the final NUL


def build(args: argparse.Namespace) -> None:
    """``chronobox build``: the tools drive, an image from a reference or Dockerfile, or a project's image."""
    if args.tools:
        print(f"built the tools drive: {build_tools(args.vm_dir)}")
        return
    if args.project:
        image = build_project_image(args.vm_dir, load_config(args.project).image)
    else:
        image = build_image(
            args.vm_dir,
            reference=args.image,
            dockerfile=args.dockerfile,
            context=args.context,
            name=args.name,
        )
    size_mib = image.size_bytes / BYTES_PER_MIB
    print(f"built image {image.name!r} from {image.config.source} ({size_mib:.0f} MiB)")
    print(f"use it with: chronobox-mcp --image {image.name}")


def build_project_image(vm_dir: Path, source: ImageSource) -> Image:
    """Build the image a project's config declares.

    Raises:
        ImageError: If the config names an image without saying how to build it.
    """
    if source.reference is None and source.dockerfile is None:
        raise ImageError(
            f"chronobox.toml names the image {source.name!r} without a reference or dockerfile; "
            "build it with --image or --dockerfile"
        )
    return build_image(
        vm_dir,
        reference=source.reference,
        dockerfile=source.dockerfile,
        context=source.context,
        name=source.name,
    )


def show_images(args: argparse.Namespace) -> None:
    """``chronobox images``: one line per built image with its size and source."""
    images = list_images(args.vm_dir)
    if not images:
        print(f"no images in {args.vm_dir}; build one with `chronobox build`")
        return
    width = max(len(image.name) for image in images)
    for image in images:
        print(f"{image.name:<{width}}  {image.size_bytes / BYTES_PER_MIB:>7.0f} MiB  {image.config.source}")


def init(args: argparse.Namespace) -> None:
    """``chronobox init``: write a starter ``chronobox.toml`` in the project folder, then say what next.

    Raises:
        ConfigError: If the folder does not exist or already has a ``chronobox.toml``.
    """
    folder = args.project.resolve()
    file = folder / CONFIG_FILE
    try:
        with file.open("x") as config:
            config.write(starter_config(folder))
    except FileExistsError:
        raise ConfigError(f"{file} already exists") from None
    except FileNotFoundError:
        raise ConfigError(f"no folder {folder}") from None
    steps = ["chronobox build --project .  # the image, from the Dockerfile"]
    if load_config(folder).image.dockerfile is None:
        steps = []
    steps += [
        "chronobox warm               # optional: prepare the warm start now",
        "claude mcp add chronobox -- chronobox mcp --project .",
    ]
    print(f"wrote {file}")
    say_guide(load_config(folder))
    print("next: add the project's services and setup, then:")
    for step in steps:
        print(f"  {step}")


def guide(args: argparse.Namespace) -> None:
    """``chronobox guide``: write the chronobox guide for agents into the project's AGENTS.md, or
    refresh it (after the services change, or a chronobox upgrade)."""
    say_guide(load_config(args.project))


def say_guide(config: ProjectConfig) -> None:
    """Write the guide, then say where, and how Claude Code reads it if the project has a CLAUDE.md."""
    result = write_guide(config)
    print(f"{'wrote' if result.created else 'updated'} the chronobox guide for agents in {result.file}")
    if result.claude_hint:
        print(
            f"  Claude Code reads {CLAUDE_FILE} instead of AGENTS.md here: add a line `{CLAUDE_IMPORT}` "
            f"to {CLAUDE_FILE} so it reads the guide too"
        )


def warm(args: argparse.Namespace) -> None:
    """``chronobox warm``: make sure the project's warm start exists and say how long starting took."""
    config = load_config(args.project)
    boot = asyncio.run(prepare_warm_start(config, args.vm_dir))
    if boot.was_warm:
        print(f"{config.file}: the warm start is up to date ({boot.seconds:.1f} s to start)")
    else:
        print(
            f"{config.file}: booted, prepared and saved in {boot.seconds:.1f} s; "
            "agent sessions now start warm"
        )


async def prepare_warm_start(config: ProjectConfig, vm_dir: Path) -> ProjectBoot:
    """Start the project's machine once, so its warm start exists, then stop it.

    Raises:
        VMError: If the machine does not boot or a prepare, service or setup step fails.
    """
    backend = FirecrackerBackend(session_root(), vm_dir)
    boot = ProjectBoot(backend, config, default_warm_dir())
    try:
        await boot()
    finally:
        await backend.cleanup()
    return boot


def up(args: argparse.Namespace) -> None:
    """``chronobox up``: serve the project's machine on its socket until Ctrl-C or ``chronobox down``.

    Raises:
        ServiceError: If the socket path is too long for a unix socket, or the project is already up.
    """
    config = load_config(args.project)
    socket_path = up_socket(config)
    if len(os.fsencode(socket_path)) > MAX_SOCKET_PATH:
        raise ServiceError(f"socket path too long for a unix socket: {socket_path}; set XDG_RUNTIME_DIR")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    # Held while the machine is up: a second `up` of the project stops here, even while the
    # first is still booting (before its socket answers).
    with socket_path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ServiceError(f"{config.path} is already up") from None
        os.environ["CHRONOBOX_VM_DIR"] = str(args.vm_dir)
        warm_option = ["--no-warm"] if args.no_warm else []
        try:
            server.main(["--project", str(config.file), "--serve", str(socket_path), *warm_option])
        except KeyboardInterrupt:
            # asyncio has already cancelled the server, which stopped the machine on its way out.
            print(f"chronobox: {config.path} stopped", file=sys.stderr)


def down(args: argparse.Namespace) -> None:
    """``chronobox down``: ask the project's ``up`` machine to shut down.

    Raises:
        ServiceError: If no machine is up for the project.
    """
    config = load_config(args.project)
    socket_path = asyncio.run(running_machine(config))
    asyncio.run(request(socket_path, {"op": Op.SHUTDOWN}))
    print(f"{config.path} is shutting down")


def copy(args: argparse.Namespace) -> None:
    """``chronobox cp``: copy between here and the ``up`` machine (main or a fork), in either direction.

    Raises:
        CopyError: If both or neither sides are on the machine, or a local side is unusable.
        ServiceError: If no machine is up, or the machine refuses the copy.
    """
    source, destination = parse_location(args.source), parse_location(args.destination)
    check_direction(source, destination)
    socket_path = asyncio.run(running_machine(load_config(args.project)))
    if destination.is_remote:
        archive = pack_local(Path(source.path))
        asyncio.run(put_remote(socket_path, destination.where, destination.path, archive))
        print(f"copied {source.path} into {args.destination}")
        return
    archive = asyncio.run(get_remote(socket_path, source.where, source.path))
    names = unpack_local(archive, Path(destination.path))
    print(f"copied {args.source} into {destination.path}: {', '.join(names) or 'empty folder'}")


async def running_machine(config: ProjectConfig) -> Path:
    """The socket of the machine ``chronobox up`` keeps for this project.

    Raises:
        ServiceError: If none is running.
    """
    socket_path = up_socket(config)
    if not await is_serving(socket_path):
        raise ServiceError(f"no machine is up for {config.path}; start one with `chronobox up`")
    return socket_path


def serve_mcp(args: argparse.Namespace) -> None:
    """``chronobox mcp``: run the MCP server with the remaining options, on this command's VM files."""
    os.environ["CHRONOBOX_VM_DIR"] = str(args.vm_dir)
    server.main(args.server_args)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line; options after ``mcp`` are left in ``server_args`` for the server."""
    parser = argparse.ArgumentParser(prog="chronobox", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--vm-dir", type=Path, default=default_vm_dir(), help="where the VM files live (CHRONOBOX_VM_DIR)"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build_parser = commands.add_parser("build", help="build the tools drive or an image")
    source = build_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--tools", action="store_true", help="the tools drive every machine boots")
    source.add_argument("--image", metavar="REFERENCE", help="a Docker image, e.g. postgres:16")
    source.add_argument("--dockerfile", type=Path, help="a Dockerfile to build")
    source.add_argument("--project", type=Path, help="the image a project's chronobox.toml declares")
    build_parser.add_argument("--context", type=Path, help="build context (default: the Dockerfile's folder)")
    build_parser.add_argument("--name", help="image name (default: from the reference or folder)")
    build_parser.set_defaults(handler=build)

    images_parser = commands.add_parser("images", help="list built images")
    images_parser.set_defaults(handler=show_images)

    project_commands = {
        "init": ("write a starter chronobox.toml and the AGENTS.md guide", init),
        "guide": ("write or refresh the chronobox guide in AGENTS.md", guide),
        "warm": ("boot a project once and save its warm start", warm),
        "up": ("keep a project's machine running (Ctrl-C stops it)", up),
        "down": ("stop the machine `chronobox up` keeps", down),
        "cp": ("copy files between here and the machine `chronobox up` keeps", copy),
    }
    for name, (summary, handler) in project_commands.items():
        command = commands.add_parser(name, help=summary)
        command.add_argument("--project", type=Path, default=Path(), help="project folder (default: here)")
        command.set_defaults(handler=handler)
        if handler is up:
            command.add_argument("--no-warm", action="store_true", help="boot fresh, ignore the warm start")
        if handler is copy:
            command.add_argument("source", help="local path, or main:/PATH or FORK:/PATH")
            command.add_argument("destination", help="a folder: local, or main:/PATH or FORK:/PATH")

    # Every option after `mcp` (--help too) belongs to the server, which parses them itself.
    mcp_parser = commands.add_parser(
        "mcp", help="serve a machine to an agent (`chronobox mcp --help` for options)", add_help=False
    )
    mcp_parser.set_defaults(handler=serve_mcp)
    args, server_args = parser.parse_known_args(argv)
    if server_args and args.handler is not serve_mcp:
        parser.error(f"unrecognized arguments: {' '.join(server_args)}")
    args.server_args = server_args
    return args


def main(argv: list[str] | None = None) -> None:
    """Run a ``chronobox`` command; expected failures exit with a one-line message, not a traceback."""
    args = parse_args(argv)
    try:
        args.handler(args)
    except (ImageError, ConfigError, VMError, ServiceError, CopyError) as error:
        sys.exit(f"chronobox: {error}")


if __name__ == "__main__":
    main()
