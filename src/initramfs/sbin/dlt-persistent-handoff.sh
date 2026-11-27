# Sourced only in initramfs PID 1. No RAM loop backing is involved.
dlt_persistent_handoff() {
    test "$$" = 1 || return 40
    . /etc/dlt-persistent.env
    test "$(cat /run/dlt-persistent/prepared 2>/dev/null)" = "$ROOT_ID" || return 41
    grep -q ' /newroot ext4 ' /proc/mounts || return 42
    moved=""
    for name in proc sys dev run; do
        if /bin/busybox mount --move "/$name" "/newroot/$name"; then
            moved="$name $moved"
        else
            for undo in $moved; do /bin/busybox mount --move "/newroot/$undo" "/$undo" || true; done
            return 43
        fi
    done
    if test -s /newroot/run/dropbear.pid; then
        read oldssh </newroot/run/dropbear.pid
        case "$oldssh" in ''|*[!0-9]*) ;; *) kill "$oldssh" 2>/dev/null || true;; esac
    fi
    kill "$DLT_TIMEOUT_PID" "$DLT_THERMAL_PID" 2>/dev/null || true
    echo PERSISTENT_ROOT_EXECUTING_UBUNTU_INIT
    exec /bin/busybox switch_root -c /dev/console /newroot /sbin/init
}
