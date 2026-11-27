# Sourced by the initramfs PID1, not by an SSH child.
dlt_production_handoff() {
    test "$$" = 1 || return 40
    test -s /run/dlt-production/slot || return 41
    slot=$(cat /run/dlt-production/slot)
    mkdir -p /newroot/proc /newroot/sys /newroot/dev /newroot/run
    moved=""
    for name in proc sys dev run; do
        if /bin/busybox mount --move "/$name" "/newroot/$name"; then moved="$name $moved"
        else
            for undo in $moved; do /bin/busybox mount --move "/newroot/$undo" "/$undo" || true; done
            return 42
        fi
    done
    if test "$slot" != recovery; then
        mkdir -p /newroot/run/dlt-local-recovery
        if ! /bin/busybox mount --move /recovery /newroot/run/dlt-local-recovery; then
            for undo in $moved; do /bin/busybox mount --move "/newroot/$undo" "/$undo" || true; done
            return 43
        fi
    fi
    if test -s /newroot/run/dropbear.pid; then
        read oldssh </newroot/run/dropbear.pid
        case "$oldssh" in ''|*[!0-9]*) ;; *) kill "$oldssh" 2>/dev/null || true;; esac
    fi
    kill "$DLT_TIMEOUT_PID" "$DLT_THERMAL_PID" 2>/dev/null || true
    echo LOCAL_DISPATCH_EXECUTING_INIT:$slot
    exec /bin/busybox switch_root -c /dev/console /newroot /sbin/init
}
