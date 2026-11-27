#!/usr/bin/python3 -I
"""Persist/restore existing host identity only; never regenerate keys or restart SSH."""
import pathlib,sys,os,json,hashlib,stat,subprocess,argparse,time
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
from device_access import atomic
ROOT=pathlib.Path('/srv/dlt-state/host-identity')
SPECS={}
for kind in ('ed25519','ecdsa','rsa'):
    SPECS['etc/ssh/ssh_host_'+kind+'_key']={'mode':0o600,'uid':0,'gid':0}
    SPECS['etc/ssh/ssh_host_'+kind+'_key.pub']={'mode':0o644,'uid':0,'gid':0}
SPECS['home/ops/.ssh/authorized_keys']={'mode':0o600,'uid':1000,'gid':1000}
def digest(data):return hashlib.sha256(data).hexdigest()
def target_root(root):
    root=pathlib.Path(root)
    if not root.is_absolute() or root.is_symlink():raise ValueError('invalid root')
    if str(root)!='/':
        if not str(root.resolve()).startswith('/run/dlt-hostid-test-') or not (root/'.isolated-hostid-test').is_file():raise ValueError('invalid isolated host identity target')
    return root
def regular(path,private=False):
    for p in (path,*path.parents):
        if p.is_symlink():raise ValueError('symlink identity path')
    st=path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_size>65536 or st.st_mode&0o022:raise ValueError('unsafe identity file')
    if private and (st.st_uid!=0 or st.st_mode&0o077):raise ValueError('private identity file permissions')
    return path.read_bytes()
def snapshot(source='/',destination=ROOT):
    source=target_root(source);destination=pathlib.Path(destination)
    if destination.exists():
        verify(destination);return {'snapshot_created':False,'existing_snapshot_retained':True}
    data={n:regular(source/n) for n in SPECS}
    # Confirm each public key actually matches the existing private key. Never
    # emit the key, password material, or command output into the engineering log.
    for kind in ('ed25519','ecdsa','rsa'):
        name='etc/ssh/ssh_host_'+kind+'_key'
        p=subprocess.run(['/usr/bin/ssh-keygen','-y','-f',str(source/name)],capture_output=True,timeout=8)
        if p.returncode or p.stdout.split()[:2]!=data[name+'.pub'].split()[:2]:raise ValueError('SSH host key pair mismatch')
    destination.mkdir(mode=0o700,parents=True,exist_ok=False)
    manifest={'schema':1,'existing_keys_only':True,'files':{}}
    for n,value in data.items():
        p=destination/'files'/n;p.parent.mkdir(mode=0o700,parents=True,exist_ok=True);atomic(p,value,0o600)
        manifest['files'][n]=dict(SPECS[n],sha256=digest(value),bytes=len(value))
    atomic(destination/'manifest.json',(json.dumps(manifest,indent=2)+'\n').encode(),0o600)
    verify(destination)
    return {'snapshot_created':True,'files':len(data),'private_key_contents_logged':False}
def verify(source=ROOT):
    source=pathlib.Path(source)
    if source.is_symlink() or source.stat().st_uid!=0 or source.stat().st_mode&0o077:raise ValueError('unsafe identity store')
    data=regular(source/'manifest.json',True);manifest=json.loads(data)
    if not isinstance(manifest,dict) or manifest.get('schema')!=1 or set(manifest.get('files',{}))!=set(SPECS):raise ValueError('invalid identity manifest')
    for n,expected in SPECS.items():
        entry=manifest['files'][n]
        if any(entry.get(k)!=v for k,v in expected.items()):raise ValueError('unexpected identity permissions')
        contents=regular(source/'files'/n,True)
        if len(contents)!=entry['bytes'] or digest(contents)!=entry['sha256']:raise ValueError('persistent identity hash mismatch')
    return manifest
def restore(source=ROOT,destination='/',apply=False):
    source=pathlib.Path(source);destination=target_root(destination);manifest=verify(source)
    passwd=(destination/'etc/passwd').read_text().splitlines();ops=[x.split(':') for x in passwd if x.startswith('ops:')]
    if len(ops)!=1 or ops[0][2:4]!=['1000','1000']:raise ValueError('protected ops UID/GID baseline mismatch')
    changes=[]
    for n,entry in manifest['files'].items():
        p=destination/n
        for parent in (p,*p.parents):
            if parent.is_symlink():raise ValueError('symlink restore target')
        different=True
        if p.exists():
            value=regular(p);st=p.stat();different=(digest(value)!=entry['sha256'] or st.st_uid!=entry['uid'] or st.st_gid!=entry['gid'] or st.st_mode&0o777!=entry['mode'])
        if different:changes.append(n)
    if apply:
        for n in changes:
            p=destination/n;entry=manifest['files'][n]
            p.parent.mkdir(mode=0o700 if n.startswith('home/') else 0o755,parents=True,exist_ok=True)
            if n.startswith('home/'):
                for parent in (destination/'home/ops',destination/'home/ops/.ssh'):
                    os.chown(parent,1000,1000);parent.chmod(0o700)
            atomic(p,regular(source/'files'/n,True),entry['mode']);os.chown(p,entry['uid'],entry['gid'])
            st=p.stat();assert digest(p.read_bytes())==entry['sha256'] and st.st_uid==entry['uid'] and st.st_mode&0o777==entry['mode']
    return {'verified_files':len(manifest['files']),'files_needing_restore':changes,'applied':apply,'new_keys_generated':False,'SSH_restart_requested':False}
def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['snapshot','check','restore']);args=p.parse_args()
    if os.geteuid()!=0:raise PermissionError('root required')
    result=snapshot() if args.action=='snapshot' else restore(apply=args.action=='restore')
    result.update(time=time.time(),boot_id=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    atomic(pathlib.Path('/run/dlt-host-identity-restore.json'),(json.dumps(result,indent=2)+'\n').encode(),0o600)
    print(json.dumps(result))
if __name__=='__main__':main()
