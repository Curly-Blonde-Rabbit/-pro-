import errno
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'src/rootfs/opt/dlt-system-console'))
from console_display_lifecycle import DisplayLifecycle, SUPPRESSED


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.mode = self.root/'mode'
        self.mode.write_text('reg_read\n')
        self.boot = self.root/'boot'
        self.boot.write_text('test-boot')
        self.loaded = False
        self.writes = 0

        def writer(fd, data):
            self.writes += 1
            os.ftruncate(fd, 0)  # regular-file test double, never real debugfs
            return os.write(fd, data)
        self.c = DisplayLifecycle(self.mode, self.root/'lease.json', self.boot,
                                  lambda: self.loaded, writer)

    def tearDown(self):
        self.tmp.cleanup()

    def test_acquire_restore(self):
        self.c.acquire()
        self.assertEqual(self.c.read_mode(), SUPPRESSED)
        self.c.restore()
        self.assertEqual(self.c.read_mode(), 'reg_read')
        self.assertFalse(self.c.lease.exists())

    def test_TE_original_restored(self):
        self.mode.write_text('te_signal_check\n')
        self.c.acquire(); self.c.restore()
        self.assertEqual(self.c.read_mode(), 'te_signal_check')

    def test_50_cycles_without_lease_leak(self):
        for _ in range(50):
            self.c.acquire(); self.c.restore()
        self.assertEqual(self.writes, 100)
        self.assertFalse(self.c.lease.exists())

    def test_module_owned_acquire_rejected(self):
        self.loaded = True
        with self.assertRaises(RuntimeError): self.c.acquire()
        self.assertEqual(self.writes, 0)

    def test_module_owned_restore_never_writes(self):
        self.c.acquire(); self.loaded = True
        with self.assertRaises(RuntimeError): self.c.restore()
        self.assertEqual(self.writes, 1)
        self.assertTrue(self.c.lease.exists())
        self.loaded = False; self.c.restore()

    def test_zero_write_does_not_retry(self):
        calls = []
        self.c.writer = lambda fd, data: calls.append(data) or 0
        with self.assertRaises(OSError) as e: self.c.acquire()
        self.assertEqual(e.exception.errno, errno.EIO)
        self.assertEqual(len(calls), 1)
        self.c.restore()

    def test_short_write_does_not_retry(self):
        calls = []
        self.c.writer = lambda fd, data: calls.append(data) or 1
        with self.assertRaises(OSError): self.c.acquire()
        self.assertEqual(len(calls), 1)
        self.c.restore()

    def test_write_readback_mismatch(self):
        self.c.writer = lambda fd, data: len(data)
        with self.assertRaises(RuntimeError): self.c.acquire()
        self.c.restore()

    def test_stale_same_boot_lease_recovered(self):
        self.c.acquire(); self.c.acquire(); self.c.restore()
        self.assertEqual(self.c.read_mode(), 'reg_read')

    def test_foreign_boot_lease_rejected(self):
        self.c.acquire(); self.boot.write_text('other-boot')
        with self.assertRaises(RuntimeError): self.c.restore()
        self.assertEqual(self.writes, 1)

    def test_lease_symlink_rejected(self):
        self.c.lease.symlink_to(self.mode)
        with self.assertRaises(RuntimeError): self.c.acquire()
        self.assertEqual(self.writes, 0)

    def test_lease_permissions_rejected(self):
        self.c.acquire(); self.c.lease.chmod(0o644)
        with self.assertRaises(RuntimeError): self.c.restore()

    def test_foreign_mode_not_overwritten(self):
        self.c.acquire(); self.mode.write_text('te_signal_check\n')
        with self.assertRaises(RuntimeError): self.c.restore()
        self.assertEqual(self.writes, 1)

    def test_unleased_software_mode_rejected(self):
        self.mode.write_text(SUPPRESSED+'\n')
        with self.assertRaises(RuntimeError): self.c.acquire()

    def test_disabled_handler_never_written(self):
        self.mode.write_text('ESD feature not enabled\n')
        with self.assertRaises(RuntimeError): self.c.acquire()
        self.assertEqual(self.writes, 0)

    def test_no_lease_cleanup_noop(self):
        self.assertTrue(self.c.restore()['restored'])
        self.assertEqual(self.writes, 0)

    def test_failure_after_acquire_restores(self):
        try:
            self.c.acquire()
            raise ValueError('simulated renderer failure')
        except ValueError:
            self.c.restore()
        self.assertEqual(self.c.read_mode(), 'reg_read')

    def test_invalid_lease_original_mode_rejected(self):
        self.c.acquire()
        d=json.loads(self.c.lease.read_text());d['original_mode']='invalid'
        self.c.lease.write_text(json.dumps(d))
        with self.assertRaises(RuntimeError):self.c.restore()
        self.assertEqual(self.writes,1)

    def test_malformed_lease_rejected(self):
        self.c.lease.write_text('{invalid');self.c.lease.chmod(0o600)
        with self.assertRaises(ValueError):self.c.acquire()
        self.assertEqual(self.writes,0)

    def test_no_lease_never_changes_foreign_mode(self):
        self.mode.write_text('foreign mode')
        self.c.restore()
        self.assertEqual(self.writes,0)
        self.assertEqual(self.mode.read_text(),'foreign mode')


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(LifecycleTests))
    print(json.dumps({'tests':result.testsRun,'failures':len(result.failures),
                      'errors':len(result.errors),'passed':result.wasSuccessful()}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
