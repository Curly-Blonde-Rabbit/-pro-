"""Receive-only AF_PACKET capture: bounded RAM -> stdout, never packet files."""
import argparse,collections,ctypes,fcntl,hashlib,ipaddress,json,os,pathlib,re,resource,selectors,signal,socket,struct,sys,time,uuid
sys.dont_write_bytecode=True
VERSION='E534-default-off-module-v1.3'
RUN=pathlib.Path('/run/dlt-netcapture')
MAX_PACKET=262144

def block(kind,data):
    data+=b'\0'*((-len(data))%4);size=12+len(data)
    return struct.pack('<II',kind,size)+data+struct.pack('<I',size)
def option(kind,data):return struct.pack('<HH',kind,len(data))+data+b'\0'*((-len(data))%4)
def headers(names,snaplen):
    out=block(0x0a0d0d0a,struct.pack('<IHHq',0x1a2b3c4d,1,0,-1))
    for name in names:out+=block(1,struct.pack('<HHI',1,0,snaplen)+option(2,name.encode())+option(9,b'\x09')+option(0,b''))
    return out

def packet_block(index,stamp,data,original):
    return block(6,struct.pack('<IIIII',index,stamp>>32,stamp&0xffffffff,len(data),original)+data)

def transport(value):
    if not value:return None
    fields=value.split()
    if len(fields)!=4:raise ValueError('transport requires SSH_CONNECTION IPv4 four-tuple')
    src,sp,dst,dp=fields;src=ipaddress.IPv4Address(src);dst=ipaddress.IPv4Address(dst);sp=int(sp);dp=int(dp)
    if not 1<=sp<=65535 or not 1<=dp<=65535:raise ValueError('invalid transport ports')
    return (int(src),sp,int(dst),dp)

class BPF:
    def __init__(self):self.code=[];self.labels={}
    def label(self,n):self.labels[n]=len(self.code)
    def emit(self,code,k=0,jt=0,jf=0):self.code.append([code,jt,jf,k])
    def jeq(self,k,yes,no):self.emit(0x15,k,yes,no)
    def instructions(self):
        result=[]
        for i,(code,jt,jf,k) in enumerate(self.code):
            offsets=[]
            for x in (jt,jf):
                x=self.labels[x]-i-1 if isinstance(x,str) else x
                if not 0<=x<=255:raise ValueError('BPF branch outside bounds')
                offsets.append(x)
            result.append((code,*offsets,k))
        return result

def filter_program(flow,snaplen):
    if flow is None:return [(6,0,0,snaplen)]
    src,sp,dst,dp=flow;b=BPF();b.emit(0x28,12);b.jeq(0x0800,'ip14','vlan1');b.label('vlan1');b.jeq(0x8100,'tag1','vlan2');b.label('vlan2');b.jeq(0x88a8,'tag1','accept');b.label('tag1');b.emit(0x28,16);b.jeq(0x0800,'ip18','vlan3');b.label('vlan3');b.jeq(0x8100,'tag2','vlan4');b.label('vlan4');b.jeq(0x88a8,'tag2','accept');b.label('tag2');b.emit(0x28,20);b.jeq(0x0800,'ip22','accept')
    for offset in (14,18,22):
        tag='ip'+str(offset);b.label(tag);b.emit(0x30,offset+9);b.jeq(6,tag+'src','accept');b.label(tag+'src');b.emit(0x20,offset+12);b.jeq(src,tag+'fwd',tag+'revsrc');b.label(tag+'revsrc');b.jeq(dst,tag+'rev','accept')
        for direction,saddr,daddr,sport,dport in [('fwd',src,dst,sp,dp),('rev',dst,src,dp,sp)]:
            t=tag+direction;b.label(t);b.emit(0x20,offset+16);b.jeq(daddr,t+'frag','accept');b.label(t+'frag');b.emit(0x28,offset+6);b.emit(0x54,0x1fff);b.jeq(0,t+'ports','drop');b.label(t+'ports');b.emit(0xb1,offset);b.emit(0x48,offset);b.jeq(sport,t+'dst','accept');b.label(t+'dst');b.emit(0x48,offset+2);b.jeq(dport,'drop','accept')
    b.label('accept');b.emit(6,snaplen);b.label('drop');b.emit(6,0);return b.instructions()

class SockFilter(ctypes.Structure):_fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint)]
class SockProg(ctypes.Structure):_fields_=[('length',ctypes.c_ushort),('filters',ctypes.POINTER(SockFilter))]
def attach(sock,program):
    instructions=(SockFilter*len(program))(*(SockFilter(*x) for x in program));prog=SockProg(len(program),instructions);lib=ctypes.CDLL(None,use_errno=True)
    if lib.setsockopt(sock.fileno(),socket.SOL_SOCKET,26,ctypes.byref(prog),ctypes.sizeof(prog))!=0:raise OSError(ctypes.get_errno(),'attach capture transport exclusion')

def private_run():
    if os.geteuid()!=0:raise PermissionError('root capture/status required')
    for p in (RUN,*RUN.parents):
        if p.is_symlink():raise ValueError('unsafe runtime path')
    # Ensure metadata, locks and temporary JSON are RAM-backed too.
    lines=pathlib.Path('/proc/self/mountinfo').read_text().splitlines()
    if not any(x.split()[4]=='/run' and x.split(' - ',1)[1].split()[0]=='tmpfs' for x in lines):raise ValueError('/run is not tmpfs')
    RUN.mkdir(mode=0o700,exist_ok=True);st=RUN.stat()
    if st.st_uid or st.st_mode&0o077:raise PermissionError('runtime metadata owner/mode mismatch')

def save(path,value):
    temp=path.with_suffix('.new');fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f,sort_keys=True)
    os.replace(temp,path)

def swaps_active():return len(pathlib.Path('/proc/swaps').read_text().splitlines())>1

def harden():
    if swaps_active():raise RuntimeError('capture refuses active swap: RAM-only policy')
    resource.setrlimit(resource.RLIMIT_CORE,(0,0));resource.setrlimit(resource.RLIMIT_AS,(256<<20,256<<20));lock_limit=min(192<<20,resource.getrlimit(resource.RLIMIT_MEMLOCK)[1]);resource.setrlimit(resource.RLIMIT_MEMLOCK,(lock_limit,lock_limit))
    lib=ctypes.CDLL(None,use_errno=True)
    if lib.mlockall(3)!=0:raise OSError(ctypes.get_errno(),'RAM lock required')
    if lib.prctl(38,1,0,0,0)!=0:raise OSError(ctypes.get_errno(),'no-new-privileges')
    class Header(ctypes.Structure):_fields_=[('version',ctypes.c_uint),('pid',ctypes.c_int)]
    class Data(ctypes.Structure):_fields_=[('effective',ctypes.c_uint),('permitted',ctypes.c_uint),('inheritable',ctypes.c_uint)]
    data=(Data*2)();head=Header(0x20080522,0)
    if lib.capset(ctypes.byref(head),ctypes.byref(data))!=0:raise OSError(ctypes.get_errno(),'drop capture process capabilities')
    if lib.prctl(4,0,0,0,0)!=0:raise OSError(ctypes.get_errno(),'disable process dumps')

def brief(frame):
    if len(frame)<14:return 'short-frame'
    et=struct.unpack_from('!H',frame,12)[0];off=14
    for _ in range(2):
        if et not in (0x8100,0x88a8) or len(frame)<off+4:break
        et=struct.unpack_from('!H',frame,off+2)[0];off+=4
    if et==0x0800 and len(frame)>=off+20:
        ihl=(frame[off]&15)*4;proto=frame[off+9];s=socket.inet_ntoa(frame[off+12:off+16]);d=socket.inet_ntoa(frame[off+16:off+20]);frag=struct.unpack_from('!H',frame,off+6)[0]&0x1fff
        if proto in (6,17) and not frag and len(frame)>=off+ihl+4:
            sp,dp=struct.unpack_from('!HH',frame,off+ihl);return '%s %s:%d -> %s:%d'%('TCP' if proto==6 else 'UDP',s,sp,d,dp)
        return 'IPv4 %s -> %s proto=%d'%(s,d,proto)
    if et==0x86dd and len(frame)>=off+40:return 'IPv6 %s -> %s'%(socket.inet_ntop(socket.AF_INET6,frame[off+8:off+24]),socket.inet_ntop(socket.AF_INET6,frame[off+24:off+40]))
    return 'EtherType=0x%04x'%et

class OutputRate:
    def __init__(self,bytes_per_second,clock=time.monotonic):
        self.rate=bytes_per_second;self.clock=clock;self.last=clock();self.tokens=min(bytes_per_second,65536);self.capacity=min(bytes_per_second,65536)
    def allowance(self):
        now=self.clock();self.tokens=min(self.capacity,self.tokens+max(0,now-self.last)*self.rate);self.last=now
        return int(self.tokens)
    def write(self,fd,data):
        n=min(len(data),self.allowance())
        if not n:raise BlockingIOError('capture output rate limit')
        written=os.write(fd,data[:n]);self.tokens-=written;return written

def resource_guard():
    mem={line.split(':',1)[0]:int(line.split()[1]) for line in pathlib.Path('/proc/meminfo').read_text().splitlines() if len(line.split())>=2 and line.split()[1].isdigit()}
    if mem.get('MemAvailable',0)<512*1024:return 'stopped-low-system-memory'
    temp=pathlib.Path('/sys/class/power_supply/battery/temp')
    if temp.exists() and int(temp.read_text())>=420:return 'stopped-high-battery-temperature'
    return None

def capture(args):
    private_run()
    import importlib.util
    pp=pathlib.Path(__file__).resolve().with_name('capture_policy.py')
    spec=importlib.util.spec_from_file_location('dlt_capture_policy_runtime',pp);policy_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy_module);require_enabled=policy_module.require_enabled
    if not args.isolated_test:require_enabled()
    flow=transport(args.transport)
    if not flow and not args.isolated_test:raise ValueError('SSH transport tuple is required to prevent capture feedback')
    if args.isolated_test and pathlib.Path('/proc/self/ns/net').readlink()==pathlib.Path('/proc/1/ns/net').readlink():raise ValueError('isolated-test flag requires separate network namespace')
    names=args.interface
    if not 1<=len(names)<=16 or len(set(names))!=len(names):raise ValueError('choose one to sixteen distinct interfaces')
    if not re.fullmatch('[A-Za-z0-9_-]{1,64}',args.session):raise ValueError('invalid session ID')
    lock=os.open(RUN/'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state_path=RUN/(args.session+'.json')
    if state_path.exists():raise ValueError('session already exists')
    if not args.isolated_test and args.duration==0 and args.role=='diagnostic':raise ValueError('diagnostic capture must be bounded')
    if args.role=='gateway' and pathlib.Path('/proc/sys/net/ipv4/ip_forward').read_text().strip()!='1':raise ValueError('gateway role requires existing IPv4 forwarding; this tool never changes routes')
    for name in names:
        if not re.fullmatch('[A-Za-z0-9_.:-]{1,15}',name):raise ValueError('invalid interface name')
        info=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        try:kind=struct.unpack_from('H',fcntl.ioctl(info,0x8927,struct.pack('256s',name.encode())),16)[0]
        finally:info.close()
        if kind not in (1,772):raise ValueError('only Ethernet-style link headers supported')
    if args.role=='mirror':
        # Refuse management-address-bearing capture interfaces. No ARP spoofing,
        # DHCP, probes or route edits are performed by this program.
        for name in names:
            fd=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            try:
                try:fcntl.ioctl(fd,0x8915,struct.pack('256s',name.encode()));raise ValueError('mirror interface has IPv4 address; use a dedicated receive interface')
                except OSError:pass
            finally:fd.close()
        for line in pathlib.Path('/proc/net/if_inet6').read_text().splitlines():
            if line.split()[-1] in names:raise ValueError('mirror interface has IPv6 address')
    state={'version':VERSION,'session':args.session,'pid':os.getpid(),'pid_start':pathlib.Path('/proc/self/stat').read_text().split()[21],'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'role':args.role,'interfaces':names,'running':False,'payload_storage':'locked RAM and SSH stdout only','queue_limit_bytes':args.buffer_mib<<20,'seen_packets':0,'captured_bytes':0,'queue_drops':0,'kernel_packets':0,'kernel_drops':0,'output_bytes':0,'queue_bytes':0,'reason':'starting','max_output_MiB_per_second':args.max_mibps,'priority':'business-first','frame_headers':'Ethernet; no WiFi monitor/radiotap','transport_exclusion':'exact IPv4 SSH four-tuple; noninitial TCP fragments for same endpoint pair also excluded'}
    sockets=[];sel=selectors.DefaultSelector();queue=collections.deque();queued=0;head_offset=0;stop=False;reason='duration-complete';error=None;start=time.monotonic();recvbuf=bytearray(args.snaplen);rate=OutputRate(args.max_mibps*(1<<20))
    def terminate(*_):
        nonlocal stop,reason
        stop=True;reason='stopped-by-signal'
    signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGINT,terminate);signal.signal(signal.SIGHUP,terminate)
    try:
        for i,name in enumerate(names):
            s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3));s.bind((name,0));s.setsockopt(socket.SOL_SOCKET,35,1)
            try:s.setsockopt(socket.SOL_SOCKET,33,1<<20)
            except OSError:s.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1<<20)
            if args.promiscuous:s.setsockopt(263,1,struct.pack('IHH8s',socket.if_nametoindex(name),1,0,b'\0'*8))
            attach(s,filter_program(flow,args.snaplen));s.setblocking(False);sel.register(s,selectors.EVENT_READ,('packet',i));sockets.append(s)
        harden();state.update(running=True,reason='capturing',memory_locked=True,capabilities_dropped=True,process_security={k:v.strip() for k,v in (x.split(':',1) for x in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in x) if k in ('CapEff','NoNewPrivs','VmLck','VmRSS')});save(state_path,state)
        os.set_blocking(1,False)
        if not args.no_control_stdin:
            os.set_blocking(0,False);sel.register(0,selectors.EVENT_READ,('control',0))
        initial=headers(names,args.snaplen) if args.format=='pcapng' else ('DLT RAM capture %s; %s\n'%(args.session,','.join(names))).encode()
        queue.append(initial);queued=len(initial);sel.register(1,selectors.EVENT_WRITE,('output',0));output_registered=True;last_stats=0
        def refresh(final=False):
            nonlocal last_stats
            for s in sockets:
                try:
                    packets,drops=struct.unpack('II',s.getsockopt(263,6,8));state['kernel_packets']+=packets;state['kernel_drops']+=drops
                except OSError:pass
            state.update(queue_bytes=queued,elapsed_seconds=round(time.monotonic()-start,3),updated=time.time());save(state_path,state);last_stats=time.monotonic()
        while not stop and (not args.duration or time.monotonic()-start<args.duration):
            now=time.monotonic()
            if now-last_stats>=1:
                if not args.isolated_test:
                    try:require_enabled()
                    except RuntimeError:reason='module-disabled';break
                problem=resource_guard()
                if problem:reason=problem;break
                if swaps_active():raise RuntimeError('swap activated during RAM-only capture')
                refresh()
            if queue and rate.allowance()==0:time.sleep(.01)
            for key,_ in sel.select(.1):
                kind,index=key.data
                if kind=='control':
                    if os.read(0,4096)==b'':stop=True;reason='control-channel-closed'
                elif kind=='output':
                    for _ in range(32):
                        if not queue:break
                        try:n=rate.write(1,memoryview(queue[0])[head_offset:])
                        except BlockingIOError:break
                        if not n:raise BrokenPipeError('receiver closed')
                        state['output_bytes']+=n;queued-=n;head_offset+=n
                        if head_offset==len(queue[0]):queue.popleft();head_offset=0
                    if not queue:sel.unregister(1);output_registered=False
                else:
                    s=key.fileobj
                    for _ in range(64):
                        try:n,anc,flags,address=s.recvmsg_into([recvbuf],128,socket.MSG_TRUNC|socket.MSG_DONTWAIT)
                        except BlockingIOError:break
                        data=bytes(memoryview(recvbuf)[:min(n,args.snaplen)]);stamp=time.time_ns()
                        for level,typ,value in anc:
                            if level==socket.SOL_SOCKET and typ==35 and len(value)>=16:
                                sec,nsec=struct.unpack_from('qq',value);stamp=sec*1000000000+nsec
                        state['seen_packets']+=1;state['captured_bytes']+=len(data)
                        record=packet_block(index,stamp,data,n) if args.format=='pcapng' else ('%d.%09d %s %s length=%d\n'%(stamp//10**9,stamp%10**9,names[index],brief(data),n)).encode()
                        if queued+len(record)>(args.buffer_mib<<20) or len(queue)>=8192:state['queue_drops']+=1;continue
                        queue.append(record);queued+=len(record)
                        if not output_registered:sel.register(1,selectors.EVENT_WRITE,('output',0));output_registered=True
        # Drain only for a bounded interval; no on-flash fallback if receiver stalls.
        deadline=time.monotonic()+2
        while queue and time.monotonic()<deadline:
            try:n=rate.write(1,memoryview(queue[0])[head_offset:])
            except BlockingIOError:time.sleep(.01);continue
            state['output_bytes']+=n;queued-=n;head_offset+=n
            if head_offset==len(queue[0]):queue.popleft();head_offset=0
        state['undelivered_records_at_exit']=len(queue)
    except BrokenPipeError:reason='receiver-disconnected'
    except BaseException as exc:error=type(exc).__name__+': '+str(exc);reason='capture-error'
    finally:
        for s in sockets:
            try:
                packets,drops=struct.unpack('II',s.getsockopt(263,6,8));state['kernel_packets']+=packets;state['kernel_drops']+=drops
            except OSError:pass
            s.close()
        sel.close();state.update(running=False,reason=reason,error=error,queue_bytes=queued,elapsed_seconds=round(time.monotonic()-start,3),updated=time.time());save(state_path,state);os.close(lock)
        print(json.dumps({'session':args.session,'reason':reason,'packets':state['seen_packets'],'queue_drops':state['queue_drops'],'kernel_drops':state['kernel_drops'],'error':error}),file=sys.stderr,flush=True)
    return 1 if error else 0

def status(args):
    private_run();items=[]
    for p in sorted(RUN.glob('*.json')):
        if p.is_symlink() or p.stat().st_size>32768:continue
        try:
            item=json.loads(p.read_text())
            if 'session' in item:items.append(item)
        except (ValueError,OSError):continue
    import importlib.util
    pp=pathlib.Path(__file__).resolve().with_name('capture_policy.py');spec=importlib.util.spec_from_file_location('dlt_capture_policy_status',pp);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module_state=module.read
    print(json.dumps({'version':VERSION,'module':module_state(),'sessions':items,'swaps_active':swaps_active()}));return 0

def stop_session(args):
    private_run()
    if not re.fullmatch('[A-Za-z0-9_-]{1,64}',args.session):raise ValueError('invalid session')
    p=RUN/(args.session+'.json');st=p.stat()
    if p.is_symlink() or st.st_uid or st.st_mode&0o077 or st.st_size>32768:raise ValueError('unsafe status')
    state=json.loads(p.read_text());pid=state['pid'];current=pathlib.Path('/proc')/str(pid)
    if not state['running']:print(json.dumps({'stopped':True,'already_finished':True}));return 0
    if state['boot_id']!=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip() or (current/'stat').read_text().split()[21]!=state['pid_start']:raise ValueError('PID/boot identity mismatch')
    os.kill(pid,signal.SIGTERM);print(json.dumps({'stop_requested':True,'session':args.session}));return 0

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True);p=sub.add_parser('capture');p.add_argument('--interface',action='append',required=True);p.add_argument('--session',default=uuid.uuid4().hex);p.add_argument('--transport');p.add_argument('--role',choices=['gateway','mirror','diagnostic'],default='diagnostic');p.add_argument('--format',choices=['pcapng','summary'],default='pcapng');p.add_argument('--buffer-mib',type=int,choices=range(1,65),default=16);p.add_argument('--max-mibps',type=float,default=2);p.add_argument('--snaplen',type=int,default=MAX_PACKET);p.add_argument('--duration',type=float,default=60);p.add_argument('--promiscuous',action='store_true');p.add_argument('--isolated-test',action='store_true');p.add_argument('--no-control-stdin',action='store_true');sub.add_parser('status');p=sub.add_parser('stop');p.add_argument('--session',required=True);args=parser.parse_args()
    if args.command=='capture':
        if args.no_control_stdin and args.duration==0:parser.error('closed-stdin mode requires bounded duration')
        if not .125<=args.max_mibps<=16:parser.error('output limit must be 0.125 through 16 MiB/s')
        if not 64<=args.snaplen<=MAX_PACKET or not 0<=args.duration<=86400:parser.error('invalid snaplen or duration')
        return capture(args)
    return status(args) if args.command=='status' else stop_session(args)
if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:print(type(exc).__name__+': '+str(exc),file=sys.stderr);sys.exit(2)
