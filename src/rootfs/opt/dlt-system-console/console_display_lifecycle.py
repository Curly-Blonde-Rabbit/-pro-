"""Transfer native ESD ownership during the independently checked IRIS session.

The old vendor check ignores esd_enabled on its synchronous TE-override path.
It must not run before IRIS firmware upload completes. This does NOT report
panel health: resident_console separately requires real IRIS/DCS register reads.
No networking, reboot, forced module removal or kernel-memory writes are used.
"""
import errno
import json
import os
import pathlib
import stat
import sys
import time

MODE = pathlib.Path('/sys/kernel/debug/dsi_ss_oled_cmd_display/esd_check_mode')
LEASE = pathlib.Path('/run/dlt-system-console/native-esd-lease.json')
BOOT = pathlib.Path('/proc/sys/kernel/random/boot_id')
SUPPRESSED = 'esd_sw_sim_success'
ORIGINAL_MODES = ('reg_read', 'te_signal_check')


def module_present():
    return pathlib.Path('/sys/module/dlt_iris_live').exists()


class DisplayLifecycle:
    def __init__(self, mode=MODE, lease=LEASE, boot=BOOT,
                 loaded=module_present, writer=os.write):
        self.mode = pathlib.Path(mode)
        self.lease = pathlib.Path(lease)
        self.boot = pathlib.Path(boot)
        self.loaded = loaded
        self.writer = writer

    def read_mode(self):
        return self.mode.read_bytes().split(b'\0', 1)[0].decode('ascii').strip()

    def write_mode(self, value):
        if value not in (*ORIGINAL_MODES, SUPPRESSED):
            raise ValueError('unsupported ESD mode')
        data = (value + '\n').encode('ascii')
        fd = os.open(self.mode, os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            # A disabled vendor handler returns ZERO, not an errno. Buffered
            # text-file writers retry forever. One bounded syscall only.
            count = self.writer(fd, data)
        finally:
            os.close(fd)
        if count != len(data):
            raise OSError(errno.EIO, 'short/zero ESD control write; no retry')
        if self.read_mode() != value:
            raise RuntimeError('ESD mode readback mismatch')

    def read_lease(self):
        s = self.lease.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o077:
            raise RuntimeError('untrusted display lease')
        v = json.loads(self.lease.read_text())
        if (v.get('schema') != 1 or
                v.get('boot_id') != self.boot.read_text().strip() or
                v.get('original_mode') not in ORIGINAL_MODES):
            raise RuntimeError('wrong boot or invalid display lease')
        return v

    def save_lease(self, value):
        self.lease.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        p = self.lease.parent
        if p.is_symlink() or p.stat().st_uid != 0 or p.stat().st_mode & 0o022:
            raise RuntimeError('untrusted display runtime directory')
        fd = os.open(self.lease, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())

    def acquire(self):
        if self.loaded():
            raise RuntimeError('IRIS already owned; refuse ESD change')
        if self.lease.exists() or self.lease.is_symlink():
            self.restore()
        original = self.read_mode()
        if original not in ORIGINAL_MODES:
            raise RuntimeError('native ESD mode not known; refuse takeover')
        self.save_lease({'schema': 1,
                         'boot_id': self.boot.read_text().strip(),
                         'original_mode': original,
                         'health_owner': 'real IRIS DCS polling, not ESD simulation',
                         'created_monotonic': time.monotonic()})
        self.write_mode(SUPPRESSED)
        return {'original_mode': original,
                'native_ESD_suppressed': True,
                'real_panel_health_required': True}

    def restore(self):
        if not self.lease.exists() and not self.lease.is_symlink():
            return {'restored': True, 'lease': 'absent'}
        v = self.read_lease()
        if self.loaded():
            # Do not write while the module holds esd_enabled=false. Preserve
            # the lease for ExecStopPost/a later clean removal; never busy-loop.
            raise RuntimeError('display module still loaded; restore deferred')
        current = self.read_mode()
        if current == SUPPRESSED:
            self.write_mode(v['original_mode'])
        elif current != v['original_mode']:
            raise RuntimeError('ESD mode changed by another owner; not overwritten')
        self.lease.unlink()
        return {'restored': True, 'native_mode': v['original_mode']}


if __name__ == '__main__':
    if sys.argv[1:] != ['--restore'] or os.geteuid() != 0:
        raise SystemExit('root --restore only')
    print(json.dumps(DisplayLifecycle().restore()))
