#!/dlt/bin/busybox sh
# RAM-only preparation. No physical block partition is mounted or written.
set -eu
PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH
BOX=/dlt/bin/busybox
test -f /etc/dlt-ramroot.env
. /etc/dlt-ramroot.env
test "$("$BOX" id -u)" = 0
test -f /etc/native-debug-build
case "$(uname -r)" in 4.14.83-perf+|4.14.83-dlt-dev1) ;; *) echo WRONG_KERNEL; exit 20;; esac
test ! -e /newroot
test ! -e /ramdisk
test -f /run/ubuntu-rootfs.ext4.gz
test "$("$BOX" sha256sum /run/ubuntu-rootfs.ext4.gz | "$BOX" awk '{print $1}')" = "$ROOTFS_GZ_SHA256"
available=$("$BOX" awk '/^MemAvailable:/{print $2}' /proc/meminfo)
case "$available" in ''|*[!0-9]*) echo INVALID_MEMORY_READING; exit 21;; esac
test "$available" -ge 4194304 || { echo NEED_4GIB_AVAILABLE_FOR_RAM_TEST; exit 22; }
mkdir /newroot /ramdisk
mounted_root=no
mounted_ram=no
cleanup_on_error() {
    ret=$?
    if test "$ret" -ne 0; then
        test "$mounted_root" = no || "$BOX" umount /newroot || true
        test "$mounted_ram" = no || "$BOX" umount /ramdisk || true
        "$BOX" rmdir /newroot /ramdisk 2>/dev/null || true
        echo RAMROOT_PREPARATION_FAILED:$ret
    fi
}
trap cleanup_on_error EXIT
"$BOX" mount -t tmpfs -o size=3G,mode=700,nosuid,nodev tmpfs /ramdisk
mounted_ram=yes
"$BOX" gzip -dc /run/ubuntu-rootfs.ext4.gz >/ramdisk/rootfs.ext4
test "$("$BOX" stat -c %s /ramdisk/rootfs.ext4)" = "$ROOTFS_RAW_BYTES"
test "$("$BOX" sha256sum /ramdisk/rootfs.ext4 | "$BOX" awk '{print $1}')" = "$ROOTFS_RAW_SHA256"
"$BOX" mount -t ext4 -o loop,nodev /ramdisk/rootfs.ext4 /newroot
mounted_root=yes
grep -q '^ID=ubuntu' /newroot/etc/os-release
test -x /newroot/lib/systemd/systemd
test -f /newroot/etc/dlt-server/image-build.json
test -s /newroot/home/ops/.ssh/authorized_keys
# Verify the actual ARM64 init binary and its dynamic loader before a PID-1 handoff.
"$BOX" chroot /newroot /lib/systemd/systemd --version
mkdir -p /newroot/etc/systemd/network /newroot/etc/systemd/system/multi-user.target.wants
cat >/newroot/etc/systemd/network/10-dlt-ramroot-usb.network <<'NETWORK'
[Match]
Name=usb* rndis*
[Link]
RequiredForOnline=no
[Network]
Address=172.31.255.1/30
DHCP=no
LinkLocalAddressing=no
ConfigureWithoutCarrier=yes
NETWORK
# Full systemd test remains bounded; this is not a production uptime policy.
cat >/newroot/etc/systemd/system/dlt-ramroot-return.service <<'SERVICE'
[Unit]
Description=Return from RAM-only Ubuntu test after ten minutes
After=basic.target
[Service]
Type=oneshot
ExecStart=/bin/systemctl --force --force reboot
TimeoutStartSec=30
SERVICE
cat >/newroot/etc/systemd/system/dlt-ramroot-return.timer <<'TIMER'
[Unit]
Description=Ten-minute bound on RAM-only Ubuntu boot test
[Timer]
OnActiveSec=600
AccuracySec=1s
Unit=dlt-ramroot-return.service
[Install]
WantedBy=timers.target
TIMER
mkdir -p /newroot/etc/systemd/system/timers.target.wants
ln -s ../dlt-ramroot-return.timer /newroot/etc/systemd/system/timers.target.wants/dlt-ramroot-return.timer
cat >/newroot/usr/local/sbin/dlt-ramroot-battery-guard <<'GUARD'
#!/bin/sh
while sleep 3; do
    test -r /sys/class/power_supply/battery/temp || continue
    t=$(cat /sys/class/power_supply/battery/temp)
    case "$t" in ''|*[!0-9-]*) continue;; esac
    if test "$t" -ge 450; then
        echo "RAM-only test battery threshold: $t" >/dev/kmsg
        exec /bin/systemctl --force --force reboot
    fi
done
GUARD
chmod 755 /newroot/usr/local/sbin/dlt-ramroot-battery-guard
cat >/newroot/etc/systemd/system/dlt-ramroot-battery-guard.service <<'SERVICE'
[Unit]
Description=Battery threshold guard for RAM-only Ubuntu test
After=basic.target
[Service]
ExecStart=/usr/local/sbin/dlt-ramroot-battery-guard
Restart=no
[Install]
WantedBy=multi-user.target
SERVICE
ln -s ../dlt-ramroot-battery-guard.service /newroot/etc/systemd/system/multi-user.target.wants/dlt-ramroot-battery-guard.service
mkdir -p /newroot/run /newroot/proc /newroot/sys /newroot/dev
mkdir -p /run/dlt-ramroot
printf '%s\n' "$ROOTFS_RAW_SHA256" >/run/dlt-ramroot/prepared
echo RAMROOT_PREPARED_HANDOFF_NOT_REQUESTED
trap - EXIT

