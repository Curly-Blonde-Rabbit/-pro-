"""Root-only Unix UI event channel. No arbitrary shell or external listener."""
import socket,os,pathlib,json,struct,sys
PATH='/run/dlt-system-console/ui.sock'

def validate(v):
    if not isinstance(v,dict):raise ValueError('invalid event')
    kind=v.get('type')
    if kind=='tap' and set(v)=={'type','x','y'}:
        if type(v['x']) is not int or type(v['y']) is not int or not (24<=v['x']<1056 and 24<=v['y']<624):raise ValueError('invalid coordinate')
    elif kind=='key' and set(v)=={'type','key'}:
        k=v['key']
        if not isinstance(k,str) or not ((len(k)==1 and 32<=ord(k)<=126) or k in ('BACKSPACE','ENTER','ESCAPE','DONE')):raise ValueError('invalid key')
    elif kind=='swipe' and set(v)=={'type','direction'}:
        if v['direction'] not in ('up','down'):raise ValueError('invalid direction')
    else:raise ValueError('invalid event type')
    return v

class Channel:
    def __init__(self):
        p=pathlib.Path(PATH)
        if p.exists():
            if not p.is_socket() or p.stat().st_uid!=0:raise RuntimeError('unsafe UI socket path')
            p.unlink()
        self.socket=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.socket.bind(PATH);os.chmod(PATH,0o600);self.socket.listen(4);self.socket.setblocking(False)
    def receive(self):
        events=[]
        for _ in range(8):
            try:client,_=self.socket.accept()
            except BlockingIOError:break
            with client:
                client.settimeout(.03)
                try:
                    _,uid,_=struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                    if uid!=0:raise ValueError('root required')
                    data=b''
                    while not data.endswith(b'\n') and len(data)<=512:
                        part=client.recv(513-len(data))
                        if not part:break
                        data+=part
                    if len(data)>512:raise ValueError('oversize event')
                    event=validate(json.loads(data));events.append(event)
                    client.sendall(b'{"queued":true}\n')
                except (ValueError,OSError,TypeError):
                    try:client.sendall(b'{"queued":false}\n')
                    except OSError:pass
        return events
    def close(self):
        self.socket.close()
        try:pathlib.Path(PATH).unlink()
        except FileNotFoundError:pass

def send(v):
    data=json.dumps(validate(v),separators=(',',':')).encode()
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(3);s.connect(PATH);s.sendall(data+b'\n')
        answer=json.loads(s.recv(512))
        if answer!={'queued':True}:raise RuntimeError('UI event rejected')
    return answer
if __name__=='__main__':
    try:
        data=sys.stdin.buffer.read(513)
        if len(data)>512:raise ValueError('oversize event')
        print(json.dumps(send(json.loads(data))))
    except (ValueError,OSError,RuntimeError):
        print('{"queued":false}');raise SystemExit(1)
