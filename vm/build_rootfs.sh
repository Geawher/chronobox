#!/bin/sh
# Build the tools drive and the default image into $CHRONOBOX_VM_DIR (default vm/build),
# rootless (needs Docker). Other images: `chronobox build --image REF` or `--dockerfile PATH`.
set -e
cd "$(dirname "$0")/.."
export CHRONOBOX_VM_DIR="${CHRONOBOX_VM_DIR:-$PWD/vm/build}"
uv run chronobox build --tools
uv run chronobox build --dockerfile vm/images/default/Dockerfile --name default
