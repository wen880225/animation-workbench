"""Fixed-canvas paired transition reconstruction, shared motion for RGBA."""
from pathlib import Path
import json
import numpy as np
from PIL import Image

def associated(a):
    x=np.asarray(a,dtype=np.float32)/255
    x[:,:,:3]*=x[:,:,3:]
    return x

def straight(x):
    x=x.copy()
    x[:,:,:3]=np.divide(x[:,:,:3],x[:,:,3:],out=np.zeros_like(x[:,:,:3]),where=x[:,:,3:]>1e-7)
    a=np.uint8(np.clip(np.rint(x*255),0,255));a[a[:,:,3]==0,:3]=0
    return a

def protect_edges(output,a,b,t):
    """Recover introduced border holes from observed endpoint pixels.

    Only protect foreground observed in BOTH frames in a narrow canvas band.
    Never fill ordinary transparent background or invent an unseen body part.
    """
    x=associated(output);pa,pb=associated(a),associated(b)
    h,w=x.shape[:2];yy,xx=np.mgrid[:h,:w]
    d=np.minimum.reduce([xx,w-1-xx,yy,h-1-yy])
    weight=np.clip(1-d/max(2,min(16,min(w,h)//16)),0,1)[...,None]
    coverage=np.minimum(pa[:,:,3:],pb[:,:,3:])
    missing=np.maximum(0,coverage-x[:,:,3:])*weight
    donor=(1-t)*pa+t*pb
    rgb=np.divide(donor[:,:,:3],donor[:,:,3:],out=np.zeros_like(donor[:,:,:3]),where=donor[:,:,3:]>1e-7)
    x[:,:,:3]+=rgb*missing;x[:,:,3:]+=missing
    return straight(x)

class Rife:
    def __init__(self,weights):
        import hashlib
        import torch
        from rife_network import IFNet
        self.torch=torch
        if not torch.cuda.is_available():raise ValueError('本地接縫模型需要可用的 NVIDIA GPU；目前推論環境未偵測到 CUDA')
        if hashlib.sha256(Path(weights).read_bytes()).hexdigest()!=WEIGHTS_SHA256:
            raise ValueError('RIFE 模型權重不符，請重新安裝本地接縫模型')
        torch.set_num_threads(4)
        self.net=IFNet().eval().cuda()
        state=torch.load(weights,map_location='cpu',weights_only=True)
        state={k.removeprefix('module.'):v for k,v in state.items()}
        state={k:v for k,v in state.items() if not k.startswith(('teacher.','caltime.'))}
        self.net.load_state_dict(state,strict=True)

    def prepare(self,a,b):
        import torch.nn.functional as F
        torch=self.torch;self.h,self.w=a.shape[:2]
        pad=(0,(-self.w)%64,0,(-self.h)%64)
        def tensor(x):return F.pad(torch.from_numpy(x.copy()).permute(2,0,1)[None].cuda(),pad,mode='replicate')
        self.pa,self.pb=tensor(associated(a)),tensor(associated(b))
        def rgb(p):return (p[:,:3]+.5*(1-p[:,3:]))[:,[2,1,0]]
        with torch.inference_mode():
            flows,logits,_=self.net(torch.cat([rgb(self.pa),rgb(self.pb)],1),.5,[16,8,4,2,1])
            self.flow=flows[-1];self.mask=torch.sigmoid(logits).clamp(.05,.95)

    def sample(self,a,b,t):
        from rife_warp import warp
        if t<=0:return a.copy()
        if t>=1:return b.copy()
        with self.torch.inference_mode():
            m=(1-t)*self.mask/((1-t)*self.mask+t*(1-self.mask))
            p=warp(self.pa,self.flow[:,:2]*(2*t))*m+warp(self.pb,self.flow[:,2:4]*(2*(1-t)))*(1-m)
            out=straight(p[0,:,:self.h,:self.w].permute(1,2,0).cpu().numpy())
        return protect_edges(out,a,b,t)

# Filled from the independently downloaded official 4.25 archive at packaging.
WEIGHTS_SHA256='6615790efd627772917205db291f51cd392528a157ecbb2ecaeec3bff8eb6de2'

def run(manifest,backend='rife'):
    model=Rife(manifest['weights']) if backend=='rife' else None
    for job in manifest['pairs']:
        def read(path):
            with Image.open(path) as im:return np.array(im.convert('RGBA'))
        a,b=read(job['left']),read(job['right'])
        if a.shape!=b.shape:raise ValueError('接縫兩側解析度不同')
        if model:model.prepare(a,b)
        anchor=None
        for item in job['outputs']:
            t=item['t']
            if item.get('reuse_anchor'):
                if anchor is None:raise ValueError('接點基準尚未產生')
                out=anchor.copy()
            elif model:out=model.sample(a,b,t)
            else:
                import loop_closure
                out=np.array(loop_closure.morph(Image.fromarray(a),Image.fromarray(b),t))
                if 0<t<1:out=protect_edges(out,a,b,t)
            if item.get('anchor'):anchor=out.copy()
            Image.fromarray(out).save(item['path'])
        Path(manifest['progress']).write_text(json.dumps({'done':job['ordinal'],'total':len(manifest['pairs'])}),encoding='utf-8')

if __name__=='__main__':
    import sys
    run(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
