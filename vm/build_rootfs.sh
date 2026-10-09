#!/bin/sh
# Build rootfs.ext4 (read-only VM root) in $CHRONOBOX_VM_DIR (default vm/build) from the
# chronobox-vm Docker image, rootless. Ownership is preserved by unpacking and running
# mkfs.ext4 inside a container.
set -e
cd "$(dirname "$0")"
OUT=${CHRONOBOX_VM_DIR:-$PWD/build}
mkdir -p "$OUT"
docker build -q -t chronobox-vm:latest image >/dev/null
cid=$(docker create chronobox-vm:latest)
docker export "$cid" -o "$OUT/rootfs.tar"
docker rm "$cid" >/dev/null
docker run --rm -v "$OUT:/out" --entrypoint sh chronobox-vm:latest -c '
  set -e
  mkdir /tmp/root && tar -xf /out/rootfs.tar -C /tmp/root --numeric-owner
  rm -f /out/rootfs.ext4
  mkfs.ext4 -q -L chronobox-root -d /tmp/root /out/rootfs.ext4 1600M
  chmod 644 /out/rootfs.ext4
'
rm -f "$OUT/rootfs.tar"
ls -la "$OUT/rootfs.ext4"
