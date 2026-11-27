#!/usr/bin/python3 -I
"""Rebuild only phone-approved ordinary identities; never rewrite ops/root/SSH."""
import pathlib,os,json,subprocess,time,shutil,uuid,argparse,contextlib,fcntl,stat,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
from device_access import Store,public_key,username_for,atomic

BASE=pathlib.Path('/srv/dlt-device-users')
INTERNET_GID=3003

def parse_users(text):
    users={};uids={}
    for line in text.splitlines():
        if not line or line.startswith('#'):continue
        p=line.split(':')
        if len(p)!=7:raise ValueError('invalid passwd database')
        uid=int(p[2]);gid=int(p[3])
        if p[0] in users or uid in uids:raise ValueError('duplicate user identity')
        users[p[0]]={'username':p[0],'uid':uid,'gid':gid,'home':p[5],'shell':p[6]};uids[uid]=p[0]
    return users,uids

def parse_groups(text):
    groups={};gids={}
    for line in text.splitlines():
        if not line or line.startswith('#'):continue
        p=line.split(':')
        if len(p)!=4:raise ValueError('invalid group database')
        gid=int(p[2])
        if p[0] in groups or gid in gids:raise ValueError('duplicate group identity')
        groups[p[0]]={'name':p[0],'gid':gid,'members':set(p[3].split(','))-{''}};gids[gid]=p[0]
    return groups,gids

def plan_restore(registry,passwd,group):
    if not isinstance(registry,dict) or registry.get('schema')!=1 or not isinstance(registry.get('devices'),dict):raise ValueError('invalid device registry')
    users,uids=parse_users(passwd);groups,gids=parse_groups(group)
    ops=users.get('ops')
    if not ops or ops['uid']!=1000 or users.get('root',{}).get('uid')!=0:raise ValueError('protected account baseline mismatch')
    if groups.get('dlt-inet',{}).get('gid')!=INTERNET_GID:raise ValueError('internet group identity mismatch')
    targets=[];desired_uids=set();desired_gids=set();names=set()
    for key_id,device in registry['devices'].items():
        if not isinstance(device,dict):raise ValueError('invalid registry device')
        key=public_key(device.get('blob'))
        if key_id!=key['id'] or device.get('id')!=key_id or device.get('key')!=key['key']:raise ValueError('device public identity mismatch')
        role=device.get('role')
        if role=='maintenance-existing':
            if device.get('protected') is not True:raise ValueError('maintenance account protection missing')
            continue
        if role!='operator' or device.get('root') is not False or device.get('protected') is not False:raise ValueError('unexpected grant role')
        user=username_for(key_id);uid=device.get('uid');gid=device.get('gid')
        if type(uid) is not int or type(gid) is not int or not 1002<=uid<=59999 or not 1002<=gid<=59999 or uid==INTERNET_GID or gid==INTERNET_GID:raise ValueError('unsafe/missing persistent UID or GID')
        home=str(BASE/user)
        if device.get('username')!=user or device.get('home')!=home or device.get('shell')!='/bin/bash':raise ValueError('managed account identity mismatch')
        if uid in desired_uids or gid in desired_gids or user in names:raise ValueError('duplicate desired account identity')
        desired_uids.add(uid);desired_gids.add(gid);names.add(user)
        if uid in uids and uids[uid]!=user:raise ValueError('UID collision; refusing reassignment')
        if gid in gids and gids[gid]!=user:raise ValueError('GID collision; refusing reassignment')
        if user in users and any(users[user][k]!=v for k,v in [('uid',uid),('gid',gid),('home',home),('shell','/bin/bash')]):raise ValueError('existing user differs; no overwrite')
        if user in groups and groups[user]['gid']!=gid:raise ValueError('existing private group differs')
        if user in groups and groups[user]['members']-{user}:raise ValueError('private group has foreign members')
        memberships={g['name'] for g in groups.values() if user in g['members']}
        if memberships-{'dlt-inet',user}:raise ValueError('ordinary account has unexpected group privileges')
        if user in users and 'dlt-inet' not in memberships:raise ValueError('existing ordinary internet group missing')
        targets.append({'username':user,'uid':uid,'gid':gid,'home':home,'shell':'/bin/bash','create_user':user not in users,'create_group':user not in groups})
    return {'schema':1,'accounts':targets,'protected_accounts_unchanged':['root','ops','dltjoin'],'SSH_keys_rewritten':False,'recursive_chown':False}

class AccountDB:
    def __init__(self,root='/'):
        self.root=pathlib.Path(root)
        if not self.root.is_absolute() or self.root.is_symlink():raise ValueError('invalid account root')
        if str(self.root)!='/':
            resolved=self.root.resolve()
            if not str(resolved).startswith('/run/dlt-identity-test-') or not (self.root/'.isolated-identity-test').is_file():raise ValueError('offline account target must be explicit isolated test root')
        for p in (self.root/'etc/passwd',self.root/'etc/group'):
            if p.is_symlink() or not p.is_file():raise ValueError('invalid account database path')
    def read(self):return (self.root/'etc/passwd').read_text(),(self.root/'etc/group').read_text()
    def command(self,args):
        tool=args[0];argv=[tool]
        if str(self.root)!='/':argv+=['--prefix',str(self.root)]
        argv+=args[1:]
        p=subprocess.run(argv,capture_output=True,text=True,timeout=15)
        if p.returncode:raise RuntimeError('identity reconstruction command failed: '+pathlib.Path(tool).name)
    def home(self,path):return self.root/path.lstrip('/')

def apply_restore(registry,db,backup_root):
    passwd,group=db.read();plan=plan_restore(registry,passwd,group)
    # Validate every existing data directory before making any account change.
    for account in plan['accounts']:
        p=db.home(account['home'])
        for parent in (p,*p.parents):
            if parent.is_symlink():raise ValueError('home path symlink rejected')
        if p.exists():
            st=p.stat()
            if not p.is_dir() or st.st_uid!=account['uid'] or st.st_gid!=account['gid'] or st.st_mode&0o077:raise ValueError('preserved home ownership/mode mismatch; never recursively reassign')
    needed=[v for v in plan['accounts'] if v['create_user'] or v['create_group']]
    if not needed:return dict(plan,applied=True,changes=0,backup=None)
    backup=pathlib.Path(backup_root)/('identity-'+uuid.uuid4().hex);backup.mkdir(parents=True,mode=0o700,exist_ok=False)
    for name in ('passwd','shadow','group','gshadow'):
        p=db.root/'etc'/name
        if p.exists():
            if p.is_symlink():raise ValueError('account database symlink rejected')
            shutil.copyfile(p,backup/name);(backup/name).chmod(0o600)
    receipt={'schema':1,'phase':'preparing','target_root':str(db.root),'created_users':[],'created_groups':[],'plan':plan,'protected_accounts_changed':False,'preserved_data_reassigned':False}
    atomic(backup/'receipt.json',(json.dumps(receipt,indent=2)+'\n').encode())
    try:
        for account in needed:
            user=account['username']
            if account['create_group']:
                db.command(['/usr/sbin/groupadd','--gid',str(account['gid']),user]);receipt['created_groups'].append(user)
            if account['create_user']:
                db.command(['/usr/sbin/useradd','--uid',str(account['uid']),'--gid',str(account['gid']),'--groups','dlt-inet','--home-dir',account['home'],'--shell','/bin/bash','--no-create-home','--no-log-init',user]);receipt['created_users'].append(user)
                home=db.home(account['home'])
                if not home.exists():
                    home.parent.mkdir(mode=0o755,parents=True,exist_ok=True);home.mkdir(mode=0o700)
                    os.chown(home,account['uid'],account['gid'])
            atomic(backup/'receipt.json',(json.dumps(receipt,indent=2)+'\n').encode())
        now_passwd,now_group=db.read();verified=plan_restore(registry,now_passwd,now_group)
        if any(v['create_user'] or v['create_group'] for v in verified['accounts']):raise RuntimeError('identity reconstruction readback mismatch')
        before_users,_=parse_users(passwd);after_users,_=parse_users(now_passwd)
        for user in ('root','ops','dltjoin'):
            if before_users.get(user)!=after_users.get(user):raise RuntimeError('protected account changed')
        receipt.update(phase='completed',uid_gid_readback_verified=True);atomic(backup/'receipt.json',(json.dumps(receipt,indent=2)+'\n').encode())
        return dict(plan,applied=True,changes=len(needed),backup=str(backup),uid_gid_readback_verified=True)
    except BaseException:
        # Partial accounts never receive an SSH key through this tool. Leave data
        # and exact backups for repair rather than replacing all live account DBs.
        disabled=[]
        for user in receipt['created_users']:
            try:
                db.command(['/usr/sbin/usermod','--lock','--shell','/usr/sbin/nologin','--expiredate','1',user]);disabled.append(user)
            except Exception:pass
        receipt.update(phase='failed-no-global-rollback',requires_review=True,partial_users_disabled=disabled)
        atomic(backup/'receipt.json',(json.dumps(receipt,indent=2)+'\n').encode());raise

def reconcile(apply=False):
    db=AccountDB();store=Store()
    with store.transaction(write=False) as registry:
        passwd,group=db.read();plan=plan_restore(registry,passwd,group)
        return apply_restore(registry,db,'/srv/dlt-state/identity-recovery-backups') if apply else dict(plan,applied=False)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if os.geteuid()!=0:raise PermissionError('root required')
    p=pathlib.Path('/run/dlt-access-identity-restore.lock');fd=os.open(p,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=reconcile(args.apply);result.update(boot_id=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),time=time.time())
        atomic(pathlib.Path('/run/dlt-access-identity-restore.json'),(json.dumps(result,indent=2)+'\n').encode())
        print(json.dumps(result))
    finally:os.close(fd)
if __name__=='__main__':main()
