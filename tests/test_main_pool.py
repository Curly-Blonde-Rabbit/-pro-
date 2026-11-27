"""Read-only unit tests; never creates, removes or resizes a device mapping."""
import copy,importlib.util,json,os,pathlib,tempfile,unittest
SOURCE=pathlib.Path(__file__).resolve().parents[1]/'src/storage/dlt_main_pool.py'
spec=importlib.util.spec_from_file_location('pool_under_test',SOURCE);g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
def fixture():
 starts=[18206784,69696,12652608,14749760]
 return {'schema':1,'board':'DLT-A0','name':'dlt-main','filesystem_uuid':'00000000-0000-4000-8000-000000000001','segments':[{'label':label,'device':dev,'bytes':size,'start_sector512':start,'partuuid':'00000000-0000-4000-8000-'+str(i+2).zfill(12)}for i,((label,dev,size),start)in enumerate(zip(g.COMPONENTS,starts))]}
class PlanTests(unittest.TestCase):
 def test_valid_geometry(self):self.assertTrue(g.validate_plan(fixture()));self.assertEqual(g.TOTAL_BYTES,122102427648)
 def test_wrong_header(self):
  for k,v in [('schema',2),('board','other'),('name','sensitive')]:
   p=fixture();p[k]=v
   with self.subTest(k=k),self.assertRaises(g.PoolError):g.validate_plan(p)
 def test_bad_uuid(self):
  p=fixture();p['filesystem_uuid']='not-a-uuid'
  with self.assertRaises(g.PoolError):g.validate_plan(p)
 def test_missing_segment(self):
  p=fixture();p['segments'].pop()
  with self.assertRaises(g.PoolError):g.validate_plan(p)
 def test_extra_segment(self):
  p=fixture();p['segments'].append(copy.deepcopy(p['segments'][0]))
  with self.assertRaises(g.PoolError):g.validate_plan(p)
 def test_reordered_segment(self):
  p=fixture();p['segments'][1:3]=reversed(p['segments'][1:3])
  with self.assertRaises(g.PoolError):g.validate_plan(p)
 def test_wrong_component_fields(self):
  for k,v in [('label','system_b'),('device','sda7'),('bytes',4096),('partuuid','bad')]:
   p=fixture();p['segments'][1][k]=v
   with self.subTest(k=k),self.assertRaises(g.PoolError):g.validate_plan(p)
 def test_invalid_start(self):
  for v in [-8,1,True,1.0,'8']:
   p=fixture();p['segments'][1]['start_sector512']=v
   with self.subTest(v=v),self.assertRaises(g.PoolError):g.validate_plan(p)
 def test_overlap(self):
  p=fixture();p['segments'][1]['start_sector512']=p['segments'][0]['start_sector512']
  with self.assertRaises(g.PoolError):g.validate_plan(p)
 def test_sector_table(self):
  p=fixture();rows=[{**r,'major_minor':str(i+8)+':1'}for i,r in enumerate(p['segments'])];table=g.make_table(rows)
  self.assertEqual(sum(int(x.split()[1])for x in table.splitlines())*512,g.TOTAL_BYTES)
  self.assertEqual(table.splitlines()[0],'0 227995544 linear 8:1 0')
 def test_table_injection(self):
  rows=[{**r,'major_minor':'8:1'}for r in fixture()['segments']];rows[1]['major_minor']='8:1\n0 99 error'
  with self.assertRaises(g.PoolError):g.make_table(rows)
 def test_canonical_table(self):self.assertEqual(g.normalize_table(' 0   8 linear 8:1 0\n\n'),'0 8 linear 8:1 0')
 @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root-owned plan validation runs on Linux')
 def test_trusted_file(self):
  with tempfile.TemporaryDirectory()as d:
   p=pathlib.Path(d)/'plan.json';p.write_text(json.dumps(fixture()));p.chmod(0o600);self.assertTrue(g.load_plan(p))
   p.chmod(0o666)
   with self.assertRaises(g.PoolError):g.load_plan(p)
 @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root-owned plan validation runs on Linux')
 def test_symlink_rejected(self):
  with tempfile.TemporaryDirectory()as d:
   p=pathlib.Path(d)/'plan.json';p.write_text(json.dumps(fixture()));p.chmod(0o600);s=pathlib.Path(d)/'alias';s.symlink_to(p)
   with self.assertRaises(g.PoolError):g.load_plan(s)
 @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root-owned plan validation runs on Linux')
 def test_oversize_rejected(self):
  with tempfile.TemporaryDirectory()as d:
   p=pathlib.Path(d)/'plan.json';p.write_bytes(b' '*16385);p.chmod(0o600)
   with self.assertRaises(g.PoolError):g.load_plan(p)
if __name__=='__main__':unittest.main()
