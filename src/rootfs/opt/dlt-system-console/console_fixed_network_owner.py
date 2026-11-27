"""Verify owner-specific control-plane filter before opening overlay inbound.
Never touches host firewall, DNS, routing, keys or the phone SSH switch.
"""
import pathlib,json,subprocess,os,stat,time,hashlib,ipaddress
STATE=pathlib.Path('/srv/dlt-state/fixed-network')
POLICY=STATE/'owner-ssh-access.json'
REPORT=pathlib.Path('/run/dlt-fixed-network-owner.json')
BIN='/usr/local/lib/dlt-fixed-network/tailscale'
SOCKET='/run/dlt-fixed-network/tailscaled.sock'
DROPIN=pathlib.Path('/etc/ssh/sshd_config.d/15-dlt-overlay-owner.conf')
def command(args,check=True,timeout=8):
 p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
 if check and p.returncode:raise RuntimeError('overlay control command failed')
 return p

def cli(*args):return command([BIN,'--socket='+SOCKET,*args])
def load_private(p):
 st=p.lstat()
 if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_mode&0o077 or st.st_size>8192:raise ValueError('unsafe owner policy')
 return json.loads(p.read_text())
def validate_profile(p):
 if p.get('schema')!=1 or p.get('enabled') is not True:raise ValueError('owner access disabled')
 for key in ['computer_stable_id','phone_stable_id']:
  if not isinstance(p.get(key),str) or not p[key] or len(p[key])>100:raise ValueError('invalid stable ID')
 for key in ['computer_addresses','phone_addresses']:
  values=p.get(key)
  if not isinstance(values,list) or len(values)!=2:raise ValueError('two owner addresses required')
  nets=[ipaddress.ip_network(v,strict=True) for v in values]
  if {n.version for n in nets}!={4,6} or any(n.prefixlen!=n.max_prefixlen for n in nets):raise ValueError('only exact node addresses permitted')
  if any(not(n.subnet_of(ipaddress.ip_network('100.64.0.0/10')) if n.version==4 else n.subnet_of(ipaddress.ip_network('fd7a:115c:a1e0::/48'))) for n in nets):raise ValueError('not overlay addresses')
 if not isinstance(p.get('SSH_dropin_sha256'),str) or len(p['SSH_dropin_sha256'])!=64:raise ValueError('missing SSH binding')
 return p

def verify_netmap(d,p):
 validate_profile(p)
 if d.get('Cached') is True:raise ValueError('network map not fresh')
 s=d.get('SelfNode') or {}
 if s.get('StableID')!=p['phone_stable_id'] or set(s.get('Addresses',[]))!=set(p['phone_addresses']):raise ValueError('phone identity mismatch')
 peers=[x for x in d.get('Peers',[]) if x.get('StableID')==p['computer_stable_id']]
 if len(peers)!=1 or set(peers[0].get('Addresses',[]))!=set(p['computer_addresses']):raise ValueError('owner computer identity mismatch')
 rules=d.get('PacketFilterRules')
 if not isinstance(rules,list) or len(rules)!=1:raise ValueError('expected exactly one owner SSH rule')
 r=rules[0]
 if set(r)!={'SrcIPs','IPProto','DstPorts'} or r['IPProto']!=[6]:raise ValueError('unexpected protocol or filter fields')
 expected_src={str(ipaddress.ip_interface(v).ip) for v in p['computer_addresses']}
 if not isinstance(r['SrcIPs'],list) or len(r['SrcIPs'])!=2 or set(r['SrcIPs'])!=expected_src:raise ValueError('extra or missing source')
 expected_dst={str(ipaddress.ip_interface(v).ip) for v in p['phone_addresses']}
 dests=r.get('DstPorts')
 if not isinstance(dests,list) or len(dests)!=2:raise ValueError('extra destinations')
 if {x.get('IP') for x in dests}!=expected_dst:raise ValueError('destination mismatch')
 for x in dests:
  if set(x)!={'IP','Ports'} or x['Ports']!={'First':22,'Last':22}:raise ValueError('ports other than SSH present')
 f=d.get('PacketFilter')
 if not isinstance(f,list) or len(f)!=1:raise ValueError('compiled filter mismatch')
 f=f[0]
 if f.get('IPProto')!=[6] or len(f.get('Srcs',[]))!=2 or set(f['Srcs'])!=set(p['computer_addresses']) or f.get('Caps') or f.get('SrcCaps'):raise ValueError('compiled source or capability mismatch')
 destinations=f.get('Dsts',[])
 if len(destinations)!=2 or {x.get('Net') for x in destinations}!=set(p['phone_addresses']) or any(x.get('Ports')!={'First':22,'Last':22} for x in destinations):raise ValueError('compiled destination mismatch')
 return True

def write_report(value):
 tmp=REPORT.with_name('.dlt-fixed-network-owner.tmp')
 fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 try:
  with os.fdopen(fd,'w') as f:json.dump(value,f,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
  tmp.replace(REPORT)
 finally:
  if tmp.exists():tmp.unlink()
def guard():
 if os.geteuid()!=0:raise PermissionError('owner maintenance required')
 allowed=False;reason='owner policy not verified';prefs={}
 try:
  profile=validate_profile(load_private(POLICY))
  st=DROPIN.lstat()
  if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_mode&0o022 or hashlib.sha256(DROPIN.read_bytes()).hexdigest()!=profile['SSH_dropin_sha256']:raise ValueError('SSH owner binding changed')
  netmap=json.loads(cli('debug','netmap').stdout);verify_netmap(netmap,profile)
  prefs=json.loads(cli('debug','prefs').stdout)
  if any(prefs.get(k) is not False for k in ['RunSSH','CorpDNS','RouteAll']) or prefs.get('NetfilterMode')!=0 or prefs.get('ExitNodeID') or prefs.get('AdvertiseRoutes'):raise ValueError('unexpected overlay preferences')
  allowed=True;reason='exact owner TCP22 rule and SSH destination binding verified'
 except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired,KeyError,TypeError,AttributeError):reason='owner policy, identity, filter or configuration not verified'
 try:
  if not prefs:prefs=json.loads(cli('debug','prefs').stdout)
  desired=not allowed
  if prefs.get('ShieldsUp') is not desired:cli('set','--shields-up='+str(desired).lower())
  actual=json.loads(cli('debug','prefs').stdout).get('ShieldsUp')
  if actual is not desired:raise RuntimeError('shield preference not confirmed')
  result={'schema':1,'policy_match':allowed,'inbound_open':actual is False,'reason':reason,'updated_monotonic':time.monotonic(),'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'host_firewall_touched':False}
 except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired,TypeError,AttributeError):
  result={'schema':1,'policy_match':False,'inbound_open':False,'reason':'overlay daemon unavailable or control not confirmed','updated_monotonic':time.monotonic(),'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'host_firewall_touched':False}
 write_report(result);return result
if __name__=='__main__':print(json.dumps(guard()))
