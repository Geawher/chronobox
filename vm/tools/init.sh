#!/bin/sh
# PID 1 of a chronobox VM, on the read-only tools drive (vda). The machine's image (vdb, read-only)
# is the lower layer of an overlay whose writable layer is on the data disk (vdc); the guest agent
# stays on the tools drive and runs every command inside that overlay with chroot.
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
mount -t tmpfs tmpfs /tmp
mount -t ext4 -o ro /dev/vdb /image
mount -t ext4 /dev/vdc /data
mkdir -p /data/upper /data/work
mount -t overlay overlay -o lowerdir=/image,upperdir=/data/upper,workdir=/data/work /newroot
mkdir -p /newroot/proc /newroot/sys /newroot/dev /newroot/tmp /newroot/run /newroot/root /newroot/etc
mount -t proc proc /newroot/proc
mount -t sysfs sys /newroot/sys
mount -t devtmpfs dev /newroot/dev
mkdir -p /newroot/dev/pts /newroot/dev/shm
mount -t devpts devpts /newroot/dev/pts
mount -t tmpfs shm /newroot/dev/shm
# The links udev or a container runtime would make; shells need /dev/fd for process substitution.
ln -sf /proc/self/fd /newroot/dev/fd
ln -sf /proc/self/fd/0 /newroot/dev/stdin
ln -sf /proc/self/fd/1 /newroot/dev/stdout
ln -sf /proc/self/fd/2 /newroot/dev/stderr
# /tmp and /run live in memory as on a booted Linux, seeded with what the image put there
# (e.g. /run/postgresql), since some images create directories there at build time.
for dir in tmp run; do
  mount -t tmpfs tmpfs /newroot/$dir
  [ -d /image/$dir ] && cp -a /image/$dir/. /newroot/$dir/
done
chmod 1777 /newroot/tmp
ip link set lo up
hostname chronobox
printf "127.0.0.1 localhost chronobox\n::1 localhost\n" > /newroot/etc/hosts
exec python3 /opt/chronobox/guest_agent.py
