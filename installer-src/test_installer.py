import base64,hashlib,importlib.util,io,json,os,pathlib,struct,sys,tarfile,tempfile,unittest,uuid
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import boot_image as b


class BootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.file=pathlib.Path(__file__).resolve().parents[1]/'payload/boot-template.img'
        cls.data=cls.file.read_bytes()
        cls.key='ssh-ed25519 '+base64.b64encode(b'\0\0\0\x0bssh-ed25519\0\0\0\x20'+bytes(range(32))).decode()
        cls.layout={k:('dlt-linux-stage1-' if k=='recovery_id' else 'dlt-production-')+uuid.uuid4().hex
                    for k in ['recovery_id','system_a_id','system_b_id']}
        cls.layout['data_uuid']=str(uuid.uuid4())

    def test_template_has_no_owner(self):
        _,_,e=b.split(self.data)
        self.assertNotIn('root/.ssh/authorized_keys',e)
        self.assertFalse(any(n.startswith('etc/dropbear/dropbear_') for n in e))

    def test_temporary_personalized_boot_has_only_new_public_key(self):
        x=b.personalize(self.data,self.key,{},True);_,k,e=b.split(x)
        self.assertIn('etc/dlt-install-mode',e)
        self.assertEqual(e['root/.ssh/authorized_keys']['body'],(self.key+'\n').encode())
        self.assertEqual(k,b.split(self.data)[1])

    def test_production_has_new_layout_no_install_mode(self):
        x=b.personalize(self.data,self.key,self.layout);_,_,e=b.split(x)
        self.assertNotIn('etc/dlt-install-mode',e)
        env=e['etc/dlt-production.env']['body'].decode()
        self.assertIn('RECOVERY_ID='+self.layout['recovery_id'],env)
        self.assertIn('DATA_UUID='+self.layout['data_uuid'],env)

    def test_reject_bad_boot_magic(self):
        with self.assertRaises(ValueError):b.split(b'invalid')

    def test_reject_truncated_image(self):
        with self.assertRaises(ValueError):b.split(self.data[:4096])

    def test_reject_other_page_size(self):
        x=bytearray(self.data);struct.pack_into('<I',x,36,2048)
        with self.assertRaises(ValueError):b.split(x)

    def test_reject_shell_option_in_key(self):
        with self.assertRaises(ValueError):b.personalize(self.data,'command=bad '+self.key,self.layout)

    def test_reject_newline_in_identity(self):
        l=dict(self.layout,recovery_id='x\nSHELL=bad')
        with self.assertRaises(ValueError):b.personalize(self.data,self.key,l)

    def test_reject_cpio_traversal(self):
        raw=b.record('../escape',{'values':[1,0o100600,0,0,1,0,0,0,0,0,0,0,0],'body':b''})
        with self.assertRaises(ValueError):b.parse_cpio(raw)

    def test_serialization_hash_id(self):
        x=b.personalize(self.data,self.key,self.layout);h,k,e=b.split(x)
        rs=struct.unpack_from('<I',h,16)[0];at=4096+((len(k)+4095)//4096)*4096
        ident=hashlib.sha1()
        for v in [k,x[at:at+rs],b'',b'']:ident.update(v);ident.update(struct.pack('<I',len(v)))
        self.assertEqual(h[576:596],ident.digest())


@unittest.skipUnless(os.name=='posix','target-side POSIX checks')
class TargetGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import target_install
        cls.t=target_install

    def test_current_installed_system_is_not_RAM_installer(self):
        if pathlib.Path('/run/dlt-public-install-session').exists():self.skipTest('actual personalized installer session')
        with self.assertRaises(ValueError):self.t.require_ram_installer()

    def test_tar_absolute_path(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'x.tar'
            with tarfile.open(p,'w') as t:t.addfile(tarfile.TarInfo('/etc/shadow'),io.BytesIO())
            with self.assertRaises(ValueError):self.t.safe_archive(p)

    def test_tar_symlink_parent(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'x.tar'
            with tarfile.open(p,'w') as t:
                link=tarfile.TarInfo('a');link.type=tarfile.SYMTYPE;link.linkname='/etc';t.addfile(link)
                t.addfile(tarfile.TarInfo('a/shadow'),io.BytesIO())
            with self.assertRaises(ValueError):self.t.safe_archive(p)

    def test_tar_block_node(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'x.tar'
            with tarfile.open(p,'w') as t:
                f=tarfile.TarInfo('dev/bad');f.type=tarfile.BLKTYPE;t.addfile(f)
            with self.assertRaises(ValueError):self.t.safe_archive(p)

    def test_dot_prefixed_symlink_parent(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'x.tar'
            with tarfile.open(p,'w') as t:
                link=tarfile.TarInfo('./a');link.type=tarfile.SYMTYPE;link.linkname='/etc';t.addfile(link)
                t.addfile(tarfile.TarInfo('./a/shadow'),io.BytesIO())
            with self.assertRaises(ValueError):self.t.safe_archive(p)

    def test_absolute_hardlink_target(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'x.tar'
            with tarfile.open(p,'w') as t:
                link=tarfile.TarInfo('bad');link.type=tarfile.LNKTYPE;link.linkname='/etc/shadow';t.addfile(link)
            with self.assertRaises(ValueError):self.t.safe_archive(p)


if __name__=='__main__':unittest.main(verbosity=2)
