import importlib.util,pathlib,tempfile,unittest
spec=importlib.util.spec_from_file_location('startup',pathlib.Path(__file__).with_name('dlt-startup-check.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Clock:
    def __init__(self):self.now=0
    def time(self):return self.now
    def sleep(self,seconds):self.now+=seconds
class Tests(unittest.TestCase):
    def config(self):return {'timeouts':dict.fromkeys(m.STAGES,2),'automatic_reboot':False}
    def runstate(self,failed=None):
        clock=Clock();published=[]
        probes={name:(lambda n=name:n!=failed) for name in m.STAGES}
        state=m.execute(self.config(),probes,lambda s:published.append(s['mode']),clock.time,clock.sleep)
        return state,clock,published
    def test_all_ready(self):self.assertEqual(self.runstate()[0]['mode'],'ready')
    def test_storage_failure(self):self.assertEqual(self.runstate('storage')[0]['mode'],'maintenance')
    def test_hardware_failure(self):self.assertEqual(self.runstate('hardware')[0]['mode'],'maintenance')
    def test_network_failure(self):self.assertEqual(self.runstate('network')[0]['mode'],'maintenance')
    def test_ssh_failure(self):self.assertEqual(self.runstate('ssh')[0]['mode'],'maintenance')
    def test_timeout_bounded(self):self.assertEqual(self.runstate('network')[1].now,2)
    def test_exception_contained(self):
        def broken():raise OSError('fixture failure')
        c=Clock();r=m.wait_for(broken,2,1,c.time,c.sleep);self.assertFalse(r['passed']);self.assertIn('OSError',r['last_error'])
    def test_eventual_recovery(self):
        c=Clock();r=m.wait_for(lambda:c.now>=2,3,1,c.time,c.sleep);self.assertTrue(r['passed']);self.assertEqual(r['attempts'],3)
    def test_reboot_rejected(self):
        c=self.config();c['automatic_reboot']=True
        with self.assertRaises(ValueError):m.validate(c)
    def test_infinite_timeout_rejected(self):
        c=self.config();c['timeouts']['network']=float('inf')
        with self.assertRaises(ValueError):m.validate(c)
    def test_negative_timeout_rejected(self):
        c=self.config();c['timeouts']['ssh']=-1
        with self.assertRaises(ValueError):m.validate(c)
    def test_atomic_state_readback(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'state.json';m.save(p,{'mode':'maintenance'})
            self.assertEqual(json.loads(p.read_text())['mode'],'maintenance')
if __name__=='__main__':unittest.main(verbosity=2)

