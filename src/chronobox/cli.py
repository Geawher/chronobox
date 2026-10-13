"""Build and list chronobox machine images.

    chronobox build --tools                          # the tools drive, once
    chronobox build --image postgres:16              # any Docker image, as is
    chronobox build --dockerfile ./Dockerfile        # or one built from a Dockerfile
    chronobox images                                 # what is built

Then ``chronobox-mcp --image NAME`` serves a machine of that image.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from chronobox.images import ImageError, build_image, build_tools, list_images
from chronobox.vm import default_vm_dir

MIB = 2**20


def build(args: argparse.Namespace) -> None:
    if args.tools:
        print(f"built the tools drive: {build_tools(args.vm_dir)}")
        return
    image = build_image(
        args.vm_dir, reference=args.image, dockerfile=args.dockerfile, context=args.context, name=args.name
    )
    print(f"built image {image.name!r} from {image.config.source} ({image.size_bytes / MIB:.0f} MiB)")
    print(f"use it with: chronobox-mcp --image {image.name}")


def show_images(args: argparse.Namespace) -> None:
    images = list_images(args.vm_dir)
    if not images:
        print(f"no images in {args.vm_dir}; build one with `chronobox build`")
        return
    width = max(len(image.name) for image in images)
    for image in images:
        print(f"{image.name:<{width}}  {image.size_bytes / MIB:>7.0f} MiB  {image.config.source}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
    build_parser.add_argument("--context", type=Path, help="build context (default: the Dockerfile's folder)")
    build_parser.add_argument("--name", help="image name (default: from the reference or folder)")
    build_parser.set_defaults(handler=build)

    images_parser = commands.add_parser("images", help="list built images")
    images_parser.set_defaults(handler=show_images)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        args.handler(args)
    except ImageError as error:
        sys.exit(f"chronobox: {error}")


if __name__ == "__main__":
    main()
