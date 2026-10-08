"""Non-destructive, fixed-canvas loop correction. Sources are never overwritten."""
import base64, io, math, json, shutil, subprocess, threading, time, hashlib, uuid
from pathlib import Path
import numpy as np
from PIL import Image
import engine as e
import seam_alignment as sa
import loop_closure as lc
from export_progress import sample,read_frame

LOCK = threading.RLock()
RUNS = {}

AFFINE_DEFAULTS=dict(dx=0,dy=0,sx=100,sy=100,angle=0,cx=50,cy=50)

def region_affine(value):
    """Validate the independent outside correction of a split region."""
    if not isinstance(value,dict):raise ValueError('框外變形必須是物件')
    out={}
    for key,default in AFFINE_DEFAULTS.items():
        raw=value.get(key,default)
        if isinstance(raw,bool):raise ValueError('框外變形數值無效')
        try:out[key]=float(raw)
        except (TypeError,ValueError,OverflowError):raise ValueError('框外變形數值無效') from None
        if not math.isfinite(out[key]):raise ValueError('框外變形數值必須為有限數字')
    if not 80<=out['sx']<=120 or not 80<=out['sy']<=120:raise ValueError('框外縮放範圍為 80–120%')
    if abs(out['dx'])>100 or abs(out['dy'])>100 or abs(out['angle'])>10:raise ValueError('框外位移限制 ±100 px，旋轉 ±10 度')
    if not 0<=out['cx']<=100 or not 0<=out['cy']<=100:raise ValueError('框外變形中心必須在畫布內')
    return out

def _identity_affine(value):
    return value['dx']==value['dy']==value['angle']==0 and value['sx']==value['sy']==100

def region(value):
    """Validate an explicit region; absence remains distinct for legacy recipes."""
    if not isinstance(value,dict): raise ValueError('變形範圍必須是物件')
    mode=value.get('mode','all');shape=value.get('shape','rect')
    if mode not in ('all','inside','outside','split','edge') or shape not in ('rect','ellipse'): raise ValueError('變形範圍模式或形狀無效')
    out=dict(mode=mode,shape=shape)
    for key,default in dict(x=25,y=25,w=50,h=50,feather=10).items():
        raw=value.get(key,default)
        if isinstance(raw,bool): raise ValueError('變形範圍數值無效')
        try:out[key]=float(raw)
        except (TypeError,ValueError,OverflowError):raise ValueError('變形範圍數值無效') from None
        if not math.isfinite(out[key]):raise ValueError('變形範圍數值必須為有限數字')
    if not 0<=out['x']<=100 or not 0<=out['y']<=100 or not 0<out['w']<=100 or not 0<out['h']<=100:raise ValueError('變形範圍必須在畫布內且寬高大於零')
    if out['x']+out['w']>100+1e-7 or out['y']+out['h']>100+1e-7:raise ValueError('變形範圍超出畫布')
    if not 0<=out['feather']<=50:raise ValueError('羽化必須介於 0–50%')
    out['w']=min(out['w'],100-out['x']);out['h']=min(out['h'],100-out['y'])
    if out['w']<=0 or out['h']<=0:raise ValueError('變形範圍寬高必須大於零')
    # Preserve temporarily inactive outside controls through save/load and mode
    # changes; only split rendering consumes them.
    if mode=='split' or 'outside' in value:out['outside']=region_affine(value.get('outside',{}))
    # Retain inactive edge controls without changing pre-edge recipe defaults.
    if mode=='edge' or any(key in value for key in ('edges','edge_left','edge_right')):
        out['edges']=value.get('edges','both')
        if out['edges'] not in ('left','right','both'):raise ValueError('邊緣鎖定方向必須為左側、右側或兩側')
        for key in ('edge_left','edge_right'):
            raw=value.get(key,12)
            if isinstance(raw,bool):raise ValueError('邊緣過渡寬度數值無效')
            try:out[key]=float(raw)
            except (TypeError,ValueError,OverflowError):raise ValueError('邊緣過渡寬度數值無效') from None
            if not math.isfinite(out[key]) or not 1<=out[key]<=45:raise ValueError('邊緣過渡寬度必須介於 1–45%')
    return out

def region_influence(value,xx,yy,w,h):
    """Destination-space mask. Pixel centers use the canvas's continuous bounds."""
    if value['mode']=='all':return np.ones(np.broadcast_shapes(xx.shape,yy.shape),dtype=np.float32)
    if value['mode']=='edge':
        influence=np.ones(np.broadcast_shapes(xx.shape,yy.shape),dtype=np.float32)
        if w<=1:return influence*0
        for side,distance in (('left',xx),('right',w-1-xx)):
            if value['edges'] not in (side,'both'):continue
            # Clamp to zero beyond a locked edge as well, so clipping tests use
            # the same anchored field as rendering rather than a global affine.
            position=np.clip(distance/(value['edge_'+side]*(w-1)/100),0,1)
            influence*=position*position*(3-2*position)
        return influence
    left=value['x']*w/100;top=value['y']*h/100
    rw=value['w']*w/100;rh=value['h']*h/100
    dx=xx+.5-(left+rw/2);dy=yy+.5-(top+rh/2)
    if value['shape']=='rect':
        qx=np.abs(dx)-rw/2;qy=np.abs(dy)-rh/2
        # Positive inside, negative outside; rect corners feather radially.
        distance=-(np.hypot(np.maximum(qx,0),np.maximum(qy,0))+np.minimum(np.maximum(qx,qy),0))
    else:
        radius=np.hypot(dx,dy);normalized=np.hypot(dx/(rw/2),dy/(rh/2))
        distance=np.divide(radius,normalized,out=np.full_like(normalized,min(rw,rh)/2),where=normalized>0)-radius
    feather=min(rw,rh)*value['feather']/100
    inside=value['mode'] in ('inside','split')
    if feather==0:
        return ((distance>=0) if inside else (distance<0)).astype(np.float32)
    signed=distance if inside else -distance
    influence=np.clip(signed/feather,0,1)
    return influence*influence*(3-2*influence)

def _edge_guard(value,w,h,cx,cy,c,s,sx,sy,dx,dy):
    """Reject continuous folds, including those between adjacent pixel centers.

    In each transition, x is normalized to [0,1] and the inverse displacement
    is polynomial. Its Jacobian and horizontal derivative are affine in y, so
    their minima occur on the top or bottom edge. Check polynomial extrema at
    both heights, rather than missing a narrow fold with a sampled pixel grid.
    """
    if w<=1:return
    Polynomial=np.polynomial.Polynomial
    influence=Polynomial([0,0,3,-2])
    inverse=np.array([[c/sx,s/sx],[-s/sy,c/sy]],dtype=np.float64)
    def minimum(poly):
        points=[0.,1.]
        for root in poly.deriv().roots():
            if abs(root.imag)<1e-8 and 0<root.real<1:points.append(float(root.real))
        return float(np.min(poly(points)))
    for side in ('left','right'):
        if value['edges'] not in (side,'both'):continue
        width=value['edge_'+side]*(w-1)/100
        direction=1 if side=='left' else -1
        x=Polynomial([0 if side=='left' else w-1,direction*width])
        derivative=influence.deriv()/(direction*width)
        for y in (0,h-1):
            displacement_u=inverse[0,0]*(x-cx-dx)+inverse[0,1]*(y-cy-dy)+cx-x
            displacement_v=inverse[1,0]*(x-cx-dx)+inverse[1,1]*(y-cy-dy)+cy-y
            du_dx=1+influence*(inverse[0,0]-1)+derivative*displacement_u
            du_dy=influence*inverse[0,1]
            dv_dx=influence*inverse[1,0]+derivative*displacement_v
            dv_dy=1+influence*(inverse[1,1]-1)
            determinant=du_dx*dv_dy-du_dy*dv_dx
            if minimum(du_dx)<=1e-3 or minimum(determinant)<=1e-3:
                label='左側' if side=='left' else '右側'
                raise ValueError(f'{label}邊緣鎖定的過渡區太窄，可能造成影像折返或過度擠壓。請加寬{label}過渡寬度，或減少寬度修正、位移或旋轉。')

def recipe(j, value):
    n=len(j['frames']); out={}
    defaults=dict(start=1,end=n,ramp=max(1,n-11),dx=0,dy=0,sx=100,sy=100,angle=0,cx=50,cy=50,protect=False,px=50,py=35,radius=15,tone=0,contrast=0)
    for k,v in defaults.items():
        out[k]=bool(value.get(k,v)) if k=='protect' else float(value.get(k,v))
        if k!='protect' and not math.isfinite(out[k]): raise ValueError('修整數值必須為有限數字')
    for k in ('start','end','ramp'): out[k]=int(out[k])
    if not 1<=out['start']<out['end']<=n: raise ValueError('循環範圍至少需要兩幀，且不能超出素材')
    if not out['start']<=out['ramp']<out['end']: raise ValueError('漸進起點必須在循環範圍內，並早於結束幀')
    if not all(j['frames'][i]['status']=='done' for i in range(out['start']-1,out['end'])): raise ValueError('所選幀尚未完成去背')
    if not 80<=out['sx']<=120 or not 80<=out['sy']<=120: raise ValueError('第一版縮放範圍為 80–120%')
    if abs(out['dx'])>100 or abs(out['dy'])>100 or abs(out['angle'])>10: raise ValueError('位移限制 ±100 px，旋轉 ±10 度')
    if any(not 0<=out[k]<=100 for k in ('cx','cy','px','py')) or not 1<=out['radius']<=60: raise ValueError('中心或保護範圍無效')
    if not -100<=out["tone"]<=100 or not -50<=out["contrast"]<=50: raise ValueError("明暗或對比超出範圍")
    if 'region' in value:out['region']=region(value['region'])
    if 'alignment' in value:out['alignment']=sa.validate_alignment(value['alignment'])
    if 'closure' in value:out['closure']=lc.validate(value['closure'],out['start'],out['end'],j['dimensions'])
    return out

def source(j, index):
    if not 1<=index<=len(j['frames']): raise ValueError('幀編號超出範圍')
    p=e.directory(j,'png')/j['frames'][index-1]['file']
    with Image.open(p) as im:
        im=im.convert('RGBA')
        if im.size!=tuple(j['dimensions']): raise ValueError('PNG 尺寸與任務畫布不一致')
        return im.copy()

def alignment_binding(j,index,side):
    if isinstance(index,bool) or not isinstance(index,(int,float)) or not math.isfinite(index) or int(index)!=index or not 1<=index<=len(j['frames']):raise ValueError('對位影格編號無效')
    index=int(index)
    files=[e.directory(j,'png')/f['file'] for f in j['frames']]
    return dict(job_id=j['id'],version='original',clip_key='',side=side,frame=index,
                fingerprint=sa.sequence_fingerprint(j['id'],'original',files,j['dimensions']),
                appearance_hash=sa.digest(dict(raw=True,frame=index)))

def validate_alignment_source(j,r):
    alignment=r.get('alignment')
    if not sa.active(alignment):return
    model=alignment['model'];provenance=model['provenance']
    if provenance['scope']!='loop' or model['dimensions']!=list(j['dimensions']):raise ValueError('對位來源或畫布已變更，請重新計算')
    sa.verify_binding(provenance['source'],alignment_binding(j,r['end'],'tail'))
    reference=provenance['reference']
    sa.verify_binding(reference,alignment_binding(j,reference['frame'],'reference'))

def weight(r,index):
    t=max(0,min(1,(index-r['ramp'])/(r['end']-r['ramp'])))
    return t*t*(3-2*t)

def transform(im,r,index):
    """Inverse warp with smoothly anchored region and premultiplied-alpha sampling."""
    t=weight(r,index)
    if sa.active(r.get('alignment')):return sa.apply(im,r['alignment'],t)
    local_region=r.get('region')
    split=local_region is not None and local_region['mode']=='split'
    outside=local_region['outside'] if split else None
    if not t or (_identity_affine(r) and (not split or _identity_affine(outside))): return im.copy(),False
    a=np.asarray(im,dtype=np.float32);h,w=a.shape[:2]
    yy,xx=np.mgrid[:h,:w].astype(np.float32)
    cx=r['cx']*(w-1)/100;cy=r['cy']*(h-1)/100
    angle=math.radians(r['angle']*t);c=math.cos(angle);s=math.sin(angle)
    sx=1+(r['sx']/100-1)*t;sy=1+(r['sy']/100-1)*t
    if local_region is not None and local_region['mode']=='edge':
        _edge_guard(local_region,w,h,cx,cy,c,s,sx,sy,r['dx']*t,r['dy']*t)
    x=xx-cx-r['dx']*t;y=yy-cy-r['dy']*t
    u=(c*x+s*y)/sx+cx;v=(-s*x+c*y)/sy+cy
    influence=None
    if split:
        influence=region_influence(local_region,xx,yy,w,h)
        # Blend inverse coordinates, then sample exactly once. Crossfading two
        # transformed images would create double edges around the feather.
        ocx=outside['cx']*(w-1)/100;ocy=outside['cy']*(h-1)/100
        oa=math.radians(outside['angle']*t);oc=math.cos(oa);os=math.sin(oa)
        osx=1+(outside['sx']/100-1)*t;osy=1+(outside['sy']/100-1)*t
        ox=xx-ocx-outside['dx']*t;oy=yy-ocy-outside['dy']*t
        ou=(oc*ox+os*oy)/osx+ocx;ov=(-os*ox+oc*oy)/osy+ocy
        same=all(r[key]==outside[key] for key in AFFINE_DEFAULTS)
        if not same:
            u=ou+(u-ou)*influence;v=ov+(v-ov)*influence
    elif local_region is not None and local_region['mode']!='all':
        influence=region_influence(local_region,xx,yy,w,h)
        masked_u=xx+(u-xx)*influence;masked_v=yy+(v-yy)*influence
        if local_region['mode']=='edge':
            # Preserve the global sampler's exact coordinates in the full-strength
            # center, avoiding a needless floating-point subtract/add roundtrip.
            u=np.where(influence==1,u,masked_u);v=np.where(influence==1,v,masked_v)
        else:u=masked_u;v=masked_v
    elif local_region is None and r['protect']:
        radius=min(w,h)*r['radius']/100
        distance=np.hypot(xx-r['px']*(w-1)/100,yy-r['py']*(h-1)/100)
        influence=np.clip((distance-radius)/max(radius*.5,1),0,1)
        influence=influence*influence*(3-2*influence)
        u=xx+(u-xx)*influence;v=yy+(v-yy)*influence
    # Alpha-associated RGB prevents white/dark fringes from hidden RGB.
    a[:,:,:3]*=a[:,:,3:4]/255
    x0=np.floor(u).astype(int);y0=np.floor(v).astype(int);fx=u-x0;fy=v-y0
    out=np.zeros_like(a)
    for ox,oy,coeff in [(0,0,(1-fx)*(1-fy)),(1,0,fx*(1-fy)),(0,1,(1-fx)*fy),(1,1,fx*fy)]:
        ix=x0+ox;iy=y0+oy;valid=(ix>=0)&(ix<w)&(iy>=0)&(iy<h)
        out+=a[np.clip(iy,0,h-1),np.clip(ix,0,w-1)]*(coeff*valid)[:,:,None]
    alpha=out[:,:,3:4];out[:,:,:3]=np.divide(out[:,:,:3]*255,alpha,out=np.zeros_like(out[:,:,:3]),where=alpha>0)
    out=np.uint8(np.clip(np.rint(out),0,255));out[out[:,:,3]==0,:3]=0
    # Preserve every channel, including invisible RGB, wherever the explicit mask is zero.
    # Do not retrofit this into legacy protection: existing output must remain unchanged.
    if split:
        unchanged=np.zeros((h,w),dtype=bool)
        if _identity_affine(r):unchanged|=influence==1
        if _identity_affine(outside):unchanged|=influence==0
        out[unchanged]=np.asarray(im)[unchanged]
    elif influence is not None and local_region is not None:out[influence==0]=np.asarray(im)[influence==0]
    if split:
        # The inside mask and its inward feather are entirely within the canvas.
        # Every outside-canvas destination therefore uses only the outside affine.
        # Sample those destinations instead of warning about an irrelevant global
        # extrapolation of the inside correction.
        clipped=False if _identity_affine(outside) else _region_clipped(np.asarray(im)[:,:,3],dict(mode='all'),ocx,ocy,oc,os,osx,osy,outside['dx']*t,outside['dy']*t)
    elif local_region is not None and local_region['mode']=='inside':
        # This warp is restricted to an in-canvas destination domain, including its feather.
        clipped=False
    elif local_region is not None and local_region['mode'] in ('outside','edge'):
        clipped=_region_clipped(np.asarray(im)[:,:,3],local_region,cx,cy,c,s,sx,sy,r['dx']*t,r['dy']*t)
    else:
        clipped=_affine_clipped(im,cx,cy,c,s,sx,sy,r['dx']*t,r['dy']*t)
    return Image.fromarray(out),clipped

def _affine_clipped(im,cx,cy,c,s,sx,sy,dx,dy):
    # Keep the legacy global/protection warning behavior unchanged.
    w,h=im.size
    ys,xs=np.where(np.asarray(im)[:,:,3]>2)
    clipped=False
    if len(xs):
        tx=c*sx*(xs-cx)-s*sy*(ys-cy)+cx+dx
        ty=s*sx*(xs-cx)+c*sy*(ys-cy)+cy+dy
        clipped=bool(np.any((tx<0)|(tx>w-1)|(ty<0)|(ty>h-1)))
    return clipped

def _region_clipped(alpha,value,cx,cy,c,s,sx,sy,dx,dy):
    """Check the actual masked inverse warp outside the canvas, not a global surrogate.

    A positive lower bound on the symmetric inverse matrix bounds every feather
    strength between identity and affine. Scan only the outer bands in small chunks.
    """
    h,w=alpha.shape
    if not np.any(alpha>2):return False
    inverse=np.array([[c/sx,s/sx],[-s/sy,c/sy]])
    lower=min(1,float(np.linalg.eigvalsh((inverse+inverse.T)/2).min()))
    if lower<=0:raise ValueError('變形超出支援的穩定範圍')
    corner=max(math.hypot(x-cx,y-cy) for x in (-1,w) for y in (-1,h))
    radius=math.ceil((corner+np.linalg.norm(inverse@np.array([dx,dy])))/lower)+2
    xlo=math.floor(cx-radius);xhi=math.ceil(cx+radius)+1
    ylo=math.floor(cy-radius);yhi=math.ceil(cy+radius)+1
    bands=[(xlo,xhi,ylo,0),(xlo,xhi,h,yhi),(xlo,0,0,h),(w,xhi,0,h)]
    for xstart,xend,ystart,yend in bands:
        for row in range(ystart,yend,32):
            yy,xx=np.mgrid[row:min(row+32,yend),xstart:xend].astype(np.float32)
            influence=region_influence(value,xx,yy,w,h)
            x=xx-cx-dx;y=yy-cy-dy
            u=xx+((c*x+s*y)/sx+cx-xx)*influence
            v=yy+((-s*x+c*y)/sy+cy-yy)*influence
            possible=(influence>0)&(u>-1)&(u<w)&(v>-1)&(v<h)
            if not possible.any():continue
            u=u[possible];v=v[possible]
            x0=np.floor(u).astype(int);y0=np.floor(v).astype(int);fx=u-x0;fy=v-y0
            sampled=np.zeros_like(u)
            for ox,oy,coeff in [(0,0,(1-fx)*(1-fy)),(1,0,fx*(1-fy)),(0,1,(1-fx)*fy),(1,1,fx*fy)]:
                ix=x0+ox;iy=y0+oy;valid=(ix>=0)&(ix<w)&(iy>=0)&(iy<h)
                sampled+=alpha[np.clip(iy,0,h-1),np.clip(ix,0,w-1)]*coeff*valid
            if np.any(sampled>2):return True
    return False

def adjust_tone(im,r):
    """Pointwise curve on straight RGB; alpha and canvas remain bit-exact."""
    tone=r.get('tone',0);contrast=r.get('contrast',0)
    if tone==0 and contrast==0: return im.copy()
    x=np.arange(256,dtype=np.float64)/255
    x=x**(2**(-tone/100))
    power=2**(contrast/50)
    a=x**power;b=(1-x)**power
    lut=np.uint8(np.clip(np.rint(255*a/(a+b)),0,255))
    pixels=np.array(im.convert('RGBA'))
    pixels[:,:,:3]=lut[pixels[:,:,:3]]
    return Image.fromarray(pixels)

def render(im,r,index):
    return transform(adjust_tone(im,r),r,index)

def render_frame(j,r,index):
    """The same image path serves the public preview and saved PNG sequence."""
    amount=lc.weight(r,index)
    if amount>=1:
        return adjust_tone(source(j,r['closure']['reference']),r),False
    im,clipped=render(source(j,index),r,index)
    if amount>0:
        reference=adjust_tone(source(j,r['closure']['reference']),r)
        im=lc.morph(im,reference,amount)
    return im,clipped

def closure_fingerprint(j,r):
    if not lc.active(r.get('closure')):return None
    files=[e.directory(j,'png')/j['frames'][idx-1]['file'] for idx in range(r['start'],r['end']+1)]
    return sa.sequence_fingerprint(j['id'],'original',files,j['dimensions'])

def validate_closure_source(j,r,expected):
    if closure_fingerprint(j,r)!=expected:
        raise ValueError('首尾共同基準的來源 PNG 在輸出期間已變更，請重新載入後再輸出')

def png64(im):
    buf=io.BytesIO();im.save(buf,format='PNG');return 'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()

def feature(j,index):
    im=source(j,index);im.thumbnail((128,192));a=np.asarray(im,dtype=np.float32)/255
    alpha=a[:,:,3:4];return np.concatenate((a[:,:,:3]*alpha,alpha),axis=2)

def candidates(j,window):
    n=len(j['frames'])
    if n<4: raise ValueError('接點搜尋至少需要四幀；較短素材請直接指定首尾幀')
    window=max(2,min(int(window),30,n//2))
    indices=set(range(1,window+2))|set(range(max(1,n-window),n+1))
    fs={i:feature(j,i) for i in indices};rank=[]
    for start in range(1,window+1):
        for end in range(max(start+2,n-window+1),n+1):
            jump=fs[start]-fs[end]
            appearance=float(np.mean(abs(jump)))
            velocity=float((np.mean(abs(jump-(fs[end]-fs[end-1])))+np.mean(abs((fs[start+1]-fs[start])-jump)))/2)
            rank.append(dict(start=start,end=end,score=round((appearance+.35*velocity)*100,3),appearance=round(appearance*100,3)))
    return sorted(rank,key=lambda x:x['score'])[:6]

def folder(j):
    p=e.jobdir(j['id'])/'loop_edits';p.mkdir(exist_ok=True);return p

def version_revision(path):
    """Browser cache identity must change when a deleted version name is reused."""
    stamps=[]
    for name in ('status.json','recipe.json','loop_transparent.webm'):
        try:
            stat=(path/name).stat();stamps.append((name,stat.st_mtime_ns,stat.st_size))
        except OSError:stamps.append((name,None,None))
    return hashlib.sha256(json.dumps(stamps).encode()).hexdigest()[:24]

def run_ffmpeg(args,log,jid=None,total=0,stage="",after=""):
    started=time.time();progress=log.with_suffix(".progress")
    if jid:progress.write_text("")
    with log.open('ab') as f:
        cmd=[e.tool('ffmpeg'),'-hide_banner','-loglevel','error','-y',*(['-progress',str(progress),'-nostats'] if jid else []),*map(str,args)]
        f.write((subprocess.list2cmdline(cmd)+'\n').encode());f.flush()
        p=subprocess.Popen(cmd,stdout=f,stderr=f,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        while p.poll() is None:
            if jid:
                with LOCK:RUNS[jid]['progress']=sample(stage,read_frame(progress),total,started,after)
            time.sleep(.3)
    if p.returncode: raise RuntimeError('FFmpeg 失敗，請查看修整版本的 ffmpeg.log')

def export_worker(j,r,out,source_fingerprint=None):
    jid=j['id']
    started=time.time()
    try:
        validate_alignment_source(j,r)
        if source_fingerprint is None:source_fingerprint=closure_fingerprint(j,r)
        validate_closure_source(j,r,source_fingerprint)
        dest=out/'transparent_png';dest.mkdir();count=r['end']-r['start']+1
        e.atomic_json(out/'recipe.json',r);clipped=[]
        for seq,idx in enumerate(range(r['start'],r['end']+1),1):
            im,clip=render_frame(j,r,idx)
            if clip: clipped.append(idx)
            target=dest/f'frame_{seq:08d}.png'
            if weight(r,idx)==0 and lc.weight(r,idx)==0 and r.get('tone',0)==0 and r.get('contrast',0)==0:
                shutil.copy2(e.directory(j,'png')/j['frames'][idx-1]['file'],target)
            else: im.save(target)
            with LOCK: RUNS[jid].update(phase=f'修整 PNG：{seq}/{count}',done=seq,progress=sample('修整 PNG',seq,count,started,'合成 WebM 與透明度驗證'))
        validate_alignment_source(j,r)
        validate_closure_source(j,r,source_fingerprint)
        if clipped: raise ValueError('修整後可能超出畫布，已保留候選 PNG 但未合成影片。請減少位移／縮放。')
        closure_report=lc.verify(dest,count,r['closure']) if lc.active(r.get('closure')) else None
        if closure_report and not closure_report['endpoints_equal']:
            raise ValueError('回讀首尾 PNG 不相同，已保留候選圖片；請重新輸出並回報問題')
        with LOCK: RUNS[jid]['phase']='合成透明 WebM 並驗證 Alpha'
        video=out/'loop_transparent.webm';fps=str(j['config']['fps']);log=out/'ffmpeg.log'
        run_ffmpeg(['-framerate',fps,'-i',dest/'frame_%08d.png','-frames:v',count,'-an','-c:v','libvpx-vp9','-pix_fmt','yuva420p','-lossless','1','-auto-alt-ref','0','-row-mt','1',video],log,jid,count,'合成 WebM','透明度驗證')
        raw=out/'verify_alpha.raw'
        run_ffmpeg(['-c:v','libvpx-vp9','-i',video,'-vf','alphaextract','-f','rawvideo','-pix_fmt','gray',raw],log,jid,count,'回讀透明度','逐幀核對')
        w,h=j['dimensions'];size=w*h;err=0
        if raw.stat().st_size!=size*count: raise ValueError('回讀幀數或解析度不符')
        verify_started=time.time()
        with raw.open('rb') as f:
            for seq in range(1,count+1):
                actual=np.frombuffer(f.read(size),np.uint8).astype(np.int16)
                with Image.open(dest/f'frame_{seq:08d}.png') as im: expected=np.asarray(im.getchannel('A')).reshape(-1).astype(np.int16)
                err=max(err,int(abs(actual-expected).max()))
                with LOCK:RUNS[jid]['progress']=sample('逐幀核對',seq,count,verify_started)
        raw.unlink()
        if err>2: raise ValueError('透明度回讀差異超過 2/255')
        report=dict(dimensions=[w,h],frames=count,fps=fps,alpha_max_error=err,source_range=[r['start'],r['end']],source_revision=j.get('media_revision',0))
        if closure_report:report.update(loop_closure=closure_report,source_fingerprint=source_fingerprint)
        validate_closure_source(j,r,source_fingerprint)
        e.atomic_json(out/'verification.json',report)
        with LOCK: RUNS[jid].update(state='complete',phase='修整版本已完成，解析度與透明度已驗證',report=report,elapsed=time.time()-started)
    except Exception as ex:
        with LOCK: RUNS[jid].update(state='error',phase=str(ex))
    finally:
        with LOCK: e.atomic_json(out/'status.json',RUNS[jid])

def handle(action,j,data):
    if j.get('state')=='running' and action not in ('load','status','open'):
        raise ValueError('此素材仍在處理中，請完成或暫停後再修整')
    if action=='load':
        root=folder(j);saved=root/'recipe.json'
        versions=[]
        for p in sorted(root.glob('修整*/status.json'),key=lambda p:p.stat().st_mtime,reverse=True):
            try:
                v=dict(json.loads(p.read_text(encoding='utf-8')),name=p.parent.name)
            except (OSError,ValueError,TypeError):
                v=dict(name=p.parent.name,state='error',phase='版本記錄無法讀取，素材仍保留在資料夾')
            for key,filename in [('recipe','recipe.json'),('report','verification.json')]:
                try:
                    value=json.loads((p.parent/filename).read_text(encoding='utf-8'))
                    if isinstance(value,dict):v[key]=value
                except (OSError,ValueError,TypeError):pass
            if v.get('state')=='running':
                with LOCK:live=RUNS.get(j['id'],{})
                if live.get('state')=='running' and live.get('version')==v['name']:v.update(live)
                else:v.update(state='error',phase='上次處理中斷，請重新匯出')
            v['revision']=version_revision(p.parent)
            v['source_freshness']=e.source_freshness(j,v.get('report',{}).get('source_revision',v.get('source_revision')),p.stat().st_mtime)
            versions.append(v)
        return dict(recipe=json.loads(saved.read_text(encoding='utf-8')) if saved.exists() else None,versions=versions)
    if action=='status':
        with LOCK:return dict(RUNS.get(j['id'],{}))
    if action=='search':return dict(candidates=candidates(j,data.get('window',12)))
    if action=='open':
        name=str(data.get('version',''))
        if not name.startswith('修整') or Path(name).name!=name or '/' in name or '\\' in name:raise ValueError('無效修整版本')
        target=folder(j)/name
        if not target.is_dir():raise ValueError('找不到修整版本')
        e.os.startfile(str(target));return dict(ok=True)
    r=recipe(j,data.get('recipe',{}))
    if action=='align':
        source_index=data.get('source_frame',r['end']);reference_index=data.get('reference_frame',r['start'])
        source_binding=alignment_binding(j,source_index,'tail')
        reference_binding=alignment_binding(j,reference_index,'reference')
        if source_binding['frame']!=r['end']:raise ValueError('局部對位的來源必須是目前尾幀，請重新選擇')
        if j['frames'][reference_binding['frame']-1]['status']!='done':raise ValueError('參考幀尚未完成去背')
        if source_binding['frame']==reference_binding['frame']:raise ValueError('請選擇不同的來源尾幀與參考幀')
        provenance=dict(scope='loop',source=source_binding,reference=reference_binding)
        alignment,report=sa.build_alignment(np.asarray(source(j,source_binding['frame'])),np.asarray(source(j,reference_binding['frame'])),provenance)
        validate_alignment_source(j,dict(r,alignment=alignment))
        return dict(alignment=alignment,report=report)
    if action in ('save','export') or (action=='preview' and not data.get('original')):
        validate_alignment_source(j,r)
    if action=='save':e.atomic_json(folder(j)/'recipe.json',r);return dict(ok=True)
    if action=='preview':
        idx=int(data.get('frame',r['end']));clipped=False
        if data.get('original'):im=source(j,idx)
        else:im,clipped=render_frame(j,r,idx)
        result=dict(image=png64(im),clipped=clipped,frame=idx,strength=weight(r,idx),
                    closure_strength=lc.weight(r,idx) if not data.get('original') else 0,
                    closure_reference=r.get('closure',{}).get('reference'))
        if lc.active(r.get('closure')):
            result['closure']=dict(enabled=True,reference=r['closure']['reference'],weight=result['closure_strength'])
        return result
    if action=='export':
        with LOCK:
            if e.ACTIVE or RUNS.get(j['id'],{}).get('state')=='running':raise ValueError('已有處理中的任務，請完成後再匯出')
            needed=(r['end']-r['start']+1)*j['dimensions'][0]*j['dimensions'][1]*8
            if shutil.disk_usage(folder(j)).free<needed+512*1024**2:raise ValueError('磁碟空間不足')
            source_fingerprint=closure_fingerprint(j,r)
            out=e.unique_folder(folder(j),'修整')
            RUNS[j['id']]=dict(state='running',phase='準備修整',version=out.name,generation=uuid.uuid4().hex,done=0,source_revision=j.get('media_revision',0))
            e.atomic_json(out/'status.json',RUNS[j['id']])
            threading.Thread(target=export_worker,args=(j,r,out,source_fingerprint),daemon=True).start()
            return dict(RUNS[j['id']])
    raise ValueError('未知循環修整操作')
