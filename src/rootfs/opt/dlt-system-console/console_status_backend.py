"""Bounded read-only server/handset telemetry; no daemon or firmware mutation."""
import json,os,pathlib,shutil,socket,subprocess,time,re,unicodedata,datetime
_cpu=None
SERVICES=('ssh.service','dlt-wifi-managed.service','dlt-system-console.service','docker.service','containerd.service','1panel.service','1panel-core.service','1panel-agent.service')
def clean_log(value,limit=180):
    value=''.join(c if unicodedata.category(c) not in ('Cc','Cf','Cs') else ' ' for c in str(value))
    value=re.sub(r'(?i)\b(password|passwd|psk|token|secret|authorization|cookie|api[_-]?key)\s*[:=]\s*(?:"[^"]*"|\S+)',r'\1=[hidden]',value)
    return value[:limit]
def read(path,default='—'):
    try:return pathlib.Path(path).read_text().strip()
    except OSError:return default
def command(argv,timeout=3):
    try:
        p=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
        return p.stdout.strip() if p.returncode==0 else ''
    except (OSError,subprocess.TimeoutExpired):return ''
def services():
    text=command(['systemctl','show',*SERVICES,'-p','Id','-p','LoadState','-p','ActiveState','-p','SubState'])
    result=[]
    for block in text.split('\n\n'):
        v=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
        if v.get('Id'):result.append({'name':v['Id'],'installed':v.get('LoadState')!='not-found','state':v.get('ActiveState','unknown'),'substate':v.get('SubState','')})
    return result
def service_info(name):
    if name not in SERVICES:raise ValueError('服务不在只读查询列表中')
    properties=('Id','Description','LoadState','ActiveState','SubState','UnitFileState','MainPID','MemoryCurrent','NRestarts','Result','ExecMainStatus')
    argv=['systemctl','show',name]
    for prop in properties:argv+=['-p',prop]
    text=command(argv,timeout=4)
    if not text:raise RuntimeError('服务状态读取失败，请重试')
    status=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
    installed=status.get('LoadState')!='not-found'
    logs=[]
    if installed:
        raw=command(['journalctl','-b','--no-pager','--output=json','-n','20','-u',name],timeout=4)
        for line in raw.splitlines()[-20:]:
            try:
                entry=json.loads(line)
                if not isinstance(entry,dict):continue
                message=entry.get('MESSAGE','')
                if isinstance(message,list):message=bytes(message[:512]).decode('utf-8',errors='replace')
                stamp=int(entry.get('__REALTIME_TIMESTAMP','0'))/1e6
                display_time=datetime.datetime.fromtimestamp(stamp,datetime.timezone(datetime.timedelta(hours=8))).strftime('%H:%M:%S') if stamp else '—'
                logs.append({'time':display_time,'message':clean_log(message),'priority':str(entry.get('PRIORITY','6'))})
            except (ValueError,TypeError,OverflowError):continue
    return {'name':name,'installed':installed,'properties':{k:clean_log(v,140) for k,v in status.items()},'logs':logs,'read_only':True,
            'log_scope':'current-boot-last-20','log_timezone':'UTC+08:00','log_message_limit':180,'sample_time':time.time()}
def server_info():
    global _cpu
    from console_ssh_exposure import info as ssh_info
    now=time.monotonic();values=[int(x) for x in read('/proc/stat','cpu 0 0 0 0').splitlines()[0].split()[1:9]]
    total=sum(values);idle=sum(values[3:5]);percent=None
    if _cpu and total>_cpu[0]:percent=round(100*(1-(idle-_cpu[1])/(total-_cpu[0])),1)
    _cpu=(total,idle)
    mem=dict(line.split(':',1) for line in read('/proc/meminfo','MemTotal: 0 kB\nMemAvailable: 0 kB').splitlines() if ':' in line)
    kib=lambda k:int(mem.get(k,'0 kB').split()[0])
    from console_storage_metrics import disk_info
    disks=[disk_info()]
    ip=command(['/sbin/ip','-j','address','show']);addresses={}
    try:
        for item in json.loads(ip):addresses[item['ifname']]=[a['local'] for a in item.get('addr_info',[]) if a.get('family')=='inet']
    except (ValueError,KeyError):pass
    interfaces=[]
    for p in sorted(pathlib.Path('/sys/class/net').iterdir()):
        if p.name=='lo' or not (p/'ifindex').exists():continue
        interfaces.append({'name':p.name,'state':read(p/'operstate'),'ipv4':addresses.get(p.name,[]),'rx_mib':round(int(read(p/'statistics/rx_bytes','0'))/2**20,2),'tx_mib':round(int(read(p/'statistics/tx_bytes','0'))/2**20,2)})
    interfaces.sort(key=lambda v:(v['state']!='up',v['name']))
    return {'hostname':socket.gethostname(),'uptime_seconds':int(float(read('/proc/uptime','0').split()[0])),'cpu_percent':percent,'cpu_cores':os.cpu_count(),
            'load':read('/proc/loadavg').split()[:3],'total_mib':round(kib('MemTotal')/1024),'used_mib':round((kib('MemTotal')-kib('MemAvailable'))/1024),
            'available_mib':round(kib('MemAvailable')/1024),'swap_total_mib':round(kib('SwapTotal')/1024),'swap_used_mib':round((kib('SwapTotal')-kib('SwapFree'))/1024),
            'ssh_exposure':ssh_info(),'disks':disks,'interfaces':interfaces,'services':services(),'sample_monotonic':now}
def phone_info():
    b=pathlib.Path('/sys/class/power_supply/battery')
    supplies=[]
    for name in ('usb','pc_port','dc','wireless'):
        p=pathlib.Path('/sys/class/power_supply',name)
        if p.exists():supplies.append({'name':name,'online':read(p/'online'),'type':read(p/'type'),'usb_type':read(p/'usb_type')})
    raw=read('/sys/kernel/dlt_iris_trial/status','')
    panel=dict(x.split('=',1) for x in raw.split() if '=' in x)
    inputs=[]
    for p in pathlib.Path('/sys/class/input').glob('event*/device/name'):
        name=read(p)
        if name=='dlt_synaptics_dsx':inputs.append({'name':name,'event':p.parent.parent.name,'physical_path':str(p.parent.resolve())})
    temp=read(b/'temp');voltage=read(b/'voltage_now');current=read(b/'current_now')
    def number(value,scale):
        try:return round(int(value)/scale,2)
        except ValueError:return None
    return {'battery':read(b/'capacity'),'temp':temp,'temperature_c':number(temp,10),'charge':read(b/'status'),'health':read(b/'health'),
            'voltage_v':number(voltage,1e6),'current_ma':number(current,1000),'technology':read(b/'technology'),'supplies':supplies,
            'panel':panel,'touch_devices':inputs,'kernel':os.uname().release,'machine':os.uname().machine,'usable_rect':[24,24,1032,600],
            'display_control':'fixed-validated-brightness64','bluetooth_hci_count':len(list(pathlib.Path('/sys/class/bluetooth').glob('hci*'))),'usb_roles':[{'name':p.parent.name,'role':read(p)} for p in pathlib.Path('/sys/class/usb_role').glob('*/role')]}
