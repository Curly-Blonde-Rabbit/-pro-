"""Persistent local-only scanout service. Does not reboot or configure networks."""
import pathlib,os,sys,json,time,subprocess,signal,fcntl,socket,hashlib,traceback
ROOT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'vendor'))
import dlt_console_scanout as scanout
from system_console_renderer import SystemConsole
import console_settings_backend as settings_backend
from console_settings_model import Settings
from console_settings_view import View
from console_ui_control import Channel
from console_power_key import Reader as PowerKeyReader

RUN=pathlib.Path('/run/dlt-system-console')
STATUS=pathlib.Path('/sys/kernel/dlt_iris_trial/status')
CONTROL=pathlib.Path('/sys/kernel/dlt_iris_trial/control')
OWNED=[]
from console_display_lifecycle import DisplayLifecycle
ESD_LEASE=None
PAGE='home'
SNAPSHOT={}
last_tick=0
last_poll=0
poll_failures=0
last_frame_state=None
ready=False
APP=Settings(settings_backend)
UI_CHANNEL=None
SETTINGS_VIEW=None
BOOT_CONTEXT={}
POWER_KEY=None

def emit(**v):print(json.dumps(v,ensure_ascii=False),flush=True)
def command(argv,timeout=12):
    p=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError({'argv':argv,'exit':p.returncode,'stderr':p.stderr[-1000:]})
    return p.stdout.strip()
def notify(text):
    address=os.environ.get('NOTIFY_SOCKET')
    if not address:return
    if address.startswith('@'):address='\0'+address[1:]
    with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as s:s.sendto(text.encode(),address)
def control(action):
    data=(action+'\n').encode('ascii')
    fd=os.open(CONTROL,os.O_WRONLY|os.O_CLOEXEC)
    try:
        if os.write(fd,data)!=len(data):raise RuntimeError('short control write')
    finally:os.close(fd)
def bridge_status():
    return dict(item.split('=',1) for item in STATUS.read_text().strip().split())
def touch_suspend(value):
    paths=list(pathlib.Path('/sys/bus/i2c/devices/3-0020/input').glob('input*/suspend'))
    if len(paths)!=1:raise RuntimeError('missing or ambiguous touch control')
    p=paths[0]
    if (p.parent/'name').read_text().strip()!='dlt_synaptics_dsx':raise RuntimeError('wrong touch target')
    p.write_text('1\n' if value else '0\n')

def preflight():
    global BOOT_CONTEXT
    manifest=json.loads((ROOT/'manifest.json').read_text())
    if os.uname().release!=manifest['kernel_release'] or os.uname().version!=manifest['kernel_version']:
        raise RuntimeError('kernel changed: display modules must be revalidated')
    for name,digest in manifest['files'].items():
        p=ROOT/name
        if p.is_symlink() or p.stat().st_uid!=0 or p.stat().st_mode&0o022:
            raise RuntimeError('untrusted owner/mode '+name)
        if hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise RuntimeError('file hash mismatch '+name)
    boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    from console_boot_context import inspect_context
    BOOT_CONTEXT=inspect_context()
    APP.runtime_mode=BOOT_CONTEXT['mode']
    # This is a LOCAL console: broken/offline networking must not suppress it.
    # Service ordering puts SSH first, but GUI never starts/stops/reconfigures it.
    p=subprocess.run(['systemctl','is-active','ssh'],capture_output=True,text=True,timeout=3)
    if p.returncode:emit(event='SSH-not-active-local-console-still-available')
    for name in ['dlt_iris_live','dlt_dsx','dlt_stock_dsi_clock_adapter','dlt_dsi_parent_guard']:
        if pathlib.Path('/sys/module',name).exists():raise RuntimeError('another display stack owns '+name)
    return boot

def load_module(name,filename,*parameters):
    command(['insmod',str(ROOT/'modules'/filename),*parameters])
    OWNED.append(name)
def load_stack():
    symbols={}
    for line in pathlib.Path('/proc/kallsyms').read_text().splitlines():
        p=line.split()
        if len(p)>2 and p[2] in ('clk_prepare_lock','clk_prepare_unlock','__clk_lookup'):symbols[p[2]]='0x'+p[0]
    if len(symbols)!=3 or not all(int(v,16) for v in symbols.values()):raise RuntimeError('missing clock symbols')
    load_module('dlt_dsi_parent_guard','dlt_dsi_parent_guard.ko',
                'lock_fn_addr='+symbols['clk_prepare_lock'],
                'unlock_fn_addr='+symbols['clk_prepare_unlock'],
                'lookup_fn_addr='+symbols['__clk_lookup'])
    load_module('dlt_dsi_parent_fix','dlt_dsi_parent_fix.ko',
                'lookup_fn_addr='+symbols['__clk_lookup'],'apply=1')
    command(['rmmod','dlt_dsi_parent_fix']);OWNED.remove('dlt_dsi_parent_fix')
    load_module('dlt_stock_dsi_clock_adapter','dlt_stock_dsi_clock_adapter.ko','apply=1')
    load_module('dlt_iris_live','dlt_iris_live.ko','allow_test=1','preserve_existing_power=1')

def start_display(fd,crtc,fb):
    global ready,last_poll
    control('initialize')
    s=bridge_status()
    if s.get('init_rc')!='0' or s.get('panel_status_rc')!='0' or s.get('panel_power')!='156':
        raise RuntimeError('panel failed initial status '+str(s))
    control('brightness64')
    # Shared panel rails are off before SETCRTC. Only probe DSX now, after
    # rails and OLED/IRIS resets are complete; early module load times out.
    load_module('dlt_dsx','dlt_dsx.ko','allow_bind=1')
    touch_suspend(False)
    scanout.submit_frame(fd,crtc,fb)
    ready=True;last_poll=time.monotonic()
    notify('READY=1\nWATCHDOG=1\nSTATUS=Local display active; panel status 0x9c')
    emit(event='display-ready',bridge=s,visual_inspection_confirmed=False)

def stop_display():
    global ready
    ready=False
    notify('STOPPING=1\nSTATUS=Stopping only local display')
    try:
        if pathlib.Path('/sys/module/dlt_dsx').exists():touch_suspend(True)
    finally:
        if CONTROL.exists():control('stop')

def snapshot():
    from console_home_metrics import snapshot as home_snapshot
    return home_snapshot()

def touch_event(kind,x,y):
    APP.pointer(kind,x,y,origin='physical-phone')

def tick(state):
    global last_tick,last_poll,poll_failures,SNAPSHOT,last_frame_state
    changed=APP.poll()
    if POWER_KEY is not None:
        for kind in POWER_KEY.events():
            APP.power_event(kind,'physical-power-key');emit(event='physical-power-key',kind=kind,screen_blank=APP.screen_blank);changed=True
        state['physical_power_key']=POWER_KEY.path
    if UI_CHANNEL is not None:
        for event in UI_CHANNEL.receive():
            if event['type']=='tap':APP.tap(event['x'],event['y'])
            elif event['type']=='key':APP.key(event['key'])
            elif event['type']=='swipe':APP.swipe(event['direction'])
            changed=True
    revision=APP.revision
    if getattr(tick,'last_revision',None)!=revision:
        tick.last_revision=revision;changed=True
    state.update(settings=APP.public_state(),page=APP.page,boot_context=BOOT_CONTEXT)
    state.pop('last_touch',None)
    now=time.monotonic()
    if now-last_tick<2:return changed
    last_tick=now
    notify('WATCHDOG=1')
    if ready and now-last_poll>=15:
        last_poll=now
        try:
            control('status');s=bridge_status()
            if s.get('panel_status_rc')!='0' or s.get('panel_power')!='156':raise RuntimeError(str(s))
            poll_failures=0
        except (OSError,RuntimeError) as exc:
            poll_failures+=1;emit(event='panel-health-failure',count=poll_failures,error=repr(exc))
            if poll_failures>=2:raise RuntimeError('panel failed twice; stop display only')
    try:SNAPSHOT=snapshot()
    except Exception:pass  # A transient status lookup must not tear down the screen.
    marker=(APP.page,json.dumps(SNAPSHOT,sort_keys=True))
    changed=changed or marker!=last_frame_state;last_frame_state=marker
    state.update(resident=True,ui_style='system-console',page=APP.page,modules=SNAPSHOT,
                 privileged_actions_connected=True,device_grants_connected=True,network_settings_connected=True,
                 panel_health_failures=poll_failures)
    return changed

class Renderer(SystemConsole):
    def draw(self,mem,pitch,state,touch_ready=False):
        SETTINGS_VIEW.draw(mem,pitch,APP,SNAPSHOT)

console=SystemConsole()
def main():
    global UI_CHANNEL,SETTINGS_VIEW,POWER_KEY,ESD_LEASE
    RUN.mkdir(mode=0o700,exist_ok=True)
    lease=os.open(RUN/'service.lock',os.O_CREAT|os.O_RDWR,0o600)
    fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
    def interrupted(*_):raise SystemExit(0)
    signal.signal(signal.SIGTERM,interrupted)
    signal.signal(signal.SIGINT,interrupted)
    try:
        boot=preflight();emit(event='preflight-passed',boot_id=boot)
        ESD_LEASE=DisplayLifecycle()
        emit(event='native-ESD-ownership',lease=ESD_LEASE.acquire())
        SETTINGS_VIEW=View(ROOT)
        UI_CHANNEL=Channel()
        POWER_KEY=PowerKeyReader()
        load_stack()
        scanout.RoundedPanel=Renderer
        scanout.DISPLAY_START_HOOK=start_display
        scanout.DISPLAY_STOP_HOOK=stop_display
        scanout.DISPLAY_EVENT_HOOK=touch_event
        scanout.DISPLAY_TICK_HOOK=tick
        sys.argv=[str(ROOT/'dlt_console_scanout.py'),'--resident','--duration','60']
        scanout.main()
    finally:
        if POWER_KEY is not None:POWER_KEY.close()
        if UI_CHANNEL is not None:UI_CHANNEL.close()
        errors=[]
        for module in reversed(OWNED):
            try:command(['rmmod',module],20)
            except Exception as exc:errors.append({'module':module,'error':repr(exc)})
        if ESD_LEASE is not None:
            try:emit(event='native-ESD-restored',result=ESD_LEASE.restore())
            except Exception as exc:errors.append({'ESD_restore':repr(exc)})
        emit(event='display-service-cleanup',errors=errors)
        os.close(lease)
        if errors:raise RuntimeError('display module cleanup incomplete')
if __name__=='__main__':
    try:main()
    except Exception as exc:
        emit(event='display-service-failed',error=repr(exc),traceback=traceback.format_exc())
        raise SystemExit(1)
