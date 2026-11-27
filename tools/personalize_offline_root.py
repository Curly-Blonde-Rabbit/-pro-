#!/usr/bin/env python3
"""Enroll a new owner in an OFFLINE, identity-free candidate root directory.

This is NOT a flasher, partition creator, or installer. It never targets '/'.
Candidate startup remains gated until a compatible layout is separately prepared.
"""
import argparse,base64,hashlib,json,os,pathlib,re,stat,subprocess,uuid


def target(root, rel):
    p=root/rel
    for x in [p,*p.parents]:
        if x==root.parent:break
        if x.is_symlink():raise ValueError('symlink in target path: '+rel)
    return p


def atomic(root,rel,data,mode=0o600,uid=0,gid=0):
    p=target(root,rel);p.parent.mkdir(parents=True,exist_ok=True)
    q=p.with_name('.enroll-'+p.name)
    fd=os.open(q,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    os.chmod(q,mode);os.chown(q,uid,gid);os.replace(q,p)


def validate_key(text):
    a=text.strip().split()
    if len(a)<2 or a[0]!='ssh-ed25519':raise ValueError('one ssh-ed25519 public key required')
    b=base64.b64decode(a[1],validate=True)
    if len(b)!=51 or b[:19]!=b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
        raise ValueError('invalid Ed25519 public-key wire format')
    return ('ssh-ed25519 '+a[1]+'\n').encode(), 'SHA256:'+base64.b64encode(hashlib.sha256(b).digest()).decode().rstrip('=')


def personalize(root,key_path):
    if os.geteuid()!=0:raise PermissionError('Linux root required for offline ownership')
    raw=pathlib.Path(root)
    if not raw.is_absolute() or raw.is_symlink():raise ValueError('absolute non-symlink offline root required')
    root=raw.resolve()
    if root==pathlib.Path('/') or not (root/'etc/dlt-public/candidate.json').is_file():
        raise ValueError('explicit identity-free candidate root required; live root rejected')
    cfg=json.loads(target(root,'etc/dlt-public/candidate.json').read_text())
    if cfg.get('identity_free') is not True:raise ValueError('not an identity-free candidate')
    if target(root,'etc/dlt-public/owner-enrolled.json').exists():raise ValueError('owner already enrolled')
    key,fingerprint=validate_key(pathlib.Path(key_path).read_text())
    if target(root,'home/ops/.ssh/authorized_keys').exists():raise ValueError('pre-existing owner key')
    ssh=target(root,'etc/ssh');ssh.mkdir(exist_ok=True)
    if list(ssh.glob('ssh_host_*')):raise ValueError('existing host identity; no silent overwrite')
    for algorithm in ['ed25519','rsa']:
        args=['ssh-keygen','-q','-t',algorithm,'-N','','-f',str(ssh/('ssh_host_'+algorithm+'_key'))]
        if algorithm=='rsa':args+=['-b','3072']
        subprocess.run(args,check=True,timeout=30,stdin=subprocess.DEVNULL,capture_output=True)
    home=target(root,'home/ops');home.mkdir(mode=0o700,parents=True,exist_ok=True);os.chown(home,1000,1000)
    dot=target(root,'home/ops/.ssh');dot.mkdir(mode=0o700);os.chown(dot,1000,1000)
    atomic(root,'home/ops/.ssh/authorized_keys',key,uid=1000,gid=1000)
    atomic(root,'etc/machine-id',(uuid.uuid4().hex+'\n').encode(),0o444)
    receipt={'schema':1,'owner_public_key_fingerprint':fingerprint,'unique_host_keys_created':True,
             'network_enrollment_enabled':False,'disk_layout_prepared':False,
             'ready_to_flash':False,'note':'Offline owner initialization only; not storage/boot installation acceptance.'}
    atomic(root,'etc/dlt-public/owner-enrolled.json',(json.dumps(receipt,indent=2)+'\n').encode())
    return receipt


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',required=True);p.add_argument('--owner-key',required=True)
    a=p.parse_args();print(json.dumps(personalize(a.root,a.owner_key),indent=2))
