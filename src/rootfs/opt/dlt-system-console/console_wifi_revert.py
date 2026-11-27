"""Independent one-shot rollback if GUI dies during an association change."""
import pathlib,json,os,uuid,sys,subprocess
from console_settings_backend import WPA
PATH=pathlib.Path('/run/dlt-system-console/wifi-transition.json')

def write(v):
    data=json.dumps(v,separators=(',',':')).encode();tmp=PATH.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    os.replace(tmp,PATH)
def arm(oldid,newid,preserve_existing=False):
    if PATH.exists() and json.loads(PATH.read_text()).get('state')=='pending':
        raise RuntimeError('上一笔网络切换尚未完成')
    token=uuid.uuid4().hex;unit='dlt-console-wifi-revert-'+token[:12]
    v={'token':token,'oldid':oldid,'newid':newid,'state':'pending','unit':unit,
       'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'preserve_other_connection':bool(preserve_existing)}
    write(v)
    p=subprocess.run(['systemd-run','--quiet','--unit='+unit,'--on-active=45',
                      '/usr/bin/python3',str(pathlib.Path(__file__).resolve()),token],capture_output=True,timeout=5)
    if p.returncode:v['state']='not-armed';write(v);raise RuntimeError('未能建立自动恢复保护')
    return token
def finish(token,state):
    if not PATH.exists():return
    v=json.loads(PATH.read_text())
    if v.get('token')!=token:return
    v['state']=state;write(v)
    subprocess.run(['systemctl','stop',v['unit']+'.timer'],capture_output=True,timeout=5)
def revert(token):
    if not PATH.exists():return
    v=json.loads(PATH.read_text())
    if v.get('token')!=token or v.get('state')!='pending':return
    if v.get('boot_id')!=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip():return
    with WPA() as w:
        keep_other=False
        if v.get('preserve_other_connection'):
            from console_settings_backend import fields
            state=fields(w.cmd('STATUS'));keep_other=state.get('wpa_state')=='COMPLETED' and state.get('id')!=v.get('newid')
        if not keep_other and isinstance(v.get('oldid'),str) and v['oldid'].isdigit():w.cmd('SELECT_NETWORK '+v['oldid'])
        if isinstance(v.get('newid'),str) and v['newid'].isdigit():w.cmd('REMOVE_NETWORK '+v['newid'])
    v['state']='rolled-back-preserved-existing' if keep_other else 'rolled-back';write(v)
if __name__=='__main__':
    if len(sys.argv)!=2 or len(sys.argv[1])!=32:raise SystemExit(2)
    revert(sys.argv[1])
