# Sourced only from the diagnostic PID-1 init loop.
dlt_ramroot_handoff() {
    test "$$" = 1 || { echo HANDOFF_REQUIRES_PID1; return 40; }
    test -f /run/dlt-ramroot/request || return 41
    test ! -e /run/dlt-ramroot/handoff-failed || return 42
    . /etc/dlt-ramroot.env
    test "$(cat /run/dlt-ramroot/prepared)" = "$ROOTFS_RAW_SHA256" || return 43
    test -x /newroot/lib/systemd/systemd || return 44
    grep -q ' /newroot ext4 ' /proc/mounts || return 45
    grep -q ' /ramdisk tmpfs ' /proc/mounts || return 46
    # No board firmware or Wi-Fi daemons are brought up by this initial USB-only test.
    # Their standalone service lifecycle is intentionally a separate work item.
    mkdir -p /newroot/proc /newroot/sys /newroot/dev /newroot/run
    moved=""
    for name in proc sys dev run; do
        if /bin/busybox mount --move "/$name" "/newroot/$name"; then
            moved="$name $moved"
        else
            for undo in $moved; do /bin/busybox mount --move "/newroot/$undo" "/$undo" || true; done
            touch /run/dlt-ramroot/handoff-failed
            echo RAMROOT_MOUNT_MOVE_FAILED:$name
            return 47
        fi
    done
    mkdir -p /newroot/run/dlt-ramroot-data
    if ! /bin/busybox mount --move /ramdisk /newroot/run/dlt-ramroot-data; then
        for undo in $moved; do /bin/busybox mount --move "/newroot/$undo" "/$undo" || true; done
        touch /run/dlt-ramroot/handoff-failed
        return 48
    fi
    # All checks/mount moves have succeeded; stop the old listeners only at this point.
    if test -s /newroot/run/dropbear.pid; then
        read oldssh </newroot/run/dropbear.pid
        case "$oldssh" in ''|*[!0-9]*) ;; *) kill "$oldssh" 2>/dev/null || true;; esac
    fi
    case "$DLT_TIMEOUT_PID" in ''|*[!0-9]*) ;; *) kill "$DLT_TIMEOUT_PID" 2>/dev/null || true;; esac
    case "$DLT_THERMAL_PID" in ''|*[!0-9]*) ;; *) kill "$DLT_THERMAL_PID" 2>/dev/null || true;; esac
    echo RAMROOT_EXECUTING_UBUNTU_INIT
    # /newroot is a mount point independent of /run, avoiding a mount-tree cycle.
    exec /bin/busybox switch_root -c /dev/console /newroot /sbin/init
}

