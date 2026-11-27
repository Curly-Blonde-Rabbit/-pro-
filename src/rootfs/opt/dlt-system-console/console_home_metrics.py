"""Nonblocking home tile metrics. All system sampling runs off the display thread."""
import pathlib,os,shutil,time,threading,re,json,subprocess,ipaddress,unicodedata,socket,struct

def clean(value,limit=80):return ''.join(c for c in str(value) if unicodedata.category(c) not in ('Cc','Cf','Cs'))[:limit]
def text_read(path):return pathlib.Path(path).read_text().strip()
def percent(used,total):return round(max(0,min(100,100*used/total)),1) if total>0 else None
def cpu_percent(current,previous):
 if previous is None:return None
 dt=current[0]-previous[0];di=current[1]-previous[1]
 return percent(dt-di,dt) if dt>0 and di>=0 else None

def cpu_ticks():
 v=list(map(int,text_read('/proc/stat').splitlines()[0].split()[1:9]));return sum(v),sum(v[3:5])

def resource_metrics(previous=None):
 ticks=cpu_ticks();mem={k:int(v.split()[0]) for k,v in (line.split(':',1) for line in text_read('/proc/meminfo').splitlines() if ':' in line)};total=mem.get('MemTotal',0);available=mem.get('MemAvailable')
 cpu=cpu_percent(ticks,previous);ram=percent(total-available,total) if available is not None else None;disks={}
 from console_storage_metrics import disk_info
 disks={'main':disk_info()}
 battery=temperature=None
 try:battery=int(text_read('/sys/class/power_supply/battery/capacity'));battery=battery if 0<=battery<=100 else None
 except (OSError,ValueError):pass
 try:temperature=int(text_read('/sys/class/power_supply/battery/temp'))/10
 except (OSError,ValueError):pass
 return ticks,{'cpu_percent':cpu,'memory_percent':ram,'disks':disks,'disk_basis':'physical-pool-once-including-internal-system-images'},{'battery_percent':battery,'temperature_c':temperature}

def command(args,timeout=2):
 try:
  p=subprocess.run(args,capture_output=True,text=True,timeout=timeout);return p.stdout.strip() if p.returncode==0 else ''
 except (OSError,subprocess.TimeoutExpired):return ''

def checksum(data):
    if len(data)%2:data+=b'\0'
    total=sum(struct.unpack('!%dH'%(len(data)//2),data));total=(total&65535)+(total>>16);total=(total&65535)+(total>>16);return (~total)&65535

def gateway_ping(address,timeout=1):
    # One small ICMP echo to the configured gateway; no shell, DNS or external host.
    ipaddress.IPv4Address(address);sock=None
    try:
        sock=socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP);sock.setsockopt(socket.SOL_SOCKET,25,b'wlan0\0');sock.settimeout(timeout)
        identifier=int.from_bytes(os.urandom(2),'big');sequence=1;payload=b'DLT-HOME'+os.urandom(8);header=struct.pack('!BBHHH',8,0,0,identifier,sequence);packet=struct.pack('!BBHHH',8,0,checksum(header+payload),identifier,sequence)+payload
        start=time.monotonic();sock.sendto(packet,(address,0));deadline=start+timeout
        while time.monotonic()<deadline:
            sock.settimeout(max(.001,deadline-time.monotonic()));data,peer=sock.recvfrom(2048)
            if peer[0]!=address or len(data)<28:continue
            offset=(data[0]&15)*4
            if offset<20 or len(data)<offset+8+len(payload):continue
            typ,code,check,ident,seq=struct.unpack_from('!BBHHH',data,offset)
            if typ==0 and code==0 and ident==identifier and seq==sequence and data[offset+8:offset+8+len(payload)]==payload:return round((time.monotonic()-start)*1000,2)
    except (OSError,ValueError):return None
    finally:
        if sock:sock.close()
    return None

def wifi_metrics(old=None):
 from console_settings_backend import WPA,fields,decode_ssid
 old=old or {};now=time.monotonic()
 try:
  with WPA() as w:
   w.sock.settimeout(.7);state=fields(w.cmd('STATUS'));connected=state.get('wpa_state')=='COMPLETED';ssid=decode_ssid(state.get('ssid',''));signal={}
   if connected:
    try:signal=fields(w.cmd('SIGNAL_POLL'))
    except (OSError,RuntimeError):pass
 except (OSError,RuntimeError):return {'connected':False,'ssid':'','signal_dbm':None,'gateway_latency_ms':None,'state':'状态待检查','sample_monotonic':now}
 try:rssi=int(signal['RSSI']);rssi=rssi if -127<=rssi<=0 else None
 except (KeyError,ValueError):rssi=None
 result={'connected':connected,'ssid':clean(ssid),'signal_dbm':rssi,'gateway_latency_ms':None,'gateway_latency_less_than':False,'gateway':None,'latency_scope':'current default gateway via wlan0, not Internet latency','sample_monotonic':now}
 if not connected:return result
 try:
  routes=json.loads(command(['/sbin/ip','-j','route','show','default','dev','wlan0'],1) or '[]');gateway=min((r for r in routes if r.get('gateway')),key=lambda r:r.get('metric',0))['gateway'];ipaddress.IPv4Address(gateway)
 except (ValueError,KeyError,TypeError):return result
 result['gateway']=gateway
 if old.get('gateway')==gateway and old.get('ssid')==result['ssid'] and now-old.get('latency_checked_monotonic',-100)<10:
  for key in ('gateway_latency_ms','gateway_latency_less_than','latency_checked_monotonic'):result[key]=old.get(key)
 else:
  result['gateway_latency_ms']=gateway_ping(gateway);result['latency_checked_monotonic']=now
 return result

def bluetooth_metrics():
 if not list(pathlib.Path('/sys/class/bluetooth').glob('hci*')):return {'ready':False,'connected_names':[],'state':'控制器未就绪'}
 # BlueZ ObjectManager snapshot, not discovery or one info-process per device.
 output=command(['/usr/bin/busctl','--system','--json=short','call','org.bluez','/','org.freedesktop.DBus.ObjectManager','GetManagedObjects'],1.5)
 try:
  obj=json.loads(output);data=obj['data'][0];names=[]
  for interfaces in data.values():
   device=interfaces.get('org.bluez.Device1')
   if not device:continue
   def val(key):
    v=device.get(key,{})
    return v.get('data') if isinstance(v,dict) else None
   if val('Connected') is True:names.append(clean(val('Alias') or val('Name') or val('Address') or '蓝牙设备'))
  return {'ready':True,'connected_names':names[:8],'state':'已连接' if names else '未连接'}
 except (ValueError,KeyError,TypeError,AttributeError):return {'ready':True,'connected_names':[],'state':'状态待检查'}

def pct(v):return '—' if v is None else '%g%%'%v

def make_tiles(server,phone,wifi,bt):
 s1='CPU '+pct(server.get('cpu_percent'))+' · 内存 '+pct(server.get('memory_percent'));disks=server.get('disks',{});s2='硬盘 '+pct(disks.get('main',{}).get('percent'))
 battery=phone.get('battery_percent');temp=phone.get('temperature_c');p1='电量 '+pct(battery);p2='温度 '+('—' if temp is None else '%g℃'%temp)
 if wifi.get('connected'):
  w1=clean(wifi.get('ssid') or '隐藏网络');lat=wifi.get('gateway_latency_ms');rssi=wifi.get('signal_dbm');w2='延迟 '+(('—' if lat is None else ('<' if wifi.get('gateway_latency_less_than') else '')+'%g ms'%lat))+' · '+('—' if rssi is None else str(rssi)+' dBm')
 else:w1=wifi.get('state','未连接');w2=''
 names=bt.get('connected_names',[]);b1=names[0] if names else bt.get('state','状态待检查');b2=('另有 '+str(len(names)-1)+' 个已连接') if len(names)>1 else ''
 return {k:{'label':lines[0],'lines':lines} for k,lines in [('server',[s1,s2]),('phone',[p1,p2]),('wifi',[w1,w2]),('bluetooth',[b1,b2])]}

class Sampler:
 def __init__(self):
  self.lock=threading.Lock();self.started=False;self.server={};self.phone={};self.wifi={};self.bt={};self.times={};self.cpu=None
 def resource_loop(self):
  while True:
   try:
    ticks,s,p=resource_metrics(self.cpu);self.cpu=ticks
    with self.lock:self.server=s;self.phone=p;self.times['resources']=time.monotonic()
   except (OSError,ValueError,TypeError):pass
   time.sleep(2)
 def wifi_loop(self):
  while True:
   try:
    w=wifi_metrics(self.wifi)
    with self.lock:self.wifi=w;self.times['wifi']=time.monotonic()
   except Exception:pass
   time.sleep(4)
 def bt_loop(self):
  while True:
   try:
    b=bluetooth_metrics()
    with self.lock:self.bt=b;self.times['bluetooth']=time.monotonic()
   except Exception:pass
   time.sleep(6)
 def snapshot(self):
  with self.lock:
   if not self.started:
    self.started=True
    for fn in (self.resource_loop,self.wifi_loop,self.bt_loop):threading.Thread(target=fn,daemon=True).start()
   now=time.monotonic();s=self.server if now-self.times.get('resources',-999)<12 else {};p=self.phone if now-self.times.get('resources',-999)<12 else {};w=self.wifi if now-self.times.get('wifi',-999)<15 else {'state':'状态待检查'};b=self.bt if now-self.times.get('bluetooth',-999)<20 else {'state':'状态待检查'}
   return make_tiles(s,p,w,b)
SAMPLER=Sampler()
def snapshot():return SAMPLER.snapshot()
