#!/bin/busybox sh
# Initramfs rescue remains alive on every failed precondition; never formats.
set -eu
BOX=/dlt/bin/busybox
. /etc/dlt-persistent.env
test "$ROOT_LABEL" = system_b
test "$ROOT_BYTES" = 3221225472
test ! -e /newroot
dev="/dev/disk/by-partlabel/$ROOT_LABEL"
test -b "$dev"
test "$("$BOX" readlink -f "$dev")" = /dev/sda7
test "$("$BOX" blockdev --getsize64 "$dev")" = "$ROOT_BYTES"
mkdir /newroot
if ! "$BOX" mount -t ext4 -o rw,nodev,noatime "$dev" /newroot; then
    rmdir /newroot
    echo PERSISTENT_ROOT_MOUNT_FAILED_RESCUE_REMAINS
    exit 21
fi
if test "$(cat /newroot/etc/dlt-server/native-root-id 2>/dev/null)" != "$ROOT_ID" ||
   ! grep -q '^ID=ubuntu' /newroot/etc/os-release ||
   ! test -x /newroot/lib/systemd/systemd ||
   ! test -s /newroot/home/ops/.ssh/authorized_keys ||
   ! test -s /newroot/etc/ssh/ssh_host_ed25519_key; then
    "$BOX" umount /newroot
    rmdir /newroot
    echo PERSISTENT_ROOT_ID_OR_INIT_INVALID_RESCUE_REMAINS
    exit 22
fi
mkdir -p /newroot/proc /newroot/sys /newroot/dev /newroot/run
mkdir -p /run/dlt-persistent
printf '%s\n' "$ROOT_ID" >/run/dlt-persistent/prepared
echo PERSISTENT_ROOT_PREPARED
