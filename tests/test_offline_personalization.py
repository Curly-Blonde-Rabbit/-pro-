import importlib.util,json,os,pathlib,subprocess,tempfile,unittest
FILE=pathlib.Path(__file__).resolve().parents[1]/'tools/personalize_offline_root.py'
spec=importlib.util.spec_from_file_location('personalize',FILE)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


@unittest.skipUnless(os.name=='posix' and hasattr(os,'geteuid') and os.geteuid()==0,
                     'Linux root required for offline UID/host-key test')
class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=pathlib.Path(self.temp.name)
        subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(self.base/'owner')],check=True)
        self.key=self.base/'owner.pub'

    def tearDown(self):self.temp.cleanup()

    def root(self,name):
        r=self.base/name;(r/'etc/dlt-public').mkdir(parents=True)
        (r/'etc/dlt-public/candidate.json').write_text(json.dumps({'identity_free':True}))
        return r

    def test_two_instances_generate_different_host_identity(self):
        a=self.root('a');b=self.root('b')
        m.personalize(a,self.key);m.personalize(b,self.key)
        self.assertNotEqual((a/'etc/machine-id').read_bytes(),(b/'etc/machine-id').read_bytes())
        self.assertNotEqual((a/'etc/ssh/ssh_host_ed25519_key.pub').read_bytes(),
                            (b/'etc/ssh/ssh_host_ed25519_key.pub').read_bytes())
        self.assertEqual((a/'home/ops/.ssh/authorized_keys').stat().st_uid,1000)
        self.assertFalse(json.loads((a/'etc/dlt-public/owner-enrolled.json').read_text())['ready_to_flash'])

    def test_live_root_rejected(self):
        with self.assertRaises(ValueError):m.personalize('/',self.key)

    def test_repeated_enrollment_rejected(self):
        r=self.root('a');m.personalize(r,self.key)
        with self.assertRaises(ValueError):m.personalize(r,self.key)

    def test_symlink_identity_target_rejected(self):
        r=self.root('a');outside=self.base/'outside';outside.mkdir()
        (r/'etc/ssh').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):m.personalize(r,self.key)
        self.assertEqual(list(outside.iterdir()),[])

    def test_invalid_key_rejected_without_host_creation(self):
        r=self.root('a');bad=self.base/'bad.pub';bad.write_text('ssh-ed25519 invalid')
        with self.assertRaises(ValueError):m.personalize(r,bad)
        self.assertFalse((r/'etc/ssh').exists())


if __name__=='__main__':unittest.main()
