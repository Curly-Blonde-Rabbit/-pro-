import importlib.util,json,pathlib,tempfile,unittest
spec=importlib.util.spec_from_file_location('state',pathlib.Path(__file__).with_name('dlt_boot_state.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class BootTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=pathlib.Path(self.tmp.name)/'state';self.s=m.Store(self.p)
    def tearDown(self):self.tmp.cleanup()
    def test_empty_is_recovery(self):self.assertEqual(self.s.select('b1',{'recovery'})['slot'],'recovery')
    def test_uninstalled_candidate_rejected(self):
        with self.assertRaises(ValueError):self.s.stage('system-a',{'recovery'})
    def test_healthy_trial_then_confirm(self):
        self.s.stage('system-a',{'system-a'});self.assertEqual(self.s.select('b1',{'system-a','recovery'})['slot'],'system-a')
        self.s.confirm('system-a','b1');self.assertEqual(self.s.select('b2',{'system-a','recovery'})['slot'],'system-a')
    def test_unconfirmed_reboot_falls_back(self):
        self.s.stage('system-a',{'system-a'});self.s.select('b1',{'system-a','recovery'})
        self.assertEqual(self.s.select('b2',{'system-a','recovery'})['slot'],'recovery')
        self.assertIsNone(self.s.load()['candidate'])
    def test_wrong_boot_cannot_confirm(self):
        self.s.stage('system-a',{'system-a'});self.s.select('b1',{'system-a'})
        with self.assertRaises(ValueError):self.s.confirm('system-a','other')
    def test_trial_retry_budget(self):
        self.s.stage('system-a',{'system-a'})
        for _ in range(2):self.assertEqual(self.s.select('b1',{'system-a'})['slot'],'system-a')
        self.assertEqual(self.s.select('b1',{'system-a','recovery'})['slot'],'recovery')
    def test_bad_candidate_image_falls_back(self):
        self.s.stage('system-a',{'system-a'});self.assertEqual(self.s.select('b1',{'recovery'})['slot'],'recovery')
    def test_one_copy_corrupt_uses_other(self):
        self.s.recovery();(self.p/'state.0.json').write_text('{bad')
        self.assertEqual(self.s.load()['request'],'recovery')
    def test_both_corrupt_safe_recovery(self):
        self.s.recovery()
        for p in self.p.glob('state.*.json'):p.write_text('bad')
        self.assertEqual(self.s.select('b1',{'system-a','recovery'})['slot'],'recovery')
    def test_request_recovery_is_one_shot(self):
        self.s.recovery();self.assertEqual(self.s.select('b1',{'recovery'})['reason'],'operator-requested-recovery')
        self.assertEqual(self.s.load()['request'],'none')
    def test_previous_good_preserved_on_failed_upgrade(self):
        self.s.stage('system-a',{'system-a'});self.s.select('b1',{'system-a'});self.s.confirm('system-a','b1')
        self.s.stage('system-b',{'system-b'});self.s.select('b2',{'system-a','system-b'})
        self.assertEqual(self.s.select('b3',{'system-a','system-b'})['slot'],'system-a')
    def test_state_symlink_rejected(self):
        if __import__('os').name=='nt':self.skipTest('Symlink privilege not presumed on Windows')
        self.p.mkdir(mode=0o700,exist_ok=True);(self.p/'state.0.json').symlink_to('/dev/null')
        self.assertEqual(self.s.load()['good'],'recovery')
    def test_candidate_mount_failure_clears_trial(self):
        self.s.stage('system-a',{'system-a'});self.s.select('b1',{'system-a'})
        self.s.fail('system-a')
        self.assertIsNone(self.s.load()['candidate'])
        self.assertIn('system-a',self.s.load()['rejected'])
        self.assertEqual(self.s.select('b1',{'recovery','system-b'})['slot'],'recovery')
    def test_known_good_mount_failure_uses_previous_good(self):
        self.s.stage('system-a',{'system-a'});self.s.select('b1',{'system-a'});self.s.confirm('system-a','b1')
        self.s.stage('system-b',{'system-b'});self.s.select('b2',{'system-b'});self.s.confirm('system-b','b2')
        self.s.fail('system-b')
        self.assertEqual(self.s.select('b3',{'system-a','system-b'})['slot'],'system-a')
        self.s.fail('system-a')
        self.assertEqual(self.s.select('b3',{'system-b','recovery'})['slot'],'recovery')
    def test_decision_receipt_matches_committed_state(self):
        decision=self.s.select('boot-receipt',{'recovery'})
        self.assertEqual(json.loads((self.p/'last-selection.json').read_text()),decision)
        self.assertEqual(decision['seq'],self.s.load()['seq'])
    def test_fail_rejects_unknown_slot_without_state_write(self):
        with self.assertRaises(ValueError):self.s.fail('../../system-a')
        self.assertFalse((self.p/'state.0.json').exists())
    def known_good(self):
        self.s.stage('system-b',{'system-b'});self.s.select('first',{'system-b'})
        self.s.confirm('system-b','first')
    def test_known_good_unhealthy_reboot_enters_recovery(self):
        self.known_good();self.s.select('second',{'system-b'})
        self.assertEqual(self.s.select('third',{'system-b'})['slot'],'recovery')
        self.assertEqual(self.s.load()['good'],'recovery')
    def test_every_healthy_boot_retains_good(self):
        self.known_good()
        for boot in ('second','third','fourth'):
            self.assertEqual(self.s.select(boot,{'system-b'})['slot'],'system-b')
            self.s.healthy('system-b',boot)
    def test_wrong_boot_lease_health_rejected(self):
        self.known_good();self.s.select('second',{'system-b'})
        with self.assertRaises(ValueError):self.s.healthy('system-b','first')
    def test_wrong_slot_health_rejected(self):
        self.s.select('first',{'system-b'})
        with self.assertRaises(ValueError):self.s.healthy('system-b','first')
    def test_recovery_without_network_does_not_roll_into_boot_loop(self):
        for boot in ('first','second','third'):
            self.assertEqual(self.s.select(boot,{'system-b'})['slot'],'recovery')
        self.s.healthy('recovery','third')
        self.assertTrue(self.s.load()['boot_healthy'])
    def test_legacy_valid_checksum_migrates_without_losing_good(self):
        self.known_good();s=self.s.load()
        for k in ('boot_slot','boot_id','boot_healthy'):s.pop(k)
        s['schema']=1
        obj={'payload':s,'sha256':m.hashlib.sha256(m.encode(s)).hexdigest()}
        for n in (0,1):m.atomic(self.p/('state.'+str(n)+'.json'),obj)
        self.assertEqual(self.s.load()['schema'],2)
        self.assertEqual(self.s.select('second',{'system-b'})['slot'],'system-b')
    def test_unhealthy_good_falls_back_previous(self):
        self.s.stage('system-a',{'system-a'});self.s.select('first',{'system-a'});self.s.confirm('system-a','first')
        self.s.stage('system-b',{'system-b'});self.s.select('second',{'system-b'});self.s.confirm('system-b','second')
        self.s.select('third',{'system-b','system-a'})
        self.assertEqual(self.s.select('fourth',{'system-b','system-a'})['slot'],'system-a')

if __name__=='__main__':unittest.main(verbosity=2)
