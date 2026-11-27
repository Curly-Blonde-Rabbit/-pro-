#!/usr/bin/python3
"""Bounded, standalone DSI calibration. No network or system control actions."""
import argparse, ctypes, fcntl, hashlib, json, mmap, os, pathlib, select
import errno, signal, struct, time, traceback
from rounded_panel_renderer import RoundedPanel, BUTTONS, hit_test

# RAM trial harness may install bounded bridge callbacks. Normal invocation
# has no bridge side effects and never discovers/writes a sysfs control itself.
DISPLAY_START_HOOK = None
DISPLAY_STOP_HOOK = None
DISPLAY_EVENT_HOOK = None
DISPLAY_TICK_HOOK = None

def choose_encoder(connector):
    # After a clean modeset detach GETCONNECTOR.encoder_id is legitimately 0.
    # The connector's compatible encoder list remains the source of truth.
    candidates=connector['encoders']
    current=connector['encoder']
    if current:
        if current not in candidates:raise RuntimeError('encoder not compatible with connector')
        return current
    if len(candidates)!=1:raise RuntimeError('ambiguous inactive connector encoder')
    return candidates[0]

def ioc(fd, n, fmt, *v):
    b = bytearray(struct.pack(fmt, *v))
    fcntl.ioctl(fd, (3 << 30) | (len(b) << 16) | (ord('d') << 8) | n, b, True)
    return struct.unpack(fmt, b)

def submit_frame(fd, crtc, fb, ioctl=ioc, sleep=time.sleep):
    """Commit changed pixels on a command-mode panel, not just the CPU mmap.

    No event flag: this process does not consume the DRM event queue. Only
    EBUSY is retried, at most 60 ms; a real transport error is never hidden.
    """
    for attempt in range(7):
        try:
            ioctl(fd, 0xb0, '<4IQ', crtc, fb, 0, 0, 0)
            return
        except OSError as exc:
            if exc.errno != errno.EBUSY or attempt == 6:
                raise
            sleep(.01)

def preview_ppm(mem,pitch):
    # Read the actual mmap, but convert stride/BGRX in native code instead of
    # doing 666 Python row/slice/write operations on the display thread.
    from PIL import Image
    if pitch<4320:raise ValueError('invalid preview stride')
    image=Image.frombytes('RGB',(1080,666),bytes(mem[:pitch*666]),'raw','BGRX',pitch,1)
    return b'P6\n1080 666\n255\n'+image.tobytes()

def save(p, v):
    t = p.with_suffix('.tmp')
    t.write_text(json.dumps(v, indent=2) + '\n')
    os.replace(t, p)

FONT = dict(zip('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ:-/ .', [
 '111101101101111','010110010010111','111001111100111','111001111001111',
 '101101111001001','111100111001111','111100111101111','111001010010010',
 '111101111101111','111101111001111','010101111101101','110101110101110',
 '111100100100111','110101101101110','111100110100111','111100110100100',
 '111100101101111','101101111101101','111010010010111','001001001101111',
 '101101110101101','100100100100111','101111111101101','101111111111101',
 '111101101101111','111101111100100','111101101111001','110101110101101',
 '111100111001111','111010010010010','101101101101111','101101101101010',
 '101101111111101','101101010101101','101101010010010','111001010100111',
 '000010000010000','000000111000000','001001010100100','000000000000000',
 '000000000000010']))

class Canvas:
    def __init__(self, mem, pitch): self.mem, self.pitch = mem, pitch
    def rect(self, x, y, w, h, rgb):
        x, y, w, h = int(x), int(y), int(w), int(h)
        if x < 0 or y < 0 or x+w > 1080 or y+h > 2340: raise ValueError('bounds')
        row = struct.pack('<I', rgb) * w
        for yy in range(y, y+h):
            start = yy * self.pitch + x*4
            self.mem[start:start+len(row)] = row
    def text(self, x, y, s, color=0xE5E5E5, scale=5):
        for c in s:
            pat = FONT.get(c, FONT[' '])
            for row in range(5):
                for col in range(3):
                    if pat[row*3+col] == '1':
                        self.rect(x+col*scale, y+row*scale, scale, scale, color)
            x += 4*scale

class Touch:
    def __init__(self): self.devices = {}; self.scan_time = 0
    def scan(self):
        if time.monotonic()-self.scan_time < 2: return
        self.scan_time = time.monotonic()
        for p in pathlib.Path('/dev/input').glob('event*'):
            if str(p) in [d['path'] for d in self.devices.values()]: continue
            try:
                fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError:
                # Driver removal/rebind can race the input directory scan.
                continue
            axes = {}
            for code in (0, 1, 53, 54):
                b = bytearray(24)
                try:
                    fcntl.ioctl(fd, (2<<30)|(24<<16)|(ord('E')<<8)|(0x40+code), b, True)
                    v = struct.unpack('<6i', b)
                    if v[2] > v[1]: axes[code] = (v[1], v[2])
                except OSError: pass
            pair = (53,54) if 53 in axes and 54 in axes else (0,1)
            if not all(c in axes for c in pair): os.close(fd); continue
            try:
                name = (pathlib.Path('/sys/class/input')/p.name/'device/name').read_text().strip()
            except OSError:
                os.close(fd); continue
            physical = pathlib.Path('/sys/class/input',p.name,'device').resolve()
            if name != 'dlt_synaptics_dsx' or '/soc/c80000.i2c/' not in str(physical):
                os.close(fd); continue
            self.devices[fd] = dict(path=str(p), name=name, axes=axes, pair=pair,
                                    x=None, y=None, slot=0, down=False, was=False,
                                    pending=b'', quarantine=False)
    def events(self):
        self.scan(); result = []
        ready = select.select(list(self.devices), [], [], 0)[0]
        for fd in ready:
            d = self.devices[fd]
            try: buf = d['pending'] + os.read(fd, 24*128)
            except BlockingIOError: continue
            except OSError as exc:
                if exc.errno not in (errno.ENODEV, errno.EBADF, errno.ENXIO):
                    raise
                try: os.close(fd)
                except OSError: pass
                del self.devices[fd]; continue
            if not buf: os.close(fd); del self.devices[fd]; continue
            count = len(buf)//24; d['pending'] = buf[count*24:]
            for i in range(count):
                _, _, typ, code, val = struct.unpack('llHHi', buf[i*24:i*24+24])
                if typ == 0 and code == 3:  # SYN_DROPPED: no stale admin clicks.
                    d.update(x=None,y=None,down=False,was=False,slot=0,quarantine=True)
                    continue
                if d['quarantine']:
                    if (typ == 3 and code == 57 and val < 0) or (typ == 1 and code == 330 and not val):
                        d['quarantine'] = False
                    continue
                if typ == 3:
                    if code == 47: d['slot'] = val
                    if d['slot'] != 0: continue
                    if code == d['pair'][0]: d['x'] = val
                    elif code == d['pair'][1]: d['y'] = val
                    elif code == 57: d['down'] = val >= 0
                elif typ == 1 and code == 330: d['down'] = bool(val)
                elif typ == 0 and code == 0:
                    if d['x'] is not None and d['y'] is not None and (d['down'] or d['was']):
                        xlo,xhi = d['axes'][d['pair'][0]]; ylo,yhi = d['axes'][d['pair'][1]]
                        x = round((d['x']-xlo)*1079/(xhi-xlo))
                        y = round((d['y']-ylo)*2339/(yhi-ylo))
                        result.append(dict(path=d['path'],raw=[d['x'],d['y']],
                                           native=[x,y],press=d['down'] and not d['was'],kind=('down' if not d['was'] else ('move' if d['down'] else 'up'))))
                    d['was'] = d['down']
        return result
    def info(self):
        return [{k:d[k] for k in ('path','name','axes','pair')} for d in self.devices.values()]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--allocate-only', action='store_true')
    ap.add_argument('--resident', action='store_true')
    ap.add_argument('--duration', type=int, default=1200)
    args = ap.parse_args()
    if not 5 <= args.duration <= 1800: raise ValueError('duration must be 5..1800')
    out = pathlib.Path('/run/dlt-system-console'); out.mkdir(mode=0o700, exist_ok=True)
    lockfd = os.open(str(out/'lock'), os.O_CREAT|os.O_RDWR, 0o600)
    fcntl.flock(lockfd, fcntl.LOCK_EX|fcntl.LOCK_NB)
    state = dict(boot_id=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                 started=time.time(), phase='opening', test_only=not args.resident,
                 usable_rect=[24,24,1032,600], native_size=[1080,2340],
                 hits=[0,0,0,0], input_events=0, inside_events=0,
                 physical_display_confirmed=False, physical_click_verified=False)
    fd=None; mem=None; handle=None; fb=None; old=None; changed=False
    stop = [False]
    def stopped(*_): stop[0] = True
    signal.signal(signal.SIGTERM, stopped); signal.signal(signal.SIGINT, stopped)
    try:
        panel = RoundedPanel()
        fd = os.open('/dev/dri/card0', os.O_RDWR|os.O_CLOEXEC)
        probe = __import__('dlt_display_probe').drm_probe()
        dsis = [c for c in probe['connectors'] if c['type']==16 and c['connection']==1]
        if len(dsis)!=1: raise RuntimeError('expected one connected DSI')
        connector = dsis[0]
        modes = [m for m in connector['modes'] if (m['width'],m['height'],m['refresh'])==(1080,2340,60)]
        if not modes: raise RuntimeError('native mode not present')
        enc = ioc(fd,0xa6,'<5I',choose_encoder(connector),0,0,0,0)
        crtc = enc[2]
        if not crtc:
            eligible=[c for i,c in enumerate(probe['crtcs']) if enc[3] & (1<<i) and not c['fb_id']]
            if not eligible: raise RuntimeError('no free CRTC permitted by DSI encoder')
            crtc=eligible[0]['id']
        old = ioc(fd,0xa1,'<Q7I68s',0,0,crtc,0,0,0,0,0,b'\0'*68)
        if old[3]: raise RuntimeError('existing framebuffer; refusing to replace another UI')
        state.update(connector=connector['id'],crtc=crtc,original_fb=old[3])
        b = ioc(fd,0xb2,'<6IQ',2340,1080,32,0,0,0,0)
        handle,pitch,size=b[4:]
        offset = ioc(fd,0xb3,'<IIQ',handle,0,0)[2]
        mem = mmap.mmap(fd,size,flags=mmap.MAP_SHARED,prot=mmap.PROT_READ|mmap.PROT_WRITE,offset=offset)
        mem[:] = b'\0'*size
        state.update(buffer_size=size,pitch=pitch,phase='allocated-no-modeset')
        save(out/'status.json', state)
        if args.allocate_only:
            print(json.dumps(state),flush=True); return
        fb = ioc(fd,0xae,'<7I',0,1080,2340,pitch,32,24,handle)[0]
        state.update(ui_style='rounded-two-column-v2',button_rects=BUTTONS)
        touch=Touch(); touch.scan()
        def draw():
            panel.draw(mem,pitch,state,touch_ready=bool(touch.devices))
        def write_preview():
            # Atomic readback of the real scanout buffer; rate-limited by caller.
            p=out/'framebuffer-top.ppm';tmp=p.with_suffix('.tmp')
            with tmp.open('wb') as f:f.write(preview_ppm(mem,pitch))
            os.replace(tmp,p)
        draw()
        conn = (ctypes.c_uint32*1)(connector['id'])
        state['phase']='before-modeset'; save(out/'status.json',state)
        ioc(fd,0xa2,'<Q7I68s',ctypes.addressof(conn),1,crtc,fb,0,0,0,1,bytes.fromhex(modes[0]['raw_hex']))
        changed=True
        if DISPLAY_START_HOOK is not None:
            DISPLAY_START_HOOK(fd,crtc,fb)
        actual=ioc(fd,0xa1,'<Q7I68s',0,0,crtc,0,0,0,0,0,b'\0'*68)
        state.update(phase='display-active',framebuffer=fb,crtc_fb_readback=actual[3],
                     crtc_mode_valid=actual[7],inputs=touch.info(),frame_commits=1)
        # Readback file is the actual RGB framebuffer, not a photograph of the OLED.
        write_preview()
        save(out/'status.json',state); print(json.dumps(state),flush=True)
        end=time.monotonic()+args.duration; last_save=0; last_preview=0; preview_dirty=False
        frame_stats={"count":0,"draw_total_ms":0.0,"draw_max_ms":0.0,"submit_total_ms":0.0,"submit_max_ms":0.0,"preview_total_ms":0.0,"preview_count":0}
        while not stop[0] and (args.resident or time.monotonic()<end):
            loop_start=time.monotonic()
            changed_ui=bool(DISPLAY_TICK_HOOK(state)) if DISPLAY_TICK_HOOK is not None else False
            for e in touch.events():
                state['input_events']+=1
                x,y=e['native']; inside=24<=x<1056 and 24<=y<624
                if not inside and e['kind'] == 'down': continue
                state['inside_events']+=1; changed_ui=True
                if DISPLAY_EVENT_HOOK is not None: DISPLAY_EVENT_HOOK(e['kind'],x,y)
                if e['press']:
                    n=hit_test(x,y)
                    if n is not None:
                        state['hits'][n]+=1; state['physical_click_verified']=True
                # No unbounded raw-touch log in the resident service.
            if changed_ui:
                t=time.perf_counter();draw();draw_ms=(time.perf_counter()-t)*1000
                t=time.perf_counter();submit_frame(fd,crtc,fb);submit_ms=(time.perf_counter()-t)*1000
                frame_stats['count']+=1;frame_stats['draw_total_ms']+=draw_ms;frame_stats['submit_total_ms']+=submit_ms
                frame_stats['draw_max_ms']=max(frame_stats['draw_max_ms'],draw_ms);frame_stats['submit_max_ms']=max(frame_stats['submit_max_ms'],submit_ms)
                state['frame_commits']+=1
                preview_dirty=True
            if preview_dirty and time.monotonic()-last_preview>=.15:
                t=time.perf_counter();write_preview();frame_stats['preview_total_ms']+=(time.perf_counter()-t)*1000;frame_stats['preview_count']+=1;last_preview=time.monotonic();preview_dirty=False
            if time.monotonic()-last_save>.5:
                state.update(inputs=touch.info(),updated=time.time(),render_timing=dict(frame_stats),target_loop_hz=60);save(out/'status.json',state)
                last_save=time.monotonic()
            # Only wait out the 60 Hz budget. Idle frames are not redrawn.
            time.sleep(max(0,1/60-(time.monotonic()-loop_start)))
    except Exception as e:
        state.update(phase='error',error=repr(e),traceback=traceback.format_exc())
        print(json.dumps(state),flush=True); raise
    finally:
        errors=[]
        if changed and fd is not None:
            if DISPLAY_STOP_HOOK is not None:
                try: DISPLAY_STOP_HOOK()
                except Exception as e: errors.append('bridge-stop:'+repr(e))
            try:
                # No pre-existing framebuffer: blank and detach only our scanout.
                mem[:]=b'\0'*len(mem)
                ioc(fd,0xa2,'<Q7I68s',0,0,old[2],0,0,0,0,0,b'\0'*68)
            except Exception as e: errors.append('detach:'+repr(e))
        if fb is not None:
            try: ioc(fd,0xaf,'<I',fb)
            except Exception as e: errors.append('rmfb:'+repr(e))
        if mem is not None: mem.close()
        if handle is not None:
            try: ioc(fd,0xb4,'<I',handle)
            except Exception as e: errors.append('destroy:'+repr(e))
        if fd is not None: os.close(fd)
        state.update(ended=time.time(),cleanup_errors=errors)
        if state['phase']!='error': state['phase']='stopped'
        save(out/'status.json',state)
        os.close(lockfd)

if __name__=='__main__': main()
