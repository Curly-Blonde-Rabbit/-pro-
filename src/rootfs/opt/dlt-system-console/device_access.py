"""Phone-owned device enrollment. New grants have a fixed, non-root operator role."""
import base64,contextlib,datetime,hashlib,ipaddress,json,os,pathlib,secrets,struct,subprocess,time,unicodedata

ROOT=pathlib.Path('/srv/dlt-state/device-access')

def username_for(key_id):
    if not isinstance(key_id,str) or len(key_id)!=32 or any(c not in '0123456789abcdef' for c in key_id):raise ValueError('invalid key identity')
    return 'dltu_'+key_id[:12]

class Accounts:
    """Separate non-root Linux identities; no sudo/docker/input/disk membership."""
    def create(self,key_id):
        import pwd
        user=username_for(key_id)
        try:pwd.getpwnam(user)
        except KeyError:pass
        else:raise RuntimeError('account name already exists; refusing to adopt it')
        base=pathlib.Path('/srv/dlt-device-users')
        base.mkdir(parents=True,mode=0o755,exist_ok=True)
        if base.is_symlink() or base.stat().st_uid!=0 or base.stat().st_mode&0o022:raise PermissionError('unsafe home directory parent')
        home=base/user
        if home.exists() or home.is_symlink():raise RuntimeError('home already exists; manual recovery required')
        p=subprocess.run(['/usr/sbin/useradd','--create-home','--home-dir',str(home),'--shell','/bin/bash','--groups','dlt-inet','--no-log-init',user],capture_output=True,text=True,timeout=15)
        if p.returncode:raise RuntimeError('ordinary account creation failed')
        entry=pwd.getpwnam(user)
        if entry.pw_uid<1000 or entry.pw_uid==pwd.getpwnam('ops').pw_uid:raise RuntimeError('unsafe UID')
        home.chmod(0o700)
        return {'username':user,'uid':entry.pw_uid,'gid':entry.pw_gid,'home':str(home),'shell':'/bin/bash','root':False}
    def disable(self,user):
        import pwd
        if not user.startswith('dltu_') or len(user)!=17 or any(c not in '0123456789abcdef' for c in user[5:]):raise ValueError('invalid managed account')
        e=pwd.getpwnam(user)
        if e.pw_uid<1000 or e.pw_uid==pwd.getpwnam('ops').pw_uid:raise PermissionError('protected UID')
        subprocess.run(['/usr/sbin/usermod','--lock','--shell','/usr/sbin/nologin',user],check=True,capture_output=True,timeout=15)
        # Terminate this exact ordinary UID, never ops/root; preserve its files.
        p=subprocess.run(['/usr/bin/pkill','-KILL','-u',str(e.pw_uid)],capture_output=True,timeout=5)
        if p.returncode not in (0,1):raise RuntimeError('account disabled; session termination failed')
        return {'disabled':True,'data_preserved':True}

def public_key(blob):
    if not isinstance(blob,str) or len(blob)>128:raise ValueError('invalid public key')
    try:raw=base64.b64decode(blob,validate=True)
    except Exception:raise ValueError('invalid public key')
    expected=struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)
    if len(raw)!=51 or not raw.startswith(expected):raise ValueError('only Ed25519 keys are accepted')
    canonical=base64.b64encode(raw).decode()
    if blob!=canonical:raise ValueError('noncanonical public key')
    digest=hashlib.sha256(raw).digest()
    return {'id':digest.hex()[:32],'key':'ssh-ed25519 '+canonical,'blob':canonical,
            'fingerprint':'SHA256:'+base64.b64encode(digest).decode().rstrip('=')}

def clean_name(value):
    if not isinstance(value,str):raise ValueError('invalid device name')
    value=''.join(c for c in value if unicodedata.category(c) not in ('Cc','Cf','Cs')).strip()
    if not value or len(value)>48:raise ValueError('device name must be 1 to 48 visible characters')
    return value

def atomic(path,data,mode=0o600):
    path=pathlib.Path(path);tmp=path.with_name('.'+path.name+'-'+secrets.token_hex(8))
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if tmp.exists():tmp.unlink()

class Store:
    def __init__(self,root=ROOT,accounts=None):self.root=pathlib.Path(root);self.accounts=accounts or Accounts()
    @contextlib.contextmanager
    def transaction(self,write=True):
        import fcntl
        if os.geteuid()!=0:raise PermissionError('phone authority required')
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_uid!=0 or self.root.stat().st_mode&0o077:raise PermissionError('unsafe authority store')
        with (self.root/'lock').open('a') as f:
            os.chmod(self.root/'lock',0o600);fcntl.flock(f,fcntl.LOCK_EX)
            path=self.root/'registry.json'
            if not path.exists():raise RuntimeError('authority store not initialized')
            if path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode&0o077:raise PermissionError('unsafe registry')
            s=json.loads(path.read_text())
            if s.get('schema')!=1:raise RuntimeError('unsupported authority schema')
            yield s
            if write:atomic(path,(json.dumps(s,ensure_ascii=False,indent=2)+'\n').encode())
    def initialize(self,bootstrap_blob):
        k=public_key(bootstrap_blob);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        p=self.root/'registry.json'
        if p.exists():raise RuntimeError('authority store already exists')
        value={'schema':1,'devices':{k['id']:dict(k,name='当前电脑',role='maintenance-existing',protected=True,approved_at=time.time())},'pending':{},'cooldown':{},'audit':[],
               'phone_is_grant_authority':True,'existing_ops_unrestricted_transition':True}
        atomic(p,(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())
    @staticmethod
    def prune(s):
        now=time.time()
        s['pending']={k:v for k,v in s['pending'].items() if v['expires']>now}
        s['cooldown']={k:v for k,v in s['cooldown'].items() if v>now}
        s['audit']=s['audit'][-99:]
    def request(self,blob,name,source):
        k=public_key(blob);name=clean_name(name);source=str(ipaddress.ip_address(source))
        with self.transaction() as s:
            self.prune(s)
            if k['id'] in s['devices']:
                role=s['devices'][k['id']]['role']
                return {'state':'already-authorized','role':role,'login':'ops' if role=='maintenance-existing' else s['devices'][k['id']]['username']}
            if k['id'] in s['cooldown']:raise RuntimeError('request cooldown active')
            if k['id'] in s['pending']:return {'state':'pending','request_id':s['pending'][k['id']]['request_id'],'fingerprint':k['fingerprint']}
            if len(s['pending'])>=16:raise RuntimeError('pending request queue full')
            if sum(v['source']==source for v in s['pending'].values())>=3:raise RuntimeError('source request limit reached')
            r=dict(k,name=name,source=source,request_id=secrets.token_hex(16),created=time.time(),expires=time.time()+600,role='operator')
            s['pending'][k['id']]=r
            s['audit'].append({'event':'request','id':k['id'],'time':time.time()})
            return {'state':'pending','request_id':r['request_id'],'fingerprint':k['fingerprint'],'default_role':'operator','login_after_approval':username_for(k['id']),'shell_after_approval':'non-root','message':'请在手机超级管理员页面核对指纹并批准；申请尚未获得管理权限。'}
    def view(self):
        with self.transaction(write=False) as s:
            self.prune(s)
            def safe(v):return {k:v[k] for k in ('id','name','fingerprint','role','protected','source','request_id','expires','username','home') if k in v}
            return {'devices':[safe(v) for v in s['devices'].values()],'pending':[safe(v) for v in s['pending'].values()],
                    'phone_authority':True,'new_device_default_role':'operator','ordinary_access':'isolated-non-root-SSH-shell','ops_transition_unrestricted':s['existing_ops_unrestricted_transition']}
    def decide(self,request_id,approve,origin):
        if origin!='physical-phone':raise PermissionError('仅手机实体触摸可确认设备授权')
        if type(approve) is not bool:raise ValueError('invalid decision')
        created=None
        try:
            with self.transaction() as s:
                self.prune(s)
                r=next((v for v in s['pending'].values() if v['request_id']==request_id),None)
                if r is None:raise RuntimeError('申请已过期或已处理')
                if approve:
                    created=self.accounts.create(r['id'])
                    s['devices'][r['id']]={k:r[k] for k in ('id','key','blob','fingerprint','name','role')} | {'protected':False,'approved_at':time.time()} | created
                else:s['cooldown'][r['id']]=time.time()+600
                del s['pending'][r['id']]
                s['audit'].append({'event':'approve-operator-shell' if approve else 'deny','id':r['id'],'time':time.time()})
            return {'approved':approve,'role':'operator','root':False,'terminal':approve,'can_grant_devices':False,'login':created['username'] if created else None}
        except BaseException:
            if created:self.accounts.disable(created['username'])
            raise
    def revoke(self,device_id,origin):
        if origin!='physical-phone':raise PermissionError('仅手机实体触摸可撤销设备授权')
        with self.transaction() as s:
            if device_id not in s['devices']:raise RuntimeError('设备已移除')
            if s['devices'][device_id].get('protected'):raise RuntimeError('当前维护电脑受防锁出保护，本轮不撤销')
            username=s['devices'][device_id].get('username')
            del s['devices'][device_id];s['cooldown'][device_id]=time.time()+600
            s['audit'].append({'event':'revoke','id':device_id,'time':time.time()})
        if username:self.accounts.disable(username)
        return {'revoked':True,'data_preserved':True}
    def operator(self,blob):
        k=public_key(blob)
        with self.transaction(write=False) as s:return s['devices'].get(k['id'],{}).get('role')=='operator'
    def login_allowed(self,user,blob):
        k=public_key(blob)
        with self.transaction(write=False) as s:
            d=s['devices'].get(k['id'],{})
            return d.get('role')=='operator' and d.get('username')==user

def key_lookup(user,keytype,blob,store=None):
    if keytype!='ssh-ed25519':return ''
    k=public_key(blob)
    if user=='dltjoin':return 'restrict,command="/usr/local/bin/dlt-access-entry join '+k['blob']+'" '+k['key']+'\n'
    if user.startswith('dltu_') and (store or Store()).login_allowed(user,blob):return 'restrict,pty '+k['key']+'\n'
    return ''
