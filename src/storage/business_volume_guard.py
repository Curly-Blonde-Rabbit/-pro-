"""Validate business directories on the single owned Linux pool; never format or mount."""
import pathlib,json,subprocess,stat,os
def trusted_json(path):
 path=pathlib.Path(path)
 for p in (path,*path.parents):
  if p.is_symlink():raise ValueError('business path symlink')
 st=path.stat()
 if st.st_uid or st.st_mode&0o022 or not stat.S_ISREG(st.st_mode) or st.st_size>16384:raise ValueError('untrusted business metadata')
 return json.loads(path.read_text())
def validate(plan,mount,marker,pool_marker,device,size,partuuid):
 if plan.get('schema')!=2 or plan.get('mode')!='shared-main-pool' or plan.get('provisioned')is not True:raise ValueError('business directory not provisioned')
 if mount.get('target')!='/srv' or mount.get('fstype')!='ext4':raise ValueError('main pool mount missing')
 if mount.get('uuid')!=plan.get('filesystem_uuid') or pool_marker.get('uuid')!=plan.get('filesystem_uuid'):raise ValueError('main filesystem identity mismatch')
 if marker.get('schema')!=2 or marker.get('purpose')!='dlt-business-on-main-pool' or marker.get('filesystem_uuid')!=plan.get('filesystem_uuid'):raise ValueError('business marker mismatch')
 if device!='/dev/sda17' or device!=plan.get('device') or size!=116733718528 or size!=plan.get('bytes') or partuuid!=plan.get('partuuid'):raise ValueError('main partition identity mismatch')
 return True
def main():
 if os.geteuid():raise PermissionError('root required')
 plan=trusted_json('/etc/dlt-server/business-volume.json');marker=trusted_json('/srv/business/.dlt-business-volume.json');root=pathlib.Path('/run/dlt-local-recovery/mnt/dlt-store') if pathlib.Path('/run/dlt-local-recovery/mnt/dlt-store').is_dir() else pathlib.Path('/mnt/dlt-store');pool=trusted_json(root/'.dlt-server-volume.json')
 for p in (pathlib.Path('/srv/business'),pathlib.Path('/srv')):
  if p.is_symlink() or p.stat().st_uid or p.stat().st_mode&0o022:raise ValueError('untrusted business directory')
 if pathlib.Path('/srv/business').stat().st_dev!=pathlib.Path('/srv').stat().st_dev or os.path.ismount('/srv/business'):raise ValueError('business path is not on the shared pool')
 mount=json.loads(subprocess.check_output(['findmnt','-J','-o','TARGET,SOURCE,FSTYPE,UUID','--mountpoint','/srv'],text=True,timeout=5))['filesystems'][0]
 if plan.get('schema')==3:
  import importlib.util
  rec=root.parents[1] if root.name=='dlt-store' and root.parent.name=='mnt' else pathlib.Path('/')
  if root==pathlib.Path('/mnt/dlt-store'):rec=pathlib.Path('/')
  layout=trusted_json(rec/'opt/dlt-recovery/production-layout.json')
  if plan.get('mode')!='shared-linear-main-pool' or plan.get('provisioned')is not True or plan.get('filesystem_uuid')!=layout.get('data_uuid') or plan.get('bytes')!=122102427648:raise ValueError('linear business plan mismatch')
  if pool.get('uuid')!=plan['filesystem_uuid'] or marker.get('schema')!=2 or marker.get('purpose')!='dlt-business-on-main-pool' or marker.get('filesystem_uuid')!=plan['filesystem_uuid']:raise ValueError('linear business marker mismatch')
  helper=rec/'usr/local/lib/dlt-recovery/dlt_main_pool.py'
  if helper.is_symlink() or helper.stat().st_uid or helper.stat().st_mode&0o022:raise ValueError('untrusted linear helper')
  spec=importlib.util.spec_from_file_location('dlt_main_pool_business',helper);g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
  g.validate_mount(layout,mount,'/srv',rec)
 else:
  device=str(pathlib.Path(mount['source'].split('[',1)[0]).resolve());size=int(subprocess.check_output(['blockdev','--getsize64',device],text=True,timeout=5));uuid=subprocess.check_output(['lsblk','-dn','-o','PARTUUID',device],text=True,timeout=5).strip();validate(plan,mount,marker,pool,device,size,uuid)
 print(json.dumps({'business_volume_verified':True,'mode':'shared-main-pool','separate_data_partition':False,'format_requested':False}))
if __name__=='__main__':main()
