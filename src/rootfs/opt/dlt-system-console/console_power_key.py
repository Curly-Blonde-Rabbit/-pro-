"""Physical PMIC key reader; never grabs input or changes PMIC reset timers."""
import os,pathlib,struct,time,fcntl,errno,secrets,json,subprocess

class Gesture:
    def __init__(self):self.cancel()
    def cancel(self):self.down=None;self.event_down=None;self.long_sent=False;self.quarantine=False
    def dropped(self):self.cancel();self.quarantine=True
    def key(self,value,stamp,now):
        if self.quarantine:
            if value==0:self.cancel()
            return None
        if value==1 and self.down is None:self.down=now;self.event_down=stamp;self.long_sent=False
        elif value==0 and self.down is not None:
            duration=max(0,stamp-self.event_down);long=self.long_sent or duration>=1.8
            result=None if self.long_sent else ('long' if long else ('short' if .025<=duration<=1.8 else None))
            self.cancel();return result
        return None
    def poll(self,now):
        if self.down is not None and not self.long_sent and now-self.down>=1.8:
            self.long_sent=True;return 'long'
        return None

class Reader:
    def __init__(self):self.fd=None;self.path=None;self.pending=b'';self.gesture=Gesture();self.last_scan=0
    def close(self):
        if self.fd is not None:
            try:os.close(self.fd)
            except OSError:pass
        self.fd=None;self.path=None;self.pending=b'';self.gesture.cancel()
    def events(self):
        now=time.monotonic();out=[]
        if self.fd is None and now-self.last_scan>=2:
            self.last_scan=now
            for p in pathlib.Path('/sys/class/input').glob('event*'):
                try:
                    dev=(p/'device').resolve()
                    if (p/'device/name').read_text().strip()!='qpnp_pon' or '/soc/c440000.qcom,spmi/' not in str(dev):continue
                    fd=os.open('/dev/input/'+p.name,os.O_RDONLY|os.O_NONBLOCK|os.O_CLOEXEC)
                    try:fcntl.ioctl(fd,0x400445a0,struct.pack('i',1))  # EVIOCSCLOCKID, CLOCK_MONOTONIC
                    except OSError:pass
                    self.fd=fd;self.path='/dev/input/'+p.name;break
                except OSError:continue
        if self.fd is None:return out
        try:data=os.read(self.fd,24*128)
        except BlockingIOError:data=None
        except OSError as e:
            if e.errno not in (errno.ENODEV,errno.EBADF,errno.ENXIO):raise
            self.close();return out
        if data==b'':self.close();return out
        if data:
            buf=self.pending+data;count=len(buf)//24;self.pending=buf[count*24:]
            for i in range(count):
                sec,usec,typ,code,value=struct.unpack('llHHi',buf[i*24:(i+1)*24])
                if typ==0 and code==3:self.gesture.dropped()
                elif typ==1 and code==116:
                    kind=self.gesture.key(value,sec+usec/1e6,now)
                    if kind:out.append(kind)
        kind=self.gesture.poll(now)
        if kind:out.append(kind)
        return out

def schedule(action,origin):
    if os.geteuid()!=0 or origin!='physical-phone' or action not in ('poweroff','reboot'):raise PermissionError('请在手机实体屏幕确认')
    from console_recovery import active_operation,atomic,HISTORY,boot
    if active_operation():raise RuntimeError('恢复任务执行中，请等待完成')
    token=secrets.token_hex(16)
    receipt={'transaction':token,'action':action,'title':'关机' if action=='poweroff' else '重启','phase':'queued','boot_id':boot(),'time':time.time(),'data_preserved':True,'origin':'physical-phone'}
    atomic(HISTORY/(token+'.json'),receipt)
    p=subprocess.run(['systemd-run','--quiet','--collect','--unit=dlt-local-power-'+token[:12],'--on-active=3s','/bin/systemctl','--no-block',action],capture_output=True,text=True,timeout=8)
    if p.returncode:
        receipt.update(phase='failed',message='关停请求未提交');atomic(HISTORY/(token+'.json'),receipt);raise RuntimeError('关停请求未提交')
    receipt.update(phase='reboot-scheduled',message='已提交有序关停');atomic(HISTORY/(token+'.json'),receipt)
    return receipt
