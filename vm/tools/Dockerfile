# The tools drive of every chronobox VM: the kernel boots it, its init mounts the machine's image
# under an overlay and the guest agent runs commands inside that image with chroot. The image
# itself needs nothing from chronobox (no Python, no init). The same image builds the ext4 disks
# on the host (e2fsprogs, GNU tar).
FROM alpine:3.20
RUN apk add --no-cache python3 util-linux e2fsprogs tar
RUN mkdir -p /image /data /newroot /opt/chronobox
COPY guest_agent.py /opt/chronobox/guest_agent.py
COPY init.sh /sbin/chronobox-init
RUN chmod +x /sbin/chronobox-init
