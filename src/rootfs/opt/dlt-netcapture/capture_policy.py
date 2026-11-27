"""Boot-bound capture admission. Default off, no packet storage or autostart."""
import pathlib,json,os,stat,fcntl,time,contextlib,tempfile
RUN=pathlib.Path('/run/dlt-netcapture')
def boot():return pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
def location():
 if os.geteuid()!=0:raise PermissionError('root module control required')
 for p in (RUN,*RUN.parents):
  if p.is_symlink():raise ValueError('unsafe capture runtime path')
 if not any(x.split()[4]=='/run' and x.split(' - ',1)[1].split()[0]=='tmpfs' for x in pathlib.Path('/proc/self/mountinfo').read_text().splitlines()):raise ValueError('capture module policy must be RAM-backed')
 RUN.mkdir(mode=0o700,exist_ok=True);st=RUN.stat()
 if st.st_uid or st.st_mode&0o077:raise ValueError('invalid capture runtime permissions')
def read():
 default={'enabled':False,'default_enabled':False,'boot_id':boot(),'reason':'default-off','autostart':False}
 p=RUN/'module.json'
 if not p.exists():return default
 try:
  fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
  try:
   st=os.fstat(fd)
   if not stat.S_ISREG(st.st_mode) or st.st_uid or st.st_mode&0o077 or st.st_size>4096:raise ValueError('invalid module policy file')
   obj=json.loads(os.read(fd,4097))
  finally:os.close(fd)
  if not isinstance(obj,dict):raise ValueError('invalid module policy object')
  if obj.get('boot_id')!=boot():return dict(default,reason='different-boot-default-off')
  if type(obj.get('enabled')) is not bool:raise ValueError('invalid enabled state')
  return dict(default,enabled=obj['enabled'],reason='manually-enabled' if obj['enabled'] else 'manually-disabled',updated=obj.get('updated'))
 except (OSError,ValueError,TypeError):return dict(default,reason='invalid-policy-default-off')
def set_enabled(enabled,origin):
 if origin not in ('physical-phone','trusted-maintenance'):raise PermissionError('capture switch requires phone or existing trusted maintenance channel')
 if type(enabled) is not bool:raise ValueError('boolean switch required')
 location();fd=os.open(RUN/'module.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
 try:
  fcntl.flock(fd,fcntl.LOCK_EX);value={'enabled':enabled,'default_enabled':False,'boot_id':boot(),'autostart':False,'updated':time.time()};tempfd,name=tempfile.mkstemp(prefix='.module-',dir=RUN)
  try:
   with os.fdopen(tempfd,'w') as f:os.fchmod(f.fileno(),0o600);json.dump(value,f);f.flush();os.fsync(f.fileno())
   os.replace(name,RUN/'module.json')
  finally:
   if os.path.exists(name):os.unlink(name)
  return read()
 finally:os.close(fd)
def require_enabled():
 state=read()
 if not state['enabled']:raise RuntimeError('抓包模块已关闭，请先在手机抓包模块打开开关')
if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('action',choices=['status','enable','disable']);args=p.parse_args();location()
 if args.action=='status':print(json.dumps(read()))
 else:print(json.dumps(set_enabled(args.action=='enable','trusted-maintenance')))
