#!/usr/bin/python3
"""Read-only DRM resource and evdev range inspection; no display mode changes."""
import array,ctypes,fcntl,json,os,pathlib,struct
def ioctl(fd,n,fmt,*v):
    b=bytearray(struct.pack(fmt,*v));fcntl.ioctl(fd,(3<<30)|(struct.calcsize(fmt)<<16)|(ord('d')<<8)|n,b,True);return struct.unpack(fmt,b)
def mode(b):
    v=struct.unpack('<I10HIII32s',b);return {'name':v[-1].split(b'\0')[0].decode(),'width':v[1],'height':v[6],'refresh':v[11],'type':v[13],'raw_hex':b.hex()}
def drm_probe():
    fd=os.open('/dev/dri/card0',os.O_RDWR|os.O_CLOEXEC)
    try:
        r=ioctl(fd,0xa0,'<4Q8I',*([0]*12));counts=r[4:8]
        arrays=[(ctypes.c_uint32*max(n,1))() for n in counts]
        r=ioctl(fd,0xa0,'<4Q8I',*[ctypes.addressof(x) for x in arrays],*r[4:]);out={'resources':{'framebuffers':list(arrays[0])[:r[4]],'crtcs':list(arrays[1])[:r[5]],'connectors':list(arrays[2])[:r[6]],'encoders':list(arrays[3])[:r[7]],'limits':r[8:]},'connectors':[],'crtcs':[]}
        for cid in out['resources']['connectors']:
            q=ioctl(fd,0xa7,'<4Q12I',*([0]*8),cid,*([0]*7))
            modes=(ctypes.c_ubyte*(max(q[4],1)*68))();enc=(ctypes.c_uint32*max(q[6],1))();props=(ctypes.c_uint32*max(q[5],1))();vals=(ctypes.c_uint64*max(q[5],1))()
            q=ioctl(fd,0xa7,'<4Q12I',ctypes.addressof(enc),ctypes.addressof(modes),ctypes.addressof(props),ctypes.addressof(vals),*q[4:])
            out['connectors'].append({'id':cid,'type':q[9],'type_id':q[10],'connection':q[11],'encoder':q[7],'encoders':list(enc)[:q[6]],'modes':[mode(bytes(modes[i*68:(i+1)*68])) for i in range(q[4])]})
        for cid in out['resources']['crtcs']:
            v=ioctl(fd,0xa1,'<Q7I68s',0,0,cid,0,0,0,0,0,b'\0'*68)
            out['crtcs'].append({'id':cid,'fb_id':v[3],'x':v[4],'y':v[5],'mode_valid':v[7],'mode':mode(v[8]) if v[7] else None,'raw_hex':struct.pack('<Q7I68s',*v).hex()})
        out['dumb_buffer_cap']=ioctl(fd,0x0c,'<QQ',1,0)[1]
        return out
    finally:os.close(fd)
def inputs():
    result=[]
    for p in sorted(pathlib.Path('/dev/input').glob('event*')):
        fd=os.open(p,os.O_RDONLY|os.O_NONBLOCK|os.O_CLOEXEC)
        try:
            row={'path':str(p),'name':(pathlib.Path('/sys/class/input')/p.name/'device/name').read_text().strip(),'axes':{}}
            for code in (0,1,47,53,54,57):
                b=bytearray(24)
                try:fcntl.ioctl(fd,(2<<30)|(24<<16)|(ord('E')<<8)|(0x40+code),b,True);row['axes'][str(code)]=list(struct.unpack('<6i',b))
                except OSError:pass
            result.append(row)
        finally:os.close(fd)
    return result
if __name__=='__main__':
    out={'inputs':inputs()}
    try:out['drm']=drm_probe()
    except Exception as e:out['drm_error']=repr(e)
    print(json.dumps(out,indent=2))
