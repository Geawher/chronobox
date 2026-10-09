#!/bin/sh
# PID 1 of a chronobox VM: read-only root (vda) + writable overlay on vdb, then the guest agent.
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
mount -t ext4 /dev/vdb /data
mkdir -p /data/upper /data/work
mount -t overlay overlay -o lowerdir=/,upperdir=/data/upper,workdir=/data/work /newroot
for d in proc sys dev; do mount --move /$d /newroot/$d; done
mount -t tmpfs tmpfs /newroot/tmp
mount -t tmpfs tmpfs /newroot/run
exec chroot /newroot /bin/sh -c '
  ip link set lo up
  hostname chronobox
  printf "127.0.0.1 localhost chronobox\n::1 localhost\n" > /etc/hosts
  mkdir -p /run/postgresql && chown postgres:postgres /run/postgresql && chmod 2775 /run/postgresql
  mkdir -p /dev/pts /dev/shm && mount -t devpts devpts /dev/pts && mount -t tmpfs shm /dev/shm
  exec python3 /opt/chronobox/guest_agent.py
'
