#!/bin/sh
# Download the Firecracker binary and a guest kernel into $CHRONOBOX_VM_DIR (default vm/build; no root needed).
set -e
cd "$(dirname "$0")"
OUT=${CHRONOBOX_VM_DIR:-$PWD/build}
mkdir -p "$OUT" && cd "$OUT"
TAG=v1.17.0
ARCH=x86_64
if [ ! -x firecracker ]; then
  curl -fsSL -o fc.tgz "https://github.com/firecracker-microvm/firecracker/releases/download/$TAG/firecracker-$TAG-$ARCH.tgz"
  tar xzf fc.tgz && cp "release-$TAG-$ARCH/firecracker-$TAG-$ARCH" firecracker && rm -rf fc.tgz "release-$TAG-$ARCH"
fi
if [ ! -f vmlinux ]; then
  # the CI kernel Firecracker tests with (6.1 series, vsock built in)
  key=$(curl -fsSL "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/v1.15/$ARCH/vmlinux-6.1&list-type=2" \
    | grep -oP "(?<=<Key>)(firecracker-ci/v1.15/$ARCH/vmlinux-6\.1\.[0-9]+)(?=</Key>)" | sort -V | tail -1)
  curl -fsSL -o vmlinux "https://s3.amazonaws.com/spec.ccfc.min/$key"
fi
./firecracker --version | head -1
ls -la vmlinux
