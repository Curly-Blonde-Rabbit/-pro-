#!/bin/busybox sh
# Private bounded diagnostic instance; no global tracer or data payload capture.
set -eu
BB=/dlt/bin/busybox
mkdir -p /sys/kernel/debug
if ! "$BB" awk '$2=="/sys/kernel/debug"{f=1}END{exit !f}' /proc/mounts; then
    "$BB" mount -t debugfs debugfs /sys/kernel/debug
fi
I=/sys/kernel/debug/tracing/instances/dlt_root_io
test ! -e "$I"
mkdir "$I"
echo 128 >"$I/buffer_size_kb"
F="$I/events/block/block_rq_complete"
"$BB" grep -q 'field:int error;' "$F/format"
"$BB" grep -q 'field:dev_t dev;' "$F/format"
# Linux dev_t major occupies upper12 bits. Only major7 (loop) errors.
echo 'dev >= 7340032 && dev < 8388608 && error != 0' >"$F/filter"
echo 1 >"$F/enable"
K=/sys/kernel/debug/tracing/kprobe_events
if test -w "$K" && ! "$BB" grep -q 'dlt_root_io/lo_discard_return' "$K"; then
    if echo 'r:dlt_root_io/lo_discard_return lo_discard ret=$retval:s32' >>"$K"; then
        echo 'ret < 0' >"$I/events/dlt_root_io/lo_discard_return/filter"
        echo 1 >"$I/events/dlt_root_io/lo_discard_return/enable"
        echo PRIVATE_LOOP_DISCARD_RETURN_PROBE_ARMED
    fi
fi
if test -w "$F/trigger"; then
    echo 'stacktrace if dev >= 7340032 && dev < 8388608 && error != 0' >"$F/trigger" || true
fi
echo 1 >"$I/tracing_on"
echo PRIVATE_LOOP_IO_ERROR_TRACE_ARMED
