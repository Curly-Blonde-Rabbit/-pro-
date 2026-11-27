"""Owner-controlled overlay. Never configures host firewall, DNS or SSH."""

import json,pathlib,subprocess,os,re,urllib.parse,fcntl,stat

BIN='/usr/local/lib/dlt-fixed-network'

STATE=pathlib.Path('/srv/dlt-state/fixed-network')

SOCKET='/run/dlt-fixed-network/tailscaled.sock'

SERVICE='dlt-fixed-network.service'

ADMIN_URL='https://login.tailscale.com/admin/machines'

def run(args,timeout=8,check=True):

    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)

    if check and p.returncode:raise RuntimeError('固定 IP 操作失败，请检查模块日志')

    return p



def cli(*args,timeout=8,check=True):return run([BIN+'/tailscale','--socket='+SOCKET,*args],timeout,check)

def safe_url(value):

    try:

        u=urllib.parse.urlsplit(value)

        return value if u.scheme=='https' and u.netloc=='login.tailscale.com' and u.path.startswith('/a/') and not u.fragment and not u.query and len(value)<256 else ''

    except (ValueError,TypeError):return ''

def active():return run(['systemctl','is-active','--quiet',SERVICE],check=False).returncode==0

def auth_url():

    try:

        p=STATE/'login.json';st=p.lstat()

        if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_mode&0o077 or st.st_size>4096:return ''

        return safe_url(json.loads(p.read_text()).get('url',''))

    except (OSError,ValueError,TypeError,AttributeError):return ''

def info():

    installed=pathlib.Path(BIN+'/tailscale').is_file();running=installed and active()

    value={'installed':installed,'active':running,'state':'Stopped','label':'已关闭','ips':[],'hostname':'darklighter-server','login_url':'','admin_url':ADMIN_URL,'inbound_locked':True,'ssh_unchanged':True}

    if running:

        p=cli('status','--json',check=False)

        try:data=json.loads(p.stdout)

        except ValueError:data={}

        state=data.get('BackendState','Unavailable');me=data.get('Self') or {}

        value.update(state=state,ips=data.get('TailscaleIPs') or [],hostname=me.get('HostName') or 'darklighter-server')

        value['label']={'Running':'已连接 · 接入待核验','NeedsLogin':'待登录','NeedsMachineAuth':'待账号批准','Stopped':'已断开','Starting':'连接中'}.get(state,'状态待检查')

        value['login_url']=(safe_url(data.get('AuthURL','')) or auth_url()) if state=='NeedsLogin' else ''
        try:
            prefs=json.loads(cli('debug','prefs').stdout)
            value['inbound_locked']=prefs.get('ShieldsUp') is not False
        except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired,TypeError,AttributeError):pass
        value['owner_policy_verified']=False
        try:
            report=pathlib.Path('/run/dlt-fixed-network-owner.json');st=report.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_mode&0o077 or st.st_size>4096:raise ValueError('unsafe guard status')
            guard=json.loads(report.read_text())
            fresh=guard.get('boot_id')==pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip() and 0<=__import__('time').monotonic()-guard.get('updated_monotonic',-999)<=45
            value['owner_policy_verified']=fresh and guard.get('policy_match') is True and guard.get('inbound_open') is True
        except (OSError,ValueError,TypeError,AttributeError):pass
        if state=='Running' and not value['inbound_locked'] and value['owner_policy_verified']:
            import sys
            sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
            from console_ssh_exposure import info as ssh_info
            value['label']='已连接 · SSH 就绪' if ssh_info().get('network_enabled') is True else '已连接 · SSH 已关闭'
        elif state=='Running':value['label']='已连接 · SSH 待开放'

    try:

        c=json.loads((STATE/'policy.json').read_text());value['hostname']=c['hostname']

    except (OSError,ValueError,TypeError,KeyError):pass

    return value



def atomic(path,data):

    tmp=path.with_name('.'+path.name+'.tmp')

    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)

    try:

        with os.fdopen(fd,'wb') as f:f.write((json.dumps(data,sort_keys=True)+'\n').encode());f.flush();os.fsync(f.fileno())

        os.replace(tmp,path)

    finally:

        if tmp.exists():tmp.unlink()

def locked():

    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)

    st=STATE.lstat()

    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or st.st_mode&0o077:raise RuntimeError('模块数据目录权限异常')

    fd=os.open(STATE/'operation.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)

    st=os.fstat(fd)

    if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_mode&0o077:os.close(fd);raise RuntimeError('模块锁权限异常')

    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)

    except BlockingIOError:os.close(fd);raise RuntimeError('已有固定 IP 操作，请稍后')

    return fd

def flags(name):return ['--accept-dns=false','--accept-routes=false','--netfilter-mode=off','--ssh=false','--shields-up=true','--advertise-routes=','--advertise-exit-node=false','--exit-node=','--hostname='+name]

def action(kind,origin,name=None):

    if origin not in ('physical-phone','owner-maintenance'):raise PermissionError('请在手机实体屏幕设置固定 IP')

    if os.geteuid()!=0:raise PermissionError('需要设备所有者权限')

    if kind not in ('enable','disable','login','rename'):raise ValueError('固定 IP 操作无效')

    if kind=='rename' and (not isinstance(name,str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',name)):raise ValueError('名称用小写字母、数字和短横线，最长63位')

    fd=locked()

    try:

        config=STATE/'policy.json'

        try:policy=json.loads(config.read_text())

        except FileNotFoundError:policy={'schema':1,'hostname':'darklighter-server'}

        if kind=='disable':

            policy['enabled']=False;atomic(config,policy)

            run(['systemctl','stop',SERVICE],timeout=20)

        elif kind=='rename':

            if active():cli('set','--hostname='+name)

            policy['hostname']=name;atomic(config,policy)

        else:

            policy['enabled']=True;atomic(config,policy)

            run(['systemctl','enable','--now',SERVICE],timeout=20)

            # --timeout bounds authentication wait; keep private link out of journal/public state.

            p=cli('up',*flags(policy['hostname']),'--timeout=8s',timeout=14,check=False)

            m=re.search(r'https://login\.tailscale\.com/a/[A-Za-z0-9]+',p.stdout+' '+p.stderr)

            if m and safe_url(m.group(0)):atomic(STATE/'login.json',{'url':m.group(0)})

            if p.returncode and not m:

                state=info()['state']

                if state not in ('NeedsLogin','NeedsMachineAuth','Running','Starting'):raise RuntimeError('固定 IP 连接未就绪')

        return info()

    finally:os.close(fd)

def startup_allowed():

    try:

        f=STATE/'policy.json';st=f.lstat()

        return bool(stat.S_ISREG(st.st_mode) and st.st_uid==0 and not st.st_mode&0o077 and st.st_size<4096 and json.loads(f.read_text()).get('enabled') is True)

    except (OSError,ValueError,TypeError,AttributeError):return False

if __name__=='__main__':

    import argparse

    a=argparse.ArgumentParser();a.add_argument('action',choices=['status','enable','disable','login','rename','startup-check']);a.add_argument('--name');args=a.parse_args()

    if args.action=='startup-check':raise SystemExit(0 if startup_allowed() else 1)

    v=info() if args.action=='status' else action(args.action,'owner-maintenance',args.name)

    print(json.dumps(v,ensure_ascii=False))

