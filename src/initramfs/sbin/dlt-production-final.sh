#!/bin/sh
# systemd final PID1 stage. Root loop first, physical data then recovery last.
set -eu
BB=/bin/busybox
export PATH=/bin
test "$$" = 1 || exit 61
. /etc/dlt-production-final.env
mode=${1:-reboot}
case "$mode" in reboot|poweroff|halt) ;; *) mode=reboot;; esac
( "$BB" sleep 90; "$BB" reboot -f ) &
phase=entered
finish() {
    ret=$?
    trap - EXIT
    printf 'DLT_PRODUCTION_FINAL:phase=%s exit=%s\n' "$phase" "$ret" >/dev/kmsg 2>/dev/null || true
    "$BB" sync
    if test "${RECEIPT_DIAGNOSTIC:-0}" = 1; then
        "$BB" mkdir -p /www
        printf '{"boot_id":"%s","slot":"%s","phase":"%s","exit":%s}\n' "$BOOT_ID" "$ROOT_SLOT" "$phase" "$ret" >/www/receipt.json
        "$BB" ip link set usb0 up || true
        "$BB" ip addr replace 172.31.255.1/30 dev usb0 || true
        "$BB" httpd -f -p 172.31.255.1:18081 -h /www &
        "$BB" sleep 20
    fi
    case "$mode" in poweroff|halt) "$BB" poweroff -f;; *) "$BB" reboot -f;; esac
    while :; do "$BB" sleep 10; done
}
trap finish EXIT
test "$("$BB" cat /proc/sys/kernel/random/boot_id)" = "$BOOT_ID"
case "$ROOT_LOOP:$ROOT_DEV:$ROOT_SLOT" in /dev/loop*:7:*:system-a|/dev/loop*:7:*:system-b) ;; *) exit 62;; esac
test "$("$BB" awk '$5=="/oldroot"{print $3}' /proc/self/mountinfo)" = "$ROOT_DEV"
"$BB" mkdir -p /root-backing
backing=
for p in /oldroot/run/dlt-local-recovery /run/dlt-local-recovery; do
    if "$BB" awk -v p="$p" '$5==p{found=1} END{exit !found}' /proc/self/mountinfo; then backing=$p;break;fi
done
test -n "$backing"
test -f "$backing/mnt/dlt-store/systems/$ROOT_SLOT.ext4"
test "$("$BB" stat -c %s "$backing/mnt/dlt-store/systems/$ROOT_SLOT.ext4")" = 3221225472
"$BB" mount --move "$backing" /root-backing
phase=physical_backing_moved
"$BB" awk '$5 ~ /^\/oldroot\// {print length($5),$5}' /proc/self/mountinfo | "$BB" sort -rn | "$BB" cut -d ' ' -f 2- >/owned-unmounts
while IFS= read -r path; do
    case "$path" in /oldroot/*) "$BB" umount "$path";; *) exit 63;; esac
done </owned-unmounts
"$BB" sync
"$BB" umount /oldroot
phase=production_root_unmounted
name=${ROOT_LOOP##*/}
if test -e "/sys/class/block/$name/loop/backing_file"; then
    actual=$("$BB" cat "/sys/class/block/$name/loop/backing_file")
    case "$actual" in */mnt/dlt-store/systems/system-a.ext4|*/mnt/dlt-store/systems/system-b.ext4) ;; *) exit 64;; esac
    if test ! -b "$ROOT_LOOP";then "$BB" mknod "$ROOT_LOOP" b "${ROOT_DEV%:*}" "${ROOT_DEV#*:}";fi
    "$BB" losetup -d "$ROOT_LOOP"
fi
test ! -e "/sys/class/block/$name/loop/backing_file"
phase=production_loop_detached
"$BB" umount /root-backing/mnt/dlt-store
phase=physical_data_unmounted
test "$("$BB" cat /root-backing/etc/dlt-server/native-root-id)" = "$RECOVERY_ID"
# systemd-shutdown may remount ext4 backing stores read-only before pivot.
# Only the identified recovery filesystem is reopened for the final receipt.
"$BB" mount -o remount,rw,nodev /root-backing
"$BB" mkdir -p /root-backing/var/lib/dlt-recovery
printf '{"schema":1,"boot_id":"%s","slot":"%s","root_loop_detached":true,"physical_data_unmounted":true,"phase":"recovery_last_ready"}\n' "$BOOT_ID" "$ROOT_SLOT" >/root-backing/var/lib/dlt-recovery/production-final-shutdown.json
"$BB" sync
"$BB" umount /root-backing
phase=recovery_unmounted_last
exit 0
