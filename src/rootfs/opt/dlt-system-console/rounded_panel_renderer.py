"""No system operations: antialiased, two-column panel compositor, stdlib only."""
import base64,hashlib,json,pathlib,zlib

BUTTONS=[(40,136,488,216),(552,136,488,216),(40,372,488,216),(552,372,488,216)]

def hit_test(x,y):
    # Rounded visual and rounded hit area match; no action outside the safe region.
    if not (24<=x<1056 and 24<=y<624):return None
    for n,(bx,by,w,h) in enumerate(BUTTONS):
        if not (bx<=x<bx+w and by<=y<by+h):continue
        xx,yy=x-bx,y-by;r=40
        qx=max(r,min(xx,w-r));qy=max(r,min(yy,h-r))
        if (xx-qx)**2+(yy-qy)**2<=r*r:return n
    return None

class RoundedPanel:
    def __init__(self,asset_path=None):
        p=pathlib.Path(asset_path) if asset_path else pathlib.Path(__file__).with_name('rounded-panel-assets.json')
        self.manifest=json.loads(p.read_text(encoding='utf-8'));self.assets={}
        if self.manifest['buttons']!=[list(b) for b in BUTTONS]:raise ValueError('asset/hit area mismatch')
        for k,v in self.manifest['assets'].items():
            raw=zlib.decompress(base64.b64decode(v['bgrx_z']))
            if len(raw)!=v['width']*v['height']*4 or hashlib.sha256(raw).hexdigest()!=v['sha256']:
                raise ValueError('asset integrity failed: '+k)
            self.assets[k]=(v['width'],v['height'],raw)
    def blit(self,mem,pitch,name,x,y):
        w,h,raw=self.assets[name]
        if x<0 or y<0 or x+w>1080 or y+h>666:raise ValueError('asset outside top panel')
        for row in range(h):
            start=(y+row)*pitch+x*4;mem[start:start+w*4]=raw[row*w*4:(row+1)*w*4]
        return w
    def digits(self,mem,pitch,value,x,y):
        for c in value:
            x+=self.blit(mem,pitch,'char-'+c,x,y)
        return x
    def draw(self,mem,pitch,state,touch_ready=False):
        self.blit(mem,pitch,'base-ready' if touch_ready else 'base-waiting',0,0)
        for n,(x,y,w,h) in enumerate(BUTTONS):
            self.blit(mem,pitch,f'button-{n}-'+('active' if state['hits'][n] else 'idle'),x,y)
        x=40;y=596
        if state.get('last_touch'):
            x+=self.blit(mem,pitch,'xy-prefix',x,y)
            self.digits(mem,pitch,'/'.join(str(max(0,min(int(v),9999))) for v in state['last_touch']['native']),x,y)
        else:self.blit(mem,pitch,'xy-wait',x,y)
        x=435;x+=self.blit(mem,pitch,'hits-prefix',x,y)
        self.digits(mem,pitch,str(min(sum(state['hits']),99999)),x,y)
