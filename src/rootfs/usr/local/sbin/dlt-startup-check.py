#!/usr/bin/python3
"""Bounded startup checks. No reboot, driver changes, packet capture or key logging."""
import argparse,json,os,pathlib,socket,subprocess,sys,tempfile,time
STAGES=('storage','hardware','network','ssh')
def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(data,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(name,path)
        if os.name=='posix':
            parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(parent)
            finally:os.close(parent)
    finally:
        if os.path.exists(name):os.unlink(name)
def wait_for(probe,timeout,interval,clock=time.monotonic,sleep=time.sleep):
    deadline=clock()+timeout;attempts=0;error=None
    while True:
        attempts+=1
        try:
            if probe():return {'passed':True,'attempts':attempts,'last_error':None}
        except Exception as e:error=type(e).__name__+': '+str(e)
        remaining=deadline-clock()
        if remaining<=0:return {'passed':False,'attempts':attempts,'last_error':error or 'timeout'}
        sleep(min(interval,remaining))
def validate(config):
    limits=config.get('timeouts',{})
    if set(limits)!=set(STAGES):raise ValueError('Exactly four stage timeouts are required')
    for value in limits.values():
        if type(value) not in (int,float) or not 0<=value<=180:raise ValueError('Timeout must be between 0 and 180 seconds')
    if sum(limits.values())>360:raise ValueError('Total startup wait exceeds 360 seconds')
    if config.get('automatic_reboot',False):raise ValueError('Automatic reboot is not implemented or enabled')
    return config
def execute(config,probes,publish,clock=time.monotonic,sleep=time.sleep):
    validate(config)
    state={'schema':1,'mode':'checking','automatic_reboot':False,'checks':{},'timestamp':time.time()}
    publish(state)
    for name in STAGES:
        state['checks'][name]=wait_for(probes[name],config['timeouts'][name],1,clock,sleep)
        publish(state)
    state['mode']='ready' if all(r['passed'] for r in state['checks'].values()) else 'maintenance'
    state['timestamp']=time.time();publish(state);return state
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',default='/etc/dlt-server/startup.json')
    args=parser.parse_args();config=validate(json.loads(pathlib.Path(args.config).read_text()))
    state_dir=pathlib.Path(config['state_directory']);ready=pathlib.Path('/run/dlt-server/ready')
    if ready.exists():ready.unlink()
    def storage():
        state_dir.mkdir(parents=True,exist_ok=True)
        if os.statvfs(state_dir).f_bavail*os.statvfs(state_dir).f_frsize<64*1024*1024:return False
        fd,name=tempfile.mkstemp(prefix='.durability-probe-',dir=state_dir)
        try:
            with os.fdopen(fd,'wb') as f:f.write(b'DLT_STORAGE_CHECK\n');f.flush();os.fsync(f.fileno())
        finally:os.unlink(name)
        return True
    def network():
        result=subprocess.run(['/usr/sbin/ip','-j','address','show'],capture_output=True,text=True,timeout=3,check=True)
        return any(a.get('scope')=='global' and a.get('family')=='inet'
                   for link in json.loads(result.stdout) if link.get('operstate')=='UP'
                   for a in link.get('addr_info',[]))
    def ssh():
        with socket.create_connection(('127.0.0.1',22),timeout=1):return True
    def publish(state):
        try:save(state_dir/'startup-state.json',state)
        except OSError as e:print('PERSISTENT_STATE_WRITE_FAILED: '+str(e),file=sys.stderr)
        save(pathlib.Path('/run/dlt-server/startup-state.json'),state)
    state=execute(config,{'storage':storage,'hardware':lambda:pathlib.Path(config['hardware_ready_marker']).is_file(),'network':network,'ssh':ssh},publish)
    if state['mode']=='ready':save(ready,{'ready':True})
    print(json.dumps(state));return 0 if state['mode']=='ready' else 2
if __name__=='__main__':sys.exit(main())

