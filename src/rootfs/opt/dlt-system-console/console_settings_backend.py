"""Local console settings backend; existing WPA owner stays running.

No shell interpolation, board reset, modem reset, credential logging or ports.
"""
import pathlib,os,socket,uuid,time,re,hashlib,json,subprocess,shutil

def decode_ssid(text):
    out=bytearray();i=0
    while i<len(text):
        if text[i:i+2]=='\\x' and i+4<=len(text):
            try:out.append(int(text[i+2:i+4],16));i+=4;continue
            except ValueError:pass
        if text[i:i+2]=='\\\\':out.append(92);i+=2;continue
        out.extend(text[i].encode('utf-8'));i+=1
    return out.decode('utf-8',errors='replace')

class WPA:
    def __init__(self):
        root=pathlib.Path('/run/dlt-system-console');root.mkdir(exist_ok=True,mode=0o700)
        self.local=str(root/('wpa-'+uuid.uuid4().hex+'.sock'))
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
        self.sock.settimeout(3);self.sock.bind(self.local)
        os.chmod(self.local,0o600);self.sock.connect('/run/wpa_supplicant/wlan0')
    def cmd(self,text):
        if '\n' in text or len(text)>4096:raise ValueError('invalid WPA request')
        self.sock.send(text.encode('ascii'))
        value=self.sock.recv(65536).decode('utf-8',errors='replace').strip()
        if value.startswith('FAIL') or value=='UNKNOWN COMMAND':raise RuntimeError('Wi-Fi 控制请求失败')
        return value
    def close(self):
        self.sock.close()
        try:os.unlink(self.local)
        except FileNotFoundError:pass
    def __enter__(self):return self
    def __exit__(self,*args):self.close()

def fields(text):return dict(x.split('=',1) for x in text.splitlines() if '=' in x)
def parse_scan(text,current=''):
    found={}
    for row in text.splitlines()[1:]:
        parts=row.split('\t',4)
        if len(parts)!=5:continue
        mac,freq,signal,flags,escaped=parts
        ssid=decode_ssid(escaped)
        if not ssid:continue
        try:signal=int(signal);freq=int(freq)
        except ValueError:continue
        item={'ssid':ssid,'signal':signal,'frequency':freq,'security':flags,
              'connected':ssid==current,'open':'WPA' not in flags and 'WEP' not in flags}
        if ssid not in found or signal>found[ssid]['signal']:found[ssid]=item
    return sorted(found.values(),key=lambda x:(not x['connected'],-x['signal']))

def wifi_info(scan=False):
    completed=not scan
    with WPA() as w:
        if scan:
            # Does not disconnect/select/reconfigure the working network.
            with WPA() as monitor:
                monitor.cmd('ATTACH');monitor.sock.settimeout(.5)
                try:w.cmd('SCAN')
                except RuntimeError:pass  # An existing scan may already be in progress.
                deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    try:event=monitor.sock.recv(8192).decode(errors='replace')
                    except socket.timeout:continue
                    if 'CTRL-EVENT-SCAN-RESULTS' in event:completed=True;break
                    if 'CTRL-EVENT-SCAN-FAILED' in event:break
        s=fields(w.cmd('STATUS'));ssid=decode_ssid(s.get('ssid',''))
        nets=parse_scan(w.cmd('SCAN_RESULTS'),ssid)
    try:
        _,config=saved_wifi();saved=[n['ssid'] for n in config['networks']]
    except Exception:saved=[]
    for n in nets:n['saved']=n['ssid'] in saved
    try:
        auto=subprocess.run(['systemctl','is-active','dlt-connection-auto.service'],capture_output=True,text=True,timeout=2).returncode==0
        if auto:
            report_path=pathlib.Path('/run/dlt-connection-auto-status.json');st=report_path.stat()
            if report_path.is_symlink() or st.st_uid!=0 or st.st_mode&0o077 or st.st_size>8192:raise ValueError('unsafe connection status')
            report=json.loads(report_path.read_text())
            auto=(report.get('wifi_enabled') is True and report.get('preserve_connected') is True and
                  report.get('boot_id')==pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip() and
                  0<=time.monotonic()-report.get('updated_monotonic',-999)<=40)
    except (OSError,subprocess.TimeoutExpired,ValueError,TypeError,AttributeError):auto=False
    return {'connected':s.get('wpa_state')=='COMPLETED','ssid':ssid,
            'ip':s.get('ip_address',''),'state':s.get('wpa_state','UNKNOWN'),
            'networks':nets,'saved_ssids':saved,'scan_time':time.time(),'scan_completed':completed,'auto_connect_enabled':auto}

def saved_wifi():
    import sys
    sys.path.insert(0,'/usr/local/lib/dlt-modules')
    from dlt_module_state import Store
    from console_boot_context import inspect_context
    store=Store(inspect_context()['wifi_profile_root'],'wifi')
    with store.lock():
        s=store.state();g=s.get('current') or s.get('previous')
        config=store.load(g) if g else {'schema':1,'networks':[]}
    return store,config

def wifi_connect(ssid,password,only_if_disconnected=False):
    # Both the local console and background connector use this inter-process
    # gate. Never queue a second selector behind an in-flight association.
    if os.name!='posix':return _wifi_connect(ssid,password,only_if_disconnected)
    import fcntl,stat
    root=pathlib.Path('/run/dlt-system-console');root.mkdir(mode=0o700,exist_ok=True)
    fd=os.open(root/'wifi-operation.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        st=os.fstat(fd)
        if st.st_uid!=0 or st.st_mode&0o077 or not stat.S_ISREG(st.st_mode):raise RuntimeError('网络操作锁校验失败')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('已有网络连接任务，请稍后重试')
        return _wifi_connect(ssid,password,only_if_disconnected)
    finally:os.close(fd)

def _wifi_connect(ssid,password,only_if_disconnected=False):
    if not isinstance(ssid,str) or not 1<=len(ssid.encode('utf-8'))<=32 or any(ord(c)<32 for c in ssid):
        raise ValueError('Wi-Fi 名称无效')
    store,config=saved_wifi()
    if not password:
        old=next((n for n in config['networks'] if n['ssid']==ssid),None)
        if not old:raise ValueError('请输入密码')
        password=old['psk']
    if not isinstance(password,str) or not (re.fullmatch('[0-9a-fA-F]{64}',password) or (8<=len(password)<=63 and all(32<=ord(c)<=126 for c in password))):
        raise ValueError('密码需要 8–63 位字符')
    key=password.lower() if re.fullmatch('[0-9a-fA-F]{64}',password) else hashlib.pbkdf2_hmac('sha1',password.encode(),ssid.encode(),4096,32).hex()
    candidate=None;network=None;oldid=None;token=None
    with WPA() as w:
        before=fields(w.cmd('STATUS'));oldid=before.get('id')
        if before.get('wpa_state')=='COMPLETED' and decode_ssid(before.get('ssid',''))==ssid:
            return {'ssid':ssid,'connected':True,'unchanged':True,'ip':before.get('ip_address','')}
        if only_if_disconnected and before.get('wpa_state')=='COMPLETED':
            return {'ssid':decode_ssid(before.get('ssid','')),'connected':True,
                    'preserved_existing':True,'ip':before.get('ip_address','')}
        if only_if_disconnected and before.get('wpa_state') not in ('DISCONNECTED','INACTIVE','SCANNING'):
            return {'ssid':decode_ssid(before.get('ssid','')),'connected':False,'association_in_progress':True,'preserved_existing':True}
        try:
            network=w.cmd('ADD_NETWORK')
            if not network.isdigit():raise RuntimeError('创建连接失败')
            for field,value in [('ssid',ssid.encode().hex()),('psk',key),('key_mgmt','WPA-PSK')]:
                if w.cmd('SET_NETWORK '+network+' '+field+' '+value)!='OK':raise RuntimeError('配置连接失败')
            if only_if_disconnected:
                latest=fields(w.cmd('STATUS'))
                if latest.get('wpa_state') not in ('DISCONNECTED','INACTIVE','SCANNING'):
                    w.cmd('REMOVE_NETWORK '+network)
                    return {'ssid':decode_ssid(latest.get('ssid','')),'connected':latest.get('wpa_state')=='COMPLETED',
                            'association_in_progress':latest.get('wpa_state')!='COMPLETED','preserved_existing':True,'ip':latest.get('ip_address','')}
            from console_wifi_revert import arm,finish
            token=arm(oldid,network,only_if_disconnected)
            if only_if_disconnected:
                latest=fields(w.cmd('STATUS'))
                if latest.get('wpa_state') not in ('DISCONNECTED','INACTIVE','SCANNING'):
                    w.cmd('REMOVE_NETWORK '+network);finish(token,'cancelled-preserve-existing');token=None
                    return {'ssid':decode_ssid(latest.get('ssid','')),'connected':latest.get('wpa_state')=='COMPLETED',
                            'association_in_progress':latest.get('wpa_state')!='COMPLETED','preserved_existing':True,'ip':latest.get('ip_address','')}
            w.cmd('SELECT_NETWORK '+network)
            deadline=time.monotonic()+30;connected=False;after={}
            while time.monotonic()<deadline:
                after=fields(w.cmd('STATUS'))
                if after.get('wpa_state')=='COMPLETED' and decode_ssid(after.get('ssid',''))==ssid:
                    connected=True;break
                time.sleep(.6)
            if not connected:raise RuntimeError('连接失败，已恢复原网络')
            # Commit only after the actual selected network has associated.
            new={'schema':1,'networks':[{'ssid':ssid,'psk':password}]+[n for n in config['networks'] if n['ssid']!=ssid][:7]}
            candidate=store.stage(new);selected=store.prepare()
            if selected['generation']!=candidate:raise RuntimeError('保存配置失败')
            store.accept(candidate);candidate=None
            from dlt_module_state import atomic,wpa_config
            atomic(pathlib.Path('/run/dlt-modules/wifi/wpa.conf'),wpa_config(new))
            finish(token,'committed');token=None
            return {'ssid':ssid,'connected':True,'ip':after.get('ip_address',''),'persisted':True}
        except Exception:
            keep_other=False
            if only_if_disconnected:
                try:
                    current=fields(w.cmd('STATUS'));keep_other=current.get('wpa_state')=='COMPLETED' and current.get('id')!=network
                except Exception:pass
            if not keep_other and oldid and oldid.isdigit():
                try:w.cmd('SELECT_NETWORK '+oldid)
                except Exception:pass
            if network and network.isdigit():
                try:w.cmd('REMOVE_NETWORK '+network)
                except Exception:pass
            if candidate:
                try:store.reject(candidate)
                except Exception:pass
            if token:
                try:finish(token,'rolled-back')
                except Exception:pass
            raise

def bt_cmd(commands,timeout=7):
    tool=shutil.which('bluetoothctl')
    if not tool:raise RuntimeError('蓝牙服务尚未安装')
    p=subprocess.run([tool],input='\n'.join(commands+['quit'])+'\n',capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError('蓝牙管理命令失败')
    return re.sub(r'\x1b\[[0-9;]*[A-Za-z]','',p.stdout)

def bluetooth_info(scan=False):
    adapters=list(pathlib.Path('/sys/class/bluetooth').glob('hci*'))
    if not adapters:
        return {'ready':False,'powered':False,'devices':[],
                'message':'控制器未就绪','detail':'板载蓝牙的 HCI 驱动链尚未接通',
                'diagnostics':{'uart':pathlib.Path('/dev/ttyHS0').exists(),'hci_count':0,'bluez_tools':bool(shutil.which('bluetoothctl'))},'reason_code':'no-hci'}
    if not shutil.which('bluetoothctl'):
        return {'ready':False,'powered':False,'devices':[],'message':'蓝牙管理服务未部署','detail':'已发现 HCI，尚缺 BlueZ 管理工具','reason_code':'no-bluez'}
    text=bt_cmd(['show','devices'])
    if scan:
        # Bounded discovery process; never change the Wi-Fi firmware stack.
        tool=shutil.which('bluetoothctl')
        subprocess.run([tool,'--timeout','5','scan','on'],capture_output=True,text=True,timeout=7)
        text=bt_cmd(['show','devices'])
    devices=[]
    for line in text.splitlines():
        m=re.search(r'\bDevice ([0-9A-Fa-f:]{17}) (.+)',line)
        if m and len(devices)<12:
            address=m[1].upper()
            if any(v['address']==address for v in devices):continue
            detail=bt_cmd(['info '+address],timeout=3)
            devices.append({'address':address,'name':m[2].strip(),'paired':'Paired: yes' in detail,'trusted':'Trusted: yes' in detail,'connected':'Connected: yes' in detail})
    devices.sort(key=lambda v:(not v['connected'],not v['paired'],v['name']))
    return {'ready':True,'powered':'Powered: yes' in text,'devices':devices,'message':'','connected_count':sum(v['connected'] for v in devices)}

def bluetooth_connect(address):
    if not re.fullmatch('[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}',address):raise ValueError('设备地址无效')
    if not list(pathlib.Path('/sys/class/bluetooth').glob('hci*')):raise RuntimeError('控制器未就绪')
    detail=bt_cmd(['info '+address])
    if 'Paired: yes' not in detail:raise RuntimeError('新设备交互配对代理尚未接入；不会自动确认配对请求')
    result=bt_cmd(['connect '+address],timeout=25)
    if 'Connection successful' not in result:raise RuntimeError('配对或连接未完成')
    return {'address':address,'connected':True}

def phone_info():
    from console_status_backend import phone_info as collect
    return collect()

def server_info():
    from console_status_backend import server_info as collect
    return collect()

def service_info(name):
    from console_status_backend import service_info as collect
    return collect(name)

def recovery_info():
    from console_recovery import info
    return info()

def recovery_prepare(action):
    from console_recovery import prepare
    return prepare(action)

def recovery_execute(token,origin):
    from console_recovery import execute
    return execute(token,origin)

def bluetooth_action(action,address=None,origin='remote'):
    if origin!='physical-phone':raise PermissionError('请在手机实体屏幕确认蓝牙设置')
    if action not in ('power-on','power-off','connect','disconnect','trust-on','trust-off','forget'):raise ValueError('无效蓝牙操作')
    if not list(pathlib.Path('/sys/class/bluetooth').glob('hci*')):raise RuntimeError('板载蓝牙控制器尚未接通')
    if action.startswith('power-'):
        output=bt_cmd(['power '+('on' if action=='power-on' else 'off')])
        if 'Failed' in output or 'not available' in output:raise RuntimeError('蓝牙电源状态未能改变')
        status=bt_cmd(['show'])
        if ('Powered: yes' in status)!=(action=='power-on'):raise RuntimeError('蓝牙电源状态尚未确认')
        return {'action':action,'completed':True}
    if not isinstance(address,str) or not re.fullmatch('[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}',address):raise ValueError('设备地址无效')
    if action=='connect':return bluetooth_connect(address)
    commands={'disconnect':'disconnect','trust-on':'trust','trust-off':'untrust','forget':'remove'}
    output=bt_cmd([commands[action]+' '+address])
    if 'Failed' in output or 'not available' in output:raise RuntimeError('蓝牙操作未完成，请重新读取设备状态')
    return {'action':action,'completed':True}
def admin_info():
    from device_access import Store
    return Store().view()

def system_power(action,origin):
    from console_power_key import schedule
    return schedule(action,origin)

def admin_decide(request_id,approve,origin):
    from device_access import Store
    return Store().decide(request_id,approve,origin)

def admin_revoke(device_id,origin):
    from device_access import Store
    return Store().revoke(device_id,origin)



def capture_info():
    from console_capture import info
    return info()

def capture_switch(enabled,origin):
    from console_capture import switch
    return switch(enabled,origin)

def ssh_exposure_switch(enabled,origin):
    from console_ssh_exposure import switch
    switch(enabled,origin)
    return server_info()


def fixed_network_info():
    from console_fixed_network import info
    return info()
def fixed_network_action(action,origin,name=None):
    from console_fixed_network import action as dispatch
    return dispatch(action,origin,name)
