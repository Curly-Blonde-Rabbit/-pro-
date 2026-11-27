"""Phone-controlled SSH network exposure; USB maintenance remains independent."""
import pathlib,json,os,subprocess,fcntl,tempfile,time,hashlib,socket,signal,stat,shlex
STATE=pathlib.Path('/srv/dlt-state/ssh-exposure/policy.json')
CHAIN='DLT_SSH_ACCESS'
USB='172.31.255.1'
TOOLS=tuple('/usr/sbin/'+n if pathlib.Path('/usr/sbin/'+n).is_file() else '/opt/dlt-ssh-tools/usr/sbin/'+n for n in ('iptables-legacy','ip6tables-legacy'))
def cmd(a,check=True):
 env=None
 if str(a[0]).startswith('/opt/dlt-ssh-tools/'):
  env=dict(os.environ,LD_LIBRARY_PATH='/opt/dlt-ssh-tools/usr/lib/aarch64-linux-gnu:/opt/dlt-ssh-tools/lib/aarch64-linux-gnu',XTABLES_LIBDIR='/opt/dlt-ssh-tools/usr/lib/aarch64-linux-gnu/xtables')
 p=subprocess.run(a,capture_output=True,text=True,timeout=5,env=env)
 if check and p.returncode:raise RuntimeError('SSH 接口规则更新失败')
 return p

def desired():
 for parent in STATE.parents:
  st=parent.lstat()
  if not stat.S_ISDIR(st.st_mode) or st.st_uid or st.st_mode&0o022:raise ValueError('SSH settings parent is untrusted')
 try:fd=os.open(STATE,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 except FileNotFoundError:return False
 try:
  st=os.fstat(fd)
  if not stat.S_ISREG(st.st_mode) or st.st_uid or st.st_mode&0o077 or st.st_size>4096 or st.st_nlink!=1:raise ValueError('SSH settings file is untrusted')
  raw=os.read(fd,4097)
  if len(raw)>4096:raise ValueError('SSH settings file too large')
  value=json.loads(raw.decode('utf-8'))
  if not isinstance(value,dict) or type(value.get('schema'))is not int or value['schema']!=1 or type(value.get('network_enabled'))is not bool:raise ValueError('SSH settings object is invalid')
  return value['network_enabled']
 finally:os.close(fd)
def save(enabled):
 if type(enabled)is not bool:raise ValueError('SSH enabled must be a boolean')
 STATE.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 for parent in STATE.parents:
  st=parent.lstat()
  if not stat.S_ISDIR(st.st_mode) or st.st_uid or st.st_mode&0o022:raise ValueError('SSH settings parent is untrusted')
 fd,tmp=tempfile.mkstemp(prefix='.policy-',dir=STATE.parent)
 try:
  with os.fdopen(fd,'w') as f:
   os.fchmod(f.fileno(),0o600);json.dump({'schema':1,'network_enabled':enabled,'usb_maintenance_exempt':True,'updated':time.time()},f);f.flush();os.fsync(f.fileno())
  os.replace(tmp,STATE);fd=os.open(STATE.parent,os.O_DIRECTORY)
  try:os.fsync(fd)
  finally:os.close(fd)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
def rule(enabled):return ['-j','RETURN'] if enabled else ['!','-i','usb0','-p','tcp','--dport','22','-j','REJECT','--reject-with','tcp-reset']
def jump_first(tool):
 reply=cmd([tool,'-w','3','-S','INPUT'],False)
 if reply.returncode:return False
 rules=[shlex.split(line) for line in reply.stdout.splitlines() if line.startswith('-A ')]
 expected=['-A','INPUT','-p','tcp','--dport','22','-j',CHAIN]
 return bool(rules) and rules[0] in (expected,expected[:4]+['-m','tcp']+expected[4:])
def place_jump_first(tool):
 if jump_first(tool):return
 for _ in range(32):
  if cmd([tool,'-w','3','-C','INPUT','-p','tcp','--dport','22','-j',CHAIN],False).returncode:break
  cmd([tool,'-w','3','-D','INPUT','-p','tcp','--dport','22','-j',CHAIN])
 else:raise ValueError('Too many SSH gate jumps')
 cmd([tool,'-w','3','-I','INPUT','1','-p','tcp','--dport','22','-j',CHAIN])
 if not jump_first(tool):raise RuntimeError('SSH gate order verification failed')
def enabled_actual():
 result=[]
 for tool in TOOLS:
  if not pathlib.Path(tool).is_file() or not jump_first(tool):return None
  reply=cmd([tool,'-w','3','-S',CHAIN],False)
  if reply.returncode or len([line for line in reply.stdout.splitlines() if line.startswith('-A ')])!=1:return None
  off=cmd([tool,'-w','3','-C',CHAIN,*rule(False)],False)
  on=cmd([tool,'-w','3','-C',CHAIN,*rule(True)],False)
  result.append(False if not off.returncode and on.returncode else True if not on.returncode and off.returncode else None)
 return result[0] if result and result[0] is not None and len(set(result))==1 else None
def network_socket_inodes():
 found=set()
 for n in ('tcp','tcp6'):
  p=pathlib.Path('/proc/net')/n
  for line in p.read_text().splitlines()[1:]:
   f=line.split();addr,port=f[1].split(':')
   if int(port,16)!=22 or f[3]!='01':continue
   if n=='tcp':local=socket.inet_ntop(socket.AF_INET,bytes.fromhex(addr)[::-1])
   else:local=socket.inet_ntop(socket.AF_INET6,b''.join(bytes.fromhex(addr[i:i+8])[::-1] for i in range(0,32,8)))
   if local in (USB,'::ffff:'+USB):continue
   found.add(f[9])
 return found
def kick_network_sessions():
 # Kernel socket destroy if available. Never target the USB local address.
 cmd(['/usr/bin/ss','-K','state','established','sport = :22 and src != '+USB+' and src != ::ffff:'+USB],False)
 inodes=network_socket_inodes();killed=[]
 if inodes:
  for proc in pathlib.Path('/proc').iterdir():
   if not proc.name.isdigit():continue
   try:
    if (proc/'comm').read_text().strip()!='sshd':continue
    targets={p.readlink().as_posix()[8:-1] for p in (proc/'fd').iterdir() if p.is_symlink() and p.readlink().as_posix().startswith('socket:[')}
    if targets&inodes:os.kill(int(proc.name),signal.SIGTERM);killed.append(int(proc.name))
   except (OSError,ValueError,ProcessLookupError):continue
  time.sleep(.15)
 return {'terminated_sshd_children':len(killed),'remaining_network_sessions':len(network_socket_inodes())}
def apply(enabled,origin='boot'):
 if type(enabled)is not bool:raise ValueError('SSH 开关参数异常')
 if os.geteuid():raise PermissionError('SSH 设置需要手机管理服务')
 if origin not in ('physical-phone','boot','maintenance-test'):raise PermissionError('请在手机实体屏幕设置 SSH')
 lock=pathlib.Path('/run/dlt-ssh-exposure.lock')
 with lock.open('a') as f:
  os.chmod(lock,0o600);fcntl.flock(f,fcntl.LOCK_EX)
  for tool in TOOLS:
   if not pathlib.Path(tool).is_file():raise RuntimeError('SSH IPv4/IPv6 控制组件未就绪')
  previous=enabled_actual()
  try:
   install_rules(enabled)
   save(enabled)
  except Exception:
   # A failed write must not leave an unsuccessful enable exposed.
   install_rules(previous if type(previous)is bool else False)
   raise
  sessions={} if enabled else kick_network_sessions()
  result=info();result.update(sessions);return result

def install_rules(enabled):
 # Create the pair without flushing any unrelated firewall chain/rule.
 for tool in TOOLS:
  cmd([tool,'-w','3','-N',CHAIN],False)
  place_jump_first(tool)
  existing=cmd([tool,'-w','3','-S',CHAIN]).stdout.splitlines()
  # Our chain must contain zero/one of the two exact owned rules.
  if len([x for x in existing if x.startswith('-A ')])>1:raise ValueError('SSH 规则被外部修改')
  if any(not line.startswith(('-N '+CHAIN,'-A '+CHAIN)) for line in existing):raise ValueError('SSH 规则异常')
  if not cmd([tool,'-w','3','-C',CHAIN,*rule(not enabled)],False).returncode:cmd([tool,'-w','3','-R',CHAIN,'1',*rule(enabled)])
  elif cmd([tool,'-w','3','-C',CHAIN,*rule(enabled)],False).returncode:
   if any(x.startswith('-A ') for x in existing):raise ValueError('SSH 规则被外部修改')
   cmd([tool,'-w','3','-A',CHAIN,*rule(enabled)])
 if enabled_actual()is not enabled:raise RuntimeError('SSH 接口读回校验失败')

def info():
 try:wanted=desired();error=None
 except (OSError,ValueError,TypeError):wanted=False;error='SSH 设置需修复'
 actual=enabled_actual();ready=actual is not None and error is None and actual is wanted
 return {'network_enabled':actual,'configured_enabled':wanted,'ready':ready,'label':('SSH 已开启' if actual else 'SSH 已关闭') if ready else 'SSH 待检查','usb_maintenance_preserved':True,'disconnect_existing_network_sessions':True,'error':error}
def switch(enabled,origin):
 if origin!='physical-phone':raise PermissionError('请在手机实体屏幕设置 SSH')
 return apply(enabled,origin)
if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('action',choices=['apply-saved','status','maintenance-on','maintenance-off']);a=p.parse_args()
 if a.action=='status':r=info()
 elif a.action=='apply-saved':
  try:value=desired()
  except (OSError,ValueError,TypeError):value=False
  r=apply(value,'boot')
 else:r=apply(a.action=='maintenance-on','maintenance-test')
 print(json.dumps(r))
