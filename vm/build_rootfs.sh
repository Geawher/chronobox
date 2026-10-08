#!/bin/sh
# Build vm/build/rootfs.ext4 (read-only VM root) from the chronobox-vm Docker image, rootless.
# Ownership is preserved by unpacking and running mkfs.ext4 inside a container.
set -e
cd "$(dirname "$0")"
docker build -q -t chronobox-vm:latest image >/dev/null
cid=$(docker create chronobox-vm:latest)
docker export "$cid" -o build/rootfs.tar
docker rm "$cid" >/dev/null
docker run --rm -v "$PWD/build:/out" --entrypoint sh chronobox-vm:latest -c '
  set -e
  mkdir /tmp/root && tar -xf /out/rootfs.tar -C /tmp/root --numeric-owner
  rm -f /out/rootfs.ext4
  mkfs.ext4 -q -L chronobox-root -d /tmp/root /out/rootfs.ext4 1600M
  chmod 644 /out/rootfs.ext4
'
rm -f build/rootfs.tar
ls -la build/rootfs.ext4
