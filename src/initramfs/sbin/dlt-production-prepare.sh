#!/bin/busybox sh
# Local-only boot dispatch. No Internet, host upload, format, or Android decrypt.
set -eu
BB=/dlt/bin/busybox
. /etc/dlt-production.env
if test "${FTRACE_IO_DIAGNOSTIC:-0}" = 1; then
    /sbin/dlt-production-io-trace.sh || echo PRIVATE_IO_TRACE_UNAVAILABLE_CONTINUE_WITH_RESCUE_GUARDS
fi
test ! -e /recovery
test ! -e /newroot
mkdir /recovery /newroot
cleanup_failed_prepare() {
    ret=$?
    trap - EXIT
    if test "$ret" != 0; then
        for path in /recovery/proc /recovery/sys /recovery/dev /newroot/srv /newroot /recovery/mnt/dlt-store /recovery; do
            if "$BB" awk -v p="$path" '$2==p{found=1}END{exit !found}' /proc/mounts; then
                "$BB" umount "$path" || true
            fi
        done
        echo LOCAL_DISPATCH_PREPARE_FAILED:$ret
    fi
}
trap cleanup_failed_prepare EXIT
rdev=/dev/disk/by-partlabel/system_b
test "$("$BB" readlink -f "$rdev")" = /dev/sda7
test "$("$BB" blockdev --getsize64 "$rdev")" = 3221225472
"$BB" mount -t ext4 -o rw,nodev,noatime "$rdev" /recovery
test "$(cat /recovery/etc/dlt-server/native-root-id)" = "$RECOVERY_ID"
CTL=/usr/local/lib/dlt-recovery/dlt_boot_state.py
store=/recovery/mnt/dlt-store
mkdir -p "$store"
available=
# Assemble the reviewed linear main disk before mounting its filesystem.
# /dev, /sys and /proc are temporary chroot inputs, never retained as binds.
pooldev=
map_ok=no
bind_ok=yes
for base in dev sys proc; do
    mkdir -p "/recovery/$base"
    if ! "$BB" mount -o bind "/$base" "/recovery/$base"; then bind_ok=no; break; fi
done
if test "$bind_ok" = yes; then
    if pooldev=$("$BB" chroot /recovery /usr/bin/python3 -I /usr/local/lib/dlt-recovery/dlt_main_pool.py assemble --device-only); then
        case "$pooldev" in
            /dev/dm-[0-9]*)
                if test -b "$pooldev" && test "$("$BB" blockdev --getsize64 "$pooldev")" = 122102427648; then map_ok=yes; fi;;
        esac
    fi
fi
for base in proc sys dev; do
    if "$BB" awk -v p="/recovery/$base" '$2==p{found=1}END{exit !found}' /proc/mounts; then
        "$BB" umount "/recovery/$base" || exit 41
    fi
done
# Never mount the shorter raw prefix when assembly fails: independent rescue wins.
if test "$map_ok" = yes &&
   "$BB" mount -t ext4 -o rw,nodev,nosuid,noatime "$pooldev" "$store"; then
    if "$BB" chroot /recovery /usr/bin/python3 -c 'import json,sys;assert json.load(open("/mnt/dlt-store/.dlt-server-volume.json"))["uuid"]==sys.argv[1]' "$DATA_UUID"; then
        for slot in system-a system-b; do
            if test -f "$store/systems/$slot.ext4" && test ! -L "$store/systems/$slot.ext4" &&
               test "$("$BB" stat -c %s "$store/systems/$slot.ext4")" = 3221225472; then
                available="$available --available-slot $slot"
            fi
        done
    fi
fi
bootid=$(cat /proc/sys/kernel/random/boot_id)
chosen=recovery
for attempt in 1 2 3; do
    chosen=$("$BB" chroot /recovery /usr/bin/python3 "$CTL" select --boot-id "$bootid" --token-only $available) || chosen=recovery
    case "$chosen" in
      recovery) break;;
      system-a) expected=$SYSTEM_A_ID;;
      system-b) expected=$SYSTEM_B_ID;;
      *) chosen=recovery; break;;
    esac
    image="$store/systems/$chosen.ext4"
    if "$BB" mount -t ext4 -o loop,rw,nodev,noatime "$image" /newroot; then
        if test "$(cat /newroot/etc/dlt-server/native-root-id 2>/dev/null)" = "$expected" &&
           test -x /newroot/lib/systemd/systemd && test -s /newroot/home/ops/.ssh/authorized_keys; then
            if "$BB" mount -o bind "$store/data" /newroot/srv; then
                break
            fi
        fi
        "$BB" umount /newroot
    fi
    "$BB" chroot /recovery /usr/bin/python3 "$CTL" fail --slot "$chosen" || true
    case "$chosen" in
      system-a) available=$(echo "$available" | "$BB" sed 's/--available-slot system-a//g');;
      system-b) available=$(echo "$available" | "$BB" sed 's/--available-slot system-b//g');;
    esac
    chosen=recovery
done
mkdir -p /run/dlt-production
if test "$chosen" = recovery; then
    "$BB" mount --move /recovery /newroot
    printf 'recovery\n' >/run/dlt-production/slot
    cp /newroot/var/lib/dlt-recovery/boot-state/last-selection.json /run/dlt-local-boot-decision.json 2>/dev/null ||
        printf '{"slot":"recovery","reason":"local-dispatch-fallback"}\n' >/run/dlt-local-boot-decision.json
else
    printf '%s\n' "$chosen" >/run/dlt-production/slot
    cp /recovery/var/lib/dlt-recovery/boot-state/last-selection.json /run/dlt-local-boot-decision.json
    final=/run/initramfs
    mkdir -p "$final/bin" "$final/etc" "$final/proc" "$final/sys" "$final/dev" "$final/run" "$final/oldroot"
    cp "$BB" "$final/bin/busybox"
    ln -s busybox "$final/bin/sh"
    cp /sbin/dlt-production-final.sh "$final/shutdown"
    chmod 755 "$final/shutdown" "$final/bin/busybox"
    loop=$("$BB" awk '$2=="/newroot"{print $1}' /proc/mounts)
    rootdev=$("$BB" awk '$5=="/newroot"{print $3}' /proc/self/mountinfo)
    case "$loop:$rootdev" in /dev/loop*:7:*) ;; *) echo INVALID_PRODUCTION_ROOT_LOOP; exit 31;; esac
    printf 'ROOT_LOOP=%s\nROOT_DEV=%s\nROOT_SLOT=%s\nBOOT_ID=%s\n' "$loop" "$rootdev" "$chosen" "$bootid" >"$final/etc/dlt-production-final.env"
    printf 'RECOVERY_ID=%s\nRECEIPT_DIAGNOSTIC=%s\n' "$RECOVERY_ID" "${RECEIPT_DIAGNOSTIC:-0}" >>"$final/etc/dlt-production-final.env"
    mkdir -p '/run/systemd/system/run-dlt\x2dlocal\x2drecovery.mount.d'
    mkdir -p '/run/systemd/system/run-dlt\x2dlocal\x2drecovery-mnt-dlt\x2dstore.mount.d'
    for dir in /run/systemd/system/*.mount.d; do
        case "$dir" in *dlt*) printf '[Unit]\nDefaultDependencies=no\nConflicts=\nBefore=\n' >"$dir/90-owned-by-final-initramfs.conf";; esac
    done
fi
test -x /newroot/lib/systemd/systemd
test -L /newroot/sbin/init
echo LOCAL_PRODUCTION_OR_RECOVERY_ROOT_PREPARED:$chosen
