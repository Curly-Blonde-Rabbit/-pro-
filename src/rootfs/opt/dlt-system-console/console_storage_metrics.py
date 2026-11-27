"""One visible disk: the mounted physical Linux storage pool, never root-loop plus backing twice."""
import pathlib,shutil,os,json
POOL_UUID='6cad6e0d-29ec-4442-a84c-4ee85c60f0da'
def describe(used,total,free):
 return {'path':'/srv','label':'硬盘','percent':round(max(0,min(100,100*used/total)),1) if total>0 else None,'used_bytes':used,'total_bytes':total,'free_bytes':free,'total_gib':round(total/2**30,2),'used_gib':round(used/2**30,2),'free_gib':round(free/2**30,2),'basis':'physical-pool-once-including-internal-system-images','available':total>0}
def missing():return {'path':'/srv','label':'硬盘','percent':None,'available':False,'total_gib':'—','used_gib':'—','free_gib':'—'}
def disk_info():
 try:
  # /srv must be the real ext4 bind, not an empty directory on the OS image.
  rows=[v for v in pathlib.Path('/proc/self/mountinfo').read_text().splitlines() if v.split()[4]=='/srv']
  if len(rows)!=1 or rows[0].split(' - ',1)[1].split()[0]!='ext4':return missing()
  marker=None
  for parent in ('/mnt/dlt-store','/run/dlt-local-recovery/mnt/dlt-store'):
   base=pathlib.Path(parent);p=base/'.dlt-server-volume.json'
   if p.is_file() and not p.is_symlink() and base.stat().st_dev==pathlib.Path('/srv').stat().st_dev:
    data=json.loads(p.read_text())
    if data.get('uuid')==POOL_UUID:marker=data;break
  if marker is None:return missing()
  u=shutil.disk_usage('/srv');return describe(u.used,u.total,u.free)
 except (OSError,ValueError,KeyError,IndexError):return missing()
