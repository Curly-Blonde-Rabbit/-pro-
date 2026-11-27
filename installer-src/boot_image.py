"""DLT legacy Android boot v0/newc parser; no flashing or device access."""
import copy,gzip,hashlib,pathlib,re,stat,struct


def parse_cpio(blob):
    result={};off=0
    while True:
        if blob[off:off+6]!=b'070701':raise ValueError('not newc initramfs')
        v=[int(blob[off+6+i*8:off+14+i*8],16) for i in range(13)]
        if not 1<=v[11]<=4096:raise ValueError('invalid newc name length')
        name=blob[off+110:off+110+v[11]-1].decode()
        off=(off+110+v[11]+3)&~3
        body=blob[off:off+v[6]];off=(off+v[6]+3)&~3
        if name=='TRAILER!!!':break
        if name in result or name.startswith('/') or '..' in pathlib.PurePosixPath(name).parts:
            raise ValueError('unsafe/duplicate newc path')
        if len(body)!=v[6]:raise ValueError('truncated newc content')
        result[name]={'values':v,'body':body}
    return result


def record(name,e):
    v=e['values'][:];b=e['body'];n=name.encode()+b'\0';v[6]=len(b);v[11]=len(n)
    out=b'070701'+b''.join(f'{x:08x}'.encode() for x in v)+n
    out+=b'\0'*((-len(out))%4);out+=b;out+=b'\0'*((-len(b))%4)
    return out


def split(data):
    if data[:8]!=b'ANDROID!':raise ValueError('not Android boot image')
    page=struct.unpack_from('<I',data,36)[0]
    ksize=struct.unpack_from('<I',data,8)[0];rsize=struct.unpack_from('<I',data,16)[0]
    if page!=4096 or struct.unpack_from('<I',data,24)[0]!=0:
        raise ValueError('unreviewed page size/second payload')
    start=page+((ksize+page-1)//page)*page
    if start+rsize>len(data):raise ValueError('truncated boot image')
    return data[:page],data[page:page+ksize],parse_cpio(gzip.decompress(data[start:start+rsize]))


def put(entries,name,body,mode=0o600):
    entries[name]={'values':[len(entries)+1,stat.S_IFREG|mode,0,0,1,0,len(body),0,0,0,0,len(name.encode())+1,0],
                   'body':body}


def assemble(header,kernel,entries):
    raw=b''.join(record(n,e) for n,e in sorted(entries.items(),key=lambda x:(x[0].count('/'),x[0])))
    raw+=record('TRAILER!!!',{'values':[0]*13,'body':b''});raw+=b'\0'*((-len(raw))%512)
    parsed=parse_cpio(raw)
    if set(parsed)!=set(entries) or any(parsed[n]['body']!=e['body'] for n,e in entries.items()):
        raise ValueError('newc serialization mismatch')
    rd=gzip.compress(raw,compresslevel=6,mtime=0)
    h=bytearray(header);struct.pack_into('<I',h,16,len(rd))
    ident=hashlib.sha1()
    for x in (kernel,rd,b'',b''):
        ident.update(x);ident.update(struct.pack('<I',len(x)))
    h[576:608]=ident.digest().ljust(32,b'\0')
    pad=lambda b:b+b'\0'*((-len(b))%4096)
    return bytes(h)+pad(kernel)+pad(rd)


def public_template(data):
    h,k,e=split(data)
    for n in list(e):
        if n.startswith('etc/dropbear/dropbear_') or n=='root/.ssh/authorized_keys':del e[n]
    put(e,'etc/dlt-production.env',b'PUBLIC_TEMPLATE=1\n')
    put(e,'etc/dlt-persistent.env',b'PUBLIC_TEMPLATE=1\n')
    put(e,'etc/dlt-ramroot.env',b'PUBLIC_TEMPLATE=1\n')
    put(e,'etc/native-debug-build',b'public-installer-template-v1.0.0-rc2\n',0o644)
    init=e['init']['body'].decode()
    init=init.replace('-r /etc/dropbear/dropbear_ed25519_host_key','-R')
    init=init.replace('sleep 900','sleep 3600')
    token='# Local controller selects a verified system image or the independent recovery.\nsleep 3\n'
    if init.count(token)!=1:raise ValueError('dispatcher insertion mismatch')
    init=init.replace(token,'''# This template is inert until a new owner personalizes it.
if test ! -s /root/.ssh/authorized_keys; then
    echo PUBLIC_TEMPLATE_NOT_PERSONALIZED
    while sleep 60; do :; done
fi
if test -f /etc/dlt-install-mode; then
    printf 'public-installer-session-v1\\n' >/run/dlt-public-install-session
    echo PUBLIC_INSTALL_RAM_SESSION_READY
    while sleep 5; do :; done
fi
sleep 3
''')
    put(e,'init',init.encode(),0o755)
    out=assemble(h,k,e)
    _,same,check=split(out)
    if same!=k or any(n.startswith('etc/dropbear/dropbear_') or n=='root/.ssh/authorized_keys' for n in check):
        raise ValueError('template kernel/identity check failed')
    return out


def personalize(data,public_key,layout,install=False):
    a=public_key.strip().split()
    if len(a)<2 or a[0]!='ssh-ed25519' or not re.fullmatch('[A-Za-z0-9+/=]+',a[1]):
        raise ValueError('Ed25519 owner public key required')
    h,k,e=split(data)
    if e.get('etc/native-debug-build',{}).get('body')!=b'public-installer-template-v1.0.0-rc2\n':
        raise ValueError('wrong boot template')
    put(e,'root/.ssh/authorized_keys',('ssh-ed25519 '+a[1]+'\n').encode())
    if install:put(e,'etc/dlt-install-mode',b'RAM installer only; no automatic formatting\n')
    else:
        for name in ('recovery_id','data_uuid','system_a_id','system_b_id'):
            if not re.fullmatch('[a-zA-Z0-9-]{1,96}',layout[name]):raise ValueError('invalid generated layout identity')
        env='\n'.join(k+'='+layout[v] for k,v in [('RECOVERY_ID','recovery_id'),('DATA_UUID','data_uuid'),
                    ('SYSTEM_A_ID','system_a_id'),('SYSTEM_B_ID','system_b_id')])+'\n'
        put(e,'etc/dlt-production.env',env.encode())
    return assemble(h,k,e)
