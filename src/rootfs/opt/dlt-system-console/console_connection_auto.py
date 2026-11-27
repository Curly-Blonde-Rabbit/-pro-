"""Auto-connect saved peers only when that transport has no active connection.

Runs independently of the display, never disconnects a working peer, never
changes firmware, and never pairs an unknown Bluetooth device automatically.
"""
import pathlib,json,os,time,sys,signal,subprocess
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import console_settings_backend as backend

POLICY=pathlib.Path('/srv/dlt-state/connection-policy.json')
STATE=pathlib.Path('/run/dlt-connection-auto-status.json')
DEFAULT={'schema':1,'wifi_autoconnect':True,'bluetooth_autoconnect':True,'preserve_connected':True}

def write(path,value):
    from device_access import atomic
    atomic(path,json.dumps(value,ensure_ascii=False,separators=(',',':')).encode())
def policy():
    if POLICY.is_symlink() or POLICY.stat().st_uid!=0 or POLICY.stat().st_mode&0o077 or POLICY.stat().st_size>4096:raise ValueError('unsafe connection policy')
    v=json.loads(POLICY.read_text())
    if not isinstance(v,dict) or set(v)!=set(DEFAULT) or type(v.get('schema')) is not int or v['schema']!=1 or v.get('preserve_connected') is not True or any(type(v[k]) is not bool for k in ['wifi_autoconnect','bluetooth_autoconnect','preserve_connected']):
        raise ValueError('invalid connection policy; auto-connect isolated')
    return v

class IO:
    def __init__(self):self.activity={'wifi_status_queries':0,'wifi_scans':0,'wifi_connect_requests':0,'bluetooth_discoveries':0,'bluetooth_connect_requests':0}
    def wifi_status(self):
        self.activity['wifi_status_queries']+=1
        with backend.WPA() as w:return backend.fields(w.cmd('STATUS'))
    def manual_transition(self):
        from console_recovery import active_operation
        recovery=active_operation()
        if recovery and recovery.get('action')=='wifi-restart':return True
        p=pathlib.Path('/run/dlt-system-console/wifi-transition.json')
        return p.exists() and json.loads(p.read_text()).get('state')=='pending'
    def saved_wifi(self):
        _,config=backend.saved_wifi();return [n['ssid'] for n in config['networks']]
    def scan_wifi(self):self.activity['wifi_scans']+=1;return backend.wifi_info(True)['networks']
    def connect_wifi(self,ssid):self.activity['wifi_connect_requests']+=1;return backend.wifi_connect(ssid,'',only_if_disconnected=True)
    def bt_snapshot(self):
        if not list(pathlib.Path('/sys/class/bluetooth').glob('hci*')):
            return {'ready':False,'connected':[],'paired':[]}
        text=backend.bt_cmd(['devices']);addresses=[]
        import re
        for line in text.splitlines():
            m=re.search(r'\bDevice ([0-9A-Fa-f:]{17}) ',line)
            if m and m[1].upper() not in addresses:addresses.append(m[1].upper())
        connected=[];paired=[]
        for address in addresses[:24]:
            info=backend.bt_cmd(['info '+address])
            if 'Connected: yes' in info:connected.append(address)
            if 'Paired: yes' in info:paired.append(address)
        return {'ready':True,'connected':connected,'paired':paired}
    def detect_bt(self):self.activity['bluetooth_discoveries']+=1;return {d['address'] for d in backend.bluetooth_info(True)['devices']}
    def connect_bt(self,address):
        self.activity['bluetooth_connect_requests']+=1
        text=backend.bt_cmd(['connect '+address],timeout=15)
        return 'Connection successful' in text

def wifi_step(io):
    # This branch issues ONLY STATUS. In particular no SCAN/SELECT/ENABLE.
    status=io.wifi_status()
    if status.get('wpa_state')=='COMPLETED':return {'state':'keeping-current','ssid':backend.decode_ssid(status.get('ssid',''))}
    if status.get('wpa_state')=='INTERFACE_DISABLED':return {'state':'interface-disabled'}
    if status.get('wpa_state') not in ('DISCONNECTED','INACTIVE','SCANNING'):
        return {'state':'association-in-progress'}
    if io.manual_transition():return {'state':'manual-transition-in-progress'}
    saved=io.saved_wifi()
    if not saved:return {'state':'no-saved-networks'}
    found=io.scan_wifi()
    # A working connection may appear during scan: do not replace it.
    latest=io.wifi_status().get('wpa_state')
    if latest=='COMPLETED':return {'state':'keeping-newly-connected'}
    if latest not in ('DISCONNECTED','INACTIVE','SCANNING') or io.manual_transition():return {'state':'association-in-progress'}
    target=next((n['ssid'] for n in found if n['ssid'] in saved),None)
    if not target:return {'state':'saved-networks-not-detected'}
    result=io.connect_wifi(target)
    if result.get('association_in_progress'):return {'state':'association-in-progress'}
    return {'state':'keeping-current' if result.get('preserved_existing') else 'saved-network-connected','ssid':result.get('ssid',target)}

def bluetooth_step(io):
    status=io.bt_snapshot()
    if not status['ready']:return {'state':'waiting-for-controller'}
    if status['connected']:return {'state':'keeping-current','devices':status['connected']}
    if not status['paired']:return {'state':'no-paired-devices'}
    detected=io.detect_bt()
    latest=io.bt_snapshot()
    if latest['connected']:return {'state':'keeping-newly-connected','devices':latest['connected']}
    target=next((a for a in status['paired'] if a in detected),None)
    if not target:return {'state':'paired-devices-not-detected'}
    # One final check immediately before issuing Connect. Never disconnect first.
    if io.bt_snapshot()['connected']:return {'state':'keeping-newly-connected'}
    return {'state':'paired-device-connected' if io.connect_bt(target) else 'paired-device-connect-failed','device':target}

def main():
    stopped=False
    def stop(*_):
        nonlocal stopped
        stopped=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    io=IO();last_bt=0;report={'wifi':{},'bluetooth':{}}
    while not stopped:
        try:
            p=policy()
            if p['wifi_autoconnect']:
                try:report['wifi']=wifi_step(io)
                except Exception:report['wifi']={'state':'retry-later'}
            else:report['wifi']={'state':'disabled'}
            if p['bluetooth_autoconnect'] and time.monotonic()-last_bt>=30:
                last_bt=time.monotonic()
                try:report['bluetooth']=bluetooth_step(io)
                except Exception:report['bluetooth']={'state':'retry-later'}
            elif not p['bluetooth_autoconnect']:report['bluetooth']={'state':'disabled'}
            report.update(updated=time.time(),updated_monotonic=time.monotonic(),boot_id=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),wifi_enabled=p['wifi_autoconnect'],bluetooth_enabled=p['bluetooth_autoconnect'],preserve_connected=True,system_reboot_requested=False,activity=dict(io.activity))
            write(STATE,report)
        except Exception:write(STATE,{'state':'invalid-policy-isolated','system_reboot_requested':False})
        for _ in range(40):
            if stopped:break
            time.sleep(.2)
if __name__=='__main__':main()
