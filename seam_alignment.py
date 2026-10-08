"""Persistent sparse seam alignment, with a bounded reconstructed-map cache.

dx,dy always map destination/target pixel centers to source pixel centers.
No generative model, production imports, or changes to source assets are used.
"""
from pathlib import Path
import hashlib
import json
import numpy as np
import math
import re
import threading
from collections import OrderedDict
from PIL import Image

_CACHE=OrderedDict()
_LOCK=threading.RLock()
_MAX_BYTES=128*1024*1024
_HEX=re.compile(r'^[0-9a-f]{64}$')
_ID=re.compile(r'^[0-9a-f]{32}$')


def _dependencies():
    if 'cv2' in globals():return
    try:
        import cv2 as cv
        from scipy.interpolate import RBFInterpolator as RBF, PchipInterpolator as Pchip
    except (ImportError,OSError) as exc:
        raise ValueError('局部對位需要 OpenCV 與 SciPy，請重新執行安裝腳本（setup.cmd）安裝依賴；其他修整功能仍可使用') from exc
    globals().update(cv2=cv,RBFInterpolator=RBF,PchipInterpolator=Pchip)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _number(value,low,high,integer=False):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high or (integer and int(value)!=value):
        raise ValueError('局部對位模型包含無效數值，請重新計算')
    return int(value) if integer else float(value)


def _keys(value,expected):
    if not isinstance(value,dict) or set(value)!=set(expected):raise ValueError('局部對位模型格式不符，請重新計算')


def _binding(value):
    _keys(value,('job_id','version','clip_key','side','frame','fingerprint','appearance_hash'))
    if not isinstance(value['job_id'],str) or not _ID.fullmatch(value['job_id']):raise ValueError('對位素材 ID 無效')
    if not isinstance(value['clip_key'],str) or (value['clip_key'] and not _ID.fullmatch(value['clip_key'])):raise ValueError('對位段落 ID 無效')
    version=value['version']
    if not isinstance(version,str) or not 1<=len(version)<=100 or version in ('.','..') or any(c in version for c in '/\\:\x00'):raise ValueError('對位來源版本無效')
    if value['side'] not in ('head','tail','reference'):raise ValueError('對位端點無效')
    for key in ('fingerprint','appearance_hash'):
        if not isinstance(value[key],str) or not _HEX.fullmatch(value[key]):raise ValueError('對位來源指紋無效')
    return dict(value,frame=_number(value['frame'],1,1000000,True))


def validate_model(value):
    _keys(value,('schema','dimensions','controls','boundaries','regularization','scale','provenance'))
    if value['schema']!=1 or isinstance(value['schema'],bool):raise ValueError('不支援的對位模型版本，請重新計算')
    dims=value['dimensions']
    if not isinstance(dims,list) or len(dims)!=2:raise ValueError('對位模型畫布尺寸無效')
    w,h=[_number(x,48,4096,True) for x in dims]
    if w*h>8_000_000:raise ValueError('局部對位目前支援最多 800 萬像素')
    controls=value['controls']
    if not isinstance(controls,list) or not 8<=len(controls)<=40:raise ValueError('對位控制點數量無效')
    clean=[];seen=set()
    for control in controls:
        _keys(control,('source','target'))
        item={}
        for key in ('source','target'):
            pair=control[key]
            if not isinstance(pair,list) or len(pair)!=2:raise ValueError('對位控制點格式無效')
            item[key]=[_number(pair[0],0,w-1),_number(pair[1],0,h-1)]
        if math.dist(item['source'],item['target'])>100:raise ValueError('對位位移過大，請改用更接近的影格')
        p=tuple(item['target'])
        if p in seen:raise ValueError('對位控制點不能重複')
        seen.add(p);clean.append(item)
    boundaries=value['boundaries'];_keys(boundaries,('left','right'));ends={}
    for side in ('left','right'):
        knots=boundaries[side];_keys(knots,('source','target'));ends[side]={}
        for key in ('source','target'):
            values=knots[key]
            if not isinstance(values,list) or not 2<=len(values)<=18:raise ValueError('邊界對位節點數量無效')
            values=[_number(x,0,h-1) for x in values]
            if values[0]!=0 or values[-1]!=h-1 or any(b-a<.01 for a,b in zip(values,values[1:])):raise ValueError('邊界對位必須依序排列，不能折返')
            ends[side][key]=values
        if len(ends[side]['source'])!=len(ends[side]['target']):raise ValueError('邊界對位節點數量不符')
        if any(abs(a-b)>100 for a,b in zip(ends[side]['source'],ends[side]['target'])):raise ValueError('邊界對位位移過大')
    provenance=value['provenance'];_keys(provenance,('scope','source','reference'))
    if provenance['scope'] not in ('loop','transition'):raise ValueError('對位來源範圍無效')
    provenance=dict(scope=provenance['scope'],source=_binding(provenance['source']),reference=_binding(provenance['reference']))
    return dict(schema=1,dimensions=[w,h],controls=clean,boundaries=ends,regularization=_number(value['regularization'],.000001,.1),scale=_number(value['scale'],.1,1),provenance=provenance)


def validate_alignment(value):
    if not isinstance(value,dict) or any(k not in ('enabled','strength','model') for k in value):raise ValueError('局部對位設定格式無效')
    enabled=value.get('enabled',False)
    if not isinstance(enabled,bool):raise ValueError('局部對位開關必須為布林值')
    result=dict(enabled=enabled,strength=_number(value.get('strength',100),0,100))
    if 'model' in value:result['model']=validate_model(value['model'])
    elif enabled:raise ValueError('請先計算局部對位，再啟用修正')
    return result


def active(value):
    return isinstance(value,dict) and value.get('enabled') is True


def clear_cache():
    with _LOCK:_CACHE.clear()


def sequence_fingerprint(job_id,version,files,dimensions):
    stamps=[]
    try:
        for path in files:
            p=Path(path);s=p.stat()
            if not p.is_file():raise OSError('not a file')
            stamps.append([p.name,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_ino])
    except OSError as exc:raise ValueError('對位來源圖片已變更或遺失，請重新載入並計算對位') from exc
    return digest(dict(job_id=job_id,version=version,dimensions=list(dimensions),files=stamps))


def verify_binding(saved,current):
    if saved!=current:raise ValueError('對位的來源、首尾幀或參考畫面已變更，請重新計算局部對位，或先停用對位')


def gray(rgba):
    _dependencies()
    a=rgba[:,:,3:4].astype(np.float32)/255
    rgb=rgba[:,:,:3].astype(np.float32)*a+255*(1-a)
    return cv2.cvtColor(np.uint8(np.rint(rgb)),cv2.COLOR_RGB2GRAY)


def alpha_intervals(column,threshold=127.5):
    """Subpixel endpoints for nontrivial runs; endpoints clipped to pixel centers."""
    mask=column>threshold
    starts=np.flatnonzero(np.diff(np.r_[False,mask,False].astype(np.int8))==1)
    ends=np.flatnonzero(np.diff(np.r_[False,mask,False].astype(np.int8))==-1)-1
    intervals=[]
    for first,last in zip(starts,ends):
        if last-first<7:continue
        low=float(first);high=float(last)
        if first>0:
            low=first-1+(threshold-float(column[first-1]))/(float(column[first])-float(column[first-1]))
        if last<len(column)-1:
            high=last+(float(column[last])-threshold)/(float(column[last])-float(column[last+1]))
        intervals.append([float(low),float(high)])
    return intervals


def boundary_profile(source,target,x):
    _dependencies()
    h=source.shape[0]
    a=alpha_intervals(source[:,x,3]);b=alpha_intervals(target[:,x,3])
    source_y=np.array(a,dtype=float).ravel();target_y=np.array(b,dtype=float).ravel()
    usable=len(a)==len(b) and len(a)>0 and np.max(np.abs(source_y-target_y))<=50
    pairs=[]
    if usable:
        for sy,ty in zip(source_y,target_y):pairs.append(dict(target=[int(x),float(ty)],source=[int(x),float(sy)],kind='alpha_boundary'))
        order=(target_y>0)&(target_y<h-1)&(source_y>0)&(source_y<h-1)
        knots=np.r_[0,target_y[order],h-1];values=np.r_[0,source_y[order],h-1]
        if np.any(np.diff(knots)<=0) or np.any(np.diff(values)<=0):raise ValueError('Boundary correspondences are not monotone')
        mapped=PchipInterpolator(knots,values)(np.arange(h))
    else:mapped=np.arange(h,dtype=float)
    return mapped-np.arange(h),pairs,dict(source_intervals=a,target_intervals=b,matched=bool(usable))


def _patch_ncc(first,second,point_a,point_b):
    pa=cv2.getRectSubPix(first,(17,17),tuple(map(float,point_a))).astype(float).ravel()
    pb=cv2.getRectSubPix(second,(17,17),tuple(map(float,point_b))).astype(float).ravel()
    pa-=pa.mean();pb-=pb.mean()
    return float(np.dot(pa,pb)/max(np.linalg.norm(pa)*np.linalg.norm(pb),1e-9))


def sparse_correspondences(source,target,max_points=32):
    _dependencies()
    cv2.setNumThreads(1);cv2.setRNGSeed(7)
    sg=gray(source);tg=gray(target);h,w=tg.shape
    opaque=np.uint8(target[:,:,3]>240)*255
    mask=cv2.erode(opaque,np.ones((15,15),np.uint8))
    mask[:25]=0;mask[-25:]=0;mask[:,:36]=0;mask[:,-36:]=0
    points=cv2.goodFeaturesToTrack(tg,maxCorners=500,qualityLevel=.015,minDistance=12,mask=mask,blockSize=7)
    if points is None:raise ValueError('找不到穩定的內部線稿，請改用更接近的首尾幀重新計算對位')
    options=dict(winSize=(31,31),maxLevel=3,criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,40,.005))
    forward,ok1,_=cv2.calcOpticalFlowPyrLK(tg,sg,points,None,**options)
    backward,ok2,_=cv2.calcOpticalFlowPyrLK(sg,tg,forward,None,**options)
    candidates=[]
    for p,q,r,valid1,valid2 in zip(points[:,0],forward[:,0],backward[:,0],ok1[:,0],ok2[:,0]):
        if not valid1 or not valid2 or not np.all(np.isfinite(q)):continue
        fb=float(np.linalg.norm(p-r));distance=float(np.linalg.norm(q-p))
        if fb>.6 or distance>35 or not (10<=q[0]<w-10 and 10<=q[1]<h-10):continue
        if source[int(round(q[1])),int(round(q[0])),3]<240:continue
        ncc=_patch_ncc(tg,sg,p,q)
        if ncc<.86:continue
        candidates.append(dict(target=p.tolist(),source=q.tolist(),kind='line_patch',ncc=ncc,forward_backward_px=fb))
    # A spatially distributed small set avoids concentrating controls on the face.
    candidates.sort(key=lambda item:item['ncc']-.25*item['forward_backward_px'],reverse=True)
    picked=[];cells=set()
    for item in candidates:
        p=np.array(item['target']);cell=(int(p[0]//72),int(p[1]//90))
        if cell in cells or any(np.linalg.norm(p-np.array(q['target']))<40 for q in picked):continue
        picked.append(item);cells.add(cell)
        if len(picked)>=max_points:break
    if len(picked)<8:raise ValueError('可靠對應點不足，請改用更接近的首尾幀重新計算對位')
    return picked,dict(detected=int(len(points)),passed_confidence=int(len(candidates)),selected=int(len(picked)),max_points=max_points,ncc_min=.86,forward_backward_max_px=.6)


def resample_frame(rgba,dx,dy,strength=1.):
    """One bilinear sample in premultiplied alpha, with exact identity at zero."""
    if not 0<=strength<=1:raise ValueError('strength must be between 0 and 1')
    if strength==0:return rgba.copy()
    h,w=rgba.shape[:2]
    if dx.shape!=(h,w) or dy.shape!=(h,w):raise ValueError('map/image shape mismatch')
    yy,xx=np.mgrid[:h,:w].astype(np.float32)
    u=xx+dx*strength;v=yy+dy*strength
    if not np.isfinite(u).all() or not np.isfinite(v).all():raise ValueError('nonfinite map')
    a=rgba.astype(np.float32);a[:,:,:3]*=a[:,:,3:4]/255
    ix=np.floor(u).astype(int);iy=np.floor(v).astype(int);fx=u-ix;fy=v-iy
    out=np.zeros_like(a)
    for ox,oy,k in [(0,0,(1-fx)*(1-fy)),(1,0,fx*(1-fy)),(0,1,(1-fx)*fy),(1,1,fx*fy)]:
        x=ix+ox;y=iy+oy;valid=(x>=0)&(x<w)&(y>=0)&(y<h)
        out+=a[np.clip(y,0,h-1),np.clip(x,0,w-1)]*(k*valid)[:,:,None]
    alpha=out[:,:,3:4]
    out[:,:,:3]=np.divide(out[:,:,:3]*255,alpha,out=np.zeros_like(out[:,:,:3]),where=alpha>0)
    out=np.uint8(np.clip(np.rint(out),0,255));out[out[:,:,3]==0,:3]=0
    unchanged=(dx==0)&(dy==0);out[unchanged]=rgba[unchanged]
    return out


def jacobian_stats(dx,dy,strength=1.):
    vx,ux=np.gradient(dx.astype(float)*strength);vy,uy=np.gradient(dy.astype(float)*strength)
    determinant=(1+ux)*(1+vy)-vx*uy
    h,w=dx.shape;yy,xx=np.mgrid[:h,:w]
    mx=xx+strength*dx;my=yy+strength*dy
    return dict(min=float(determinant.min()),percentile_01=float(np.percentile(determinant,1)),max=float(determinant.max()),horizontal_derivative_min=float((1+ux).min()),vertical_derivative_min=float((1+vy).min()),nonpositive_pixels=int(np.count_nonzero(determinant<=0)),outside_source_pixels=int(np.count_nonzero((mx<-.0001)|(mx>w-1+.0001)|(my<-.0001)|(my>h-1+.0001))),locked_x_max_abs_px=float(np.max(np.abs(dx[:,[0,-1]]))))


def support_map_stats(source,target,dx,dy,strength=1.):
    """Soft adoption limits on visible pixels AND their inverse sample support.

    This does not replace maps()'s global fold/out-of-canvas guard. Transparent
    RBF extrapolation remains guarded, but is not labelled visible movement.
    """
    _dependencies()
    if not 0 < strength <= 1:
        raise ValueError('局部候選強度必須大於零且不超過 100%')
    a,b=np.asarray(source),np.asarray(target)
    if a.shape != b.shape or a.shape[:2] != dx.shape or dx.shape != dy.shape:
        raise ValueError('局部候選尺寸不符')
    h,w=dx.shape;yy,xx=np.mgrid[:h,:w]
    u=xx+dx*strength;v=yy+dy*strength
    sampled=cv2.remap(a[:,:,3],u.astype(np.float32),v.astype(np.float32),cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_CONSTANT,borderValue=0)
    # Union, never intersection: displaced/new fine appendages must count too.
    support=(a[:,:,3]>2)|(b[:,:,3]>2)|(sampled>2)
    sy,sx=np.nonzero(support)
    for ox,oy in ((0,0),(1,0),(0,1),(1,1)):
        qx=np.clip(np.floor(u[sy,sx]).astype(int)+ox,0,w-1)
        qy=np.clip(np.floor(v[sy,sx]).astype(int)+oy,0,h-1)
        support[qy,qx]=True
    support=cv2.dilate(np.uint8(support),np.ones((3,3),np.uint8)).astype(bool)
    vx,ux=np.gradient(dx.astype(float)*strength);vy,uy=np.gradient(dy.astype(float)*strength)
    linear=ux+vy;quadratic=ux*vy-vx*uy
    def strength_minimum(linear,quadratic):
        minimum=np.minimum(1.,1+linear+quadratic)
        vertex=np.divide(-linear,2*quadratic,out=np.zeros_like(linear),where=quadratic>0)
        use=(quadratic>0)&(vertex>0)&(vertex<1)
        minimum[use]=np.minimum(minimum[use],1+linear[use]*vertex[use]+quadratic[use]*vertex[use]**2)
        return minimum
    minimum=strength_minimum(linear,quadratic)
    movement=np.hypot(dx,dy)*strength
    if not support.any():
        raise ValueError('局部候選沒有可量測的有效角色區域')
    low=float(minimum[support].min());high=float((1+linear+quadratic)[support].max())
    # Preserve the existing cell guard, restricted only for these additional
    # adoption thresholds. Centered gradients alone can hide adjacent reversal.
    cells=support[:-1,:-1]|support[1:,:-1]|support[:-1,1:]|support[1:,1:]
    dxh=np.diff(dx.astype(float)*strength,axis=1);dxv=np.diff(dx.astype(float)*strength,axis=0)
    dyh=np.diff(dy.astype(float)*strength,axis=1);dyv=np.diff(dy.astype(float)*strength,axis=0)
    for p,q,r,s in ((dxh[:-1],dxv[:,:-1],dyh[:-1],dyv[:,:-1]),(dxh[1:],dxv[:,1:],dyh[1:],dyv[:,1:])):
        linear=p+s;quadratic=p*s-q*r
        low=min(low,float(strength_minimum(linear,quadratic)[cells].min()))
        high=max(high,float((1+linear+quadratic)[cells].max()))
    return dict(support_max_px=float(movement[support].max()),canvas_max_px=float(movement.max()),
                support_pixels=int(support.sum()),jacobian_min=low,jacobian_max=high)


def sampling_support_loss(source,candidate,dx,dy,strength):
    """Count source alpha support no longer reached by visible output samples.

    A geometric correction never borrows target RGB. This additional native
    coverage check detects sampling-away of narrow alpha features at any fade.
    """
    _dependencies()
    a,b=np.asarray(source),np.asarray(candidate)
    h,w=dx.shape;yy,xx=np.mgrid[:h,:w]
    u=xx+dx*strength;v=yy+dy*strength
    sy,sx=np.nonzero(b[:,:,3]>64)
    coverage=np.zeros((h,w),dtype=bool)
    x0=np.floor(u[sy,sx]).astype(int);y0=np.floor(v[sy,sx]).astype(int)
    fx=u[sy,sx]-x0;fy=v[sy,sx]-y0
    for ox,oy,weight in ((0,0,(1-fx)*(1-fy)),(1,0,fx*(1-fy)),(0,1,(1-fx)*fy),(1,1,fx*fy)):
        x=x0+ox;y=y0+oy
        valid=(weight>.05)&(x>=0)&(x<w)&(y>=0)&(y<h)
        coverage[y[valid],x[valid]]=True
    distance=cv2.distanceTransform(np.uint8(~coverage),cv2.DIST_L2,cv2.DIST_MASK_PRECISE)
    return int(np.count_nonzero((a[:,:,3]>64)&(distance>1.25)))


def _line_distance(a,b):
    if not a.any() or not b.any():return None
    da=cv2.distanceTransform(np.uint8(~a),cv2.DIST_L2,cv2.DIST_MASK_PRECISE)
    db=cv2.distanceTransform(np.uint8(~b),cv2.DIST_L2,cv2.DIST_MASK_PRECISE)
    return float((da[b].mean()+db[a].mean())/2)


def metrics(candidate,target):
    ma=candidate[:,:,3]>127;mb=target[:,:,3]>127
    union=ma|mb;intersection=ma&mb
    ea=ma&~cv2.erode(np.uint8(ma),np.ones((3,3),np.uint8)).astype(bool)
    eb=mb&~cv2.erode(np.uint8(mb),np.ones((3,3),np.uint8)).astype(bool)
    inside=cv2.erode(np.uint8((candidate[:,:,3]>240)&(target[:,:,3]>240)),np.ones((15,15),np.uint8)).astype(bool)
    ga=gray(candidate);gb=gray(target)
    la=(cv2.Canny(ga,60,140)>0)&inside;lb=(cv2.Canny(gb,60,140)>0)&inside
    width=candidate.shape[1];band=max(1,round(width*.12))
    return dict(silhouette_mismatch_pixels=int(np.count_nonzero(ma!=mb)),silhouette_iou=float(intersection.sum()/max(union.sum(),1)),silhouette_symmetric_distance_px=_line_distance(ea,eb),alpha_mae_0_255=float(np.abs(candidate[:,:,3].astype(float)-target[:,:,3]).mean()),left_band_mismatch_pixels=int(np.count_nonzero(ma[:,:band]!=mb[:,:band])),right_band_mismatch_pixels=int(np.count_nonzero(ma[:,-band:]!=mb[:,-band:])),linework_symmetric_distance_px=_line_distance(la,lb),linework_edge_pixels=[int(la.sum()),int(lb.sum())],interior_gray_mae_0_255=float(np.abs(ga.astype(float)-gb)[inside].mean()),interior_comparison_pixels=int(inside.sum()))


def _construct_map(model):
    _dependencies()
    w,h=model['dimensions'];yy,xx=np.mgrid[:h,:w].astype(float)
    profiles={}
    for side in ('left','right'):
        knots=model['boundaries'][side]
        profiles[side]=PchipInterpolator(knots['target'],knots['source'])(np.arange(h))-np.arange(h)
    def smooth(z):
        z=np.clip(z,0,1);return z*z*(3-2*z)
    # Keep the gate definitions versioned by schema: saved models need no NPZ.
    boundary_width=min(65.,(w-1)/3)
    x_width=min(35.,(w-1)/4);y_width=min(25.,(h-1)/4)
    l=1-smooth(xx/boundary_width);r=1-smooth((w-1-xx)/boundary_width);middle=1-l-r
    base=l*profiles['left'][:,None]+r*profiles['right'][:,None]
    xgate=smooth(xx/x_width)*smooth((w-1-xx)/x_width)
    ygate=smooth(yy/y_width)*smooth((h-1-yy)/y_width)
    points=[];values=[]
    for item in model['controls']:
        p=np.array(item['target']);q=np.array(item['source']);x,y=np.rint(p).astype(int)
        points.append(p);values.append([(q[0]-p[0])/max(xgate[y,x],.1),((q[1]-p[1])/max(ygate[y,x],.1)-base[y,x])/max(middle[y,x],.1)])
    for y in (0,h-1):
        for x in (0,(w-1)/2,w-1):points.append([x,y]);values.append([0,0])
    try:
        rbf=RBFInterpolator(np.array(points)/min(w,h),np.array(values),kernel='thin_plate_spline',smoothing=model['regularization'],degree=1)
        field=np.empty((h,w,2),dtype=float)
        for start in range(0,h,64):
            coords=np.stack([xx[start:start+64],yy[start:start+64]],axis=-1)
            field[start:start+64]=rbf(coords.reshape(-1,2)/min(w,h)).reshape(coords.shape)
    except (ValueError,np.linalg.LinAlgError) as exc:raise ValueError('對位控制點無法形成穩定變形，請重新計算') from exc
    dx=np.asarray(field[:,:,0]*xgate*ygate*model['scale'],dtype=np.float32)
    dy=np.asarray((base+middle*field[:,:,1])*ygate*model['scale'],dtype=np.float32)
    if not np.isfinite(dx).all() or not np.isfinite(dy).all():raise ValueError('局部對位產生無效位移，請重新計算')
    return dx,dy


def _guard(dx,dy):
    """Guard every strength in [0,1] on every sampled cell, not only endpoints.

    det(I+tD)=1+t*trace(D)+t^2*det(D). The vertex gives the exact
    strength minimum for the finite-difference spatial Jacobian.
    """
    vx,ux=np.gradient(dx.astype(float));vy,uy=np.gradient(dy.astype(float))
    linear=ux+vy;quadratic=ux*vy-vx*uy
    def strength_minimum(linear,quadratic):
        minimum=np.minimum(1.,1+linear+quadratic)
        vertex=np.divide(-linear,2*quadratic,out=np.zeros_like(linear),where=quadratic>0)
        use=(quadratic>0)&(vertex>0)&(vertex<1)
        minimum[use]=np.minimum(minimum[use],1+linear[use]*vertex[use]+quadratic[use]*vertex[use]**2)
        return float(minimum.min())
    minimum=strength_minimum(linear,quadratic)
    # Check both triangles of each pixel cell too; centered differences alone
    # can hide an adjacent pair that reverses order.
    dxh=np.diff(dx.astype(float),axis=1);dxv=np.diff(dx.astype(float),axis=0)
    dyh=np.diff(dy.astype(float),axis=1);dyv=np.diff(dy.astype(float),axis=0)
    cell_min=1.
    for a,b,c,d in ((dxh[:-1],dxv[:,:-1],dyh[:-1],dyv[:,:-1]),(dxh[1:],dxv[:,1:],dyh[1:],dyv[:,1:])):
        linear=a+d;quadratic=a*d-b*c
        minimum=min(minimum,strength_minimum(linear,quadratic))
        cell_min=min(cell_min,float((1+linear+quadratic).min()))
    stats=jacobian_stats(dx,dy)
    stats['all_strengths_min']=minimum;stats['cell_jacobian_min']=cell_min
    if not math.isfinite(minimum) or minimum<=.2 or stats['horizontal_derivative_min']<=.2 or stats['vertical_derivative_min']<=.2 or stats['outside_source_pixels'] or stats['locked_x_max_abs_px']>1e-5:
        raise ValueError('局部對位可能造成折返、過度擠壓或超出畫布，請改用更接近的影格重新計算')
    return stats


def maps(model,dimensions=None):
    model=validate_model(model)
    if dimensions is not None and list(dimensions)!=model['dimensions']:raise ValueError('對位模型畫布已變更，請重新計算')
    key=digest(model)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key);return _CACHE[key]
        dx,dy=_construct_map(model);_guard(dx,dy)
        dx.flags.writeable=False;dy.flags.writeable=False
        _CACHE[key]=(dx,dy)
        while len(_CACHE)>4 or sum(x.nbytes+y.nbytes for x,y in _CACHE.values())>_MAX_BYTES:
            _CACHE.popitem(last=False)
        return dx,dy


def apply(image,alignment,strength):
    alignment=validate_alignment(alignment)
    if not active(alignment):return image.copy(),False
    # Zero strength still overrides the previous affine, but samples nothing.
    amount=float(strength)*alignment['strength']/100
    if amount==0:return image.copy(),False
    dx,dy=maps(alignment['model'],image.size)
    return Image.fromarray(resample_frame(np.asarray(image.convert('RGBA')),dx,dy,amount)),False


def build_alignment(source_array,target_array,provenance):
    source=np.asarray(source_array);target=np.asarray(target_array)
    if source.shape!=target.shape or source.ndim!=3 or source.shape[2]!=4 or source.dtype!=np.uint8 or target.dtype!=np.uint8:raise ValueError('對位需要相同畫布尺寸的 RGBA 圖片')
    h,w=source.shape[:2]
    if min(w,h)<48 or max(w,h)>4096 or w*h>8_000_000:raise ValueError('局部對位支援邊長 48–4096、最多 800 萬像素；請調整素材尺寸')
    _dependencies()
    _,lp,left_report=boundary_profile(source,target,0)
    _,rp,right_report=boundary_profile(source,target,w-1)
    controls,selection=sparse_correspondences(source,target)
    boundaries={}
    for side,pairs in (('left',lp),('right',rp)):
        # A detached character has no edge intersections; identity is intentional.
        interior=[p for p in pairs if 0<p['target'][1]<h-1 and 0<p['source'][1]<h-1]
        if len(interior)>16:raise ValueError('貼邊輪廓過於複雜，請改用更接近的影格')
        boundaries[side]=dict(target=[0.]+[p['target'][1] for p in interior]+[float(h-1)],source=[0.]+[p['source'][1] for p in interior]+[float(h-1)])
    model=validate_model(dict(schema=1,dimensions=[w,h],controls=[dict(target=p['target'],source=p['source']) for p in controls],boundaries=boundaries,regularization=.00008,scale=1.,provenance=provenance))
    dx,dy=_construct_map(model)
    scale=1.
    while scale>=.3:
        try:checks=_guard(dx*scale,dy*scale);break
        except ValueError:scale*=.9
    else:raise ValueError('找不到安全的局部對位，請改用更接近的首尾影格')
    model['scale']=scale
    # Reconstruct from the persisted model before reporting or caching results.
    dx,dy=maps(model,(w,h));candidate=resample_frame(source,dx,dy)
    left_report['candidate_intervals']=alpha_intervals(candidate[:,0,3]);right_report['candidate_intervals']=alpha_intervals(candidate[:,-1,3])
    warnings=[]
    for label,boundary in (('左',left_report),('右',right_report)):
        if not boundary['matched'] and (boundary['source_intervals'] or boundary['target_intervals']):warnings.append(f'{label}貼邊輪廓無可靠對應，保留原有邊界')
    report=dict(dimensions=[w,h],selection=selection,before=metrics(source,target),after=metrics(candidate,target),boundaries=dict(left=left_report,right=right_report),jacobian=_guard(dx,dy),guard_scale=scale,max_displacement_px=float(np.hypot(dx,dy).max()),warnings=warnings,note='對位依計算當時的參考畫面保存；請播放確認動作速度及半透明輪廓。')
    return dict(enabled=True,strength=100.,model=model),report


def estimate_similarity(source_array, target_array):
    """Estimate only translation and uniform scale; never resample saved assets.

    Alpha moments provide a deterministic initial alignment. A small bounded
    refinement uses associated RGBA, so invisible RGB is not a correspondence.
    Safety/adoption belongs to the caller; a measured large move is not clipped
    down to the permitted move and falsely reported as a successful alignment.
    """
    _dependencies()
    from scipy.optimize import minimize
    source, target = np.asarray(source_array), np.asarray(target_array)
    if source.shape != target.shape or source.ndim != 3 or source.shape[2] != 4:
        raise ValueError('對位需要相同畫布的 RGBA 影格')
    h, w = source.shape[:2]
    yy, xx = np.mgrid[:h, :w]
    def moments(image):
        alpha = image[:, :, 3].astype(np.float64) / 255
        area = float(alpha.sum())
        if area < 32:
            raise ValueError('有效角色區域不足，無法估計位置與尺寸')
        return area, np.array([(alpha * xx).sum(), (alpha * yy).sum()]) / area
    area_a, center_a = moments(source)
    area_b, center_b = moments(target)
    scale = math.sqrt(area_b / area_a)
    center = np.array([(w - 1) / 2, (h - 1) / 2])
    shift = center_b - (center_a - center) * scale - center
    initial = np.array([*shift, (scale - 1) * 100], dtype=float)
    # Grossly different silhouettes are useful measurements, not fit candidates.
    if abs(initial[2]) > 5 or np.linalg.norm(initial[:2]) > 25:
        return dict(dx=float(initial[0]), dy=float(initial[1]), scale=float(scale), refined=False)
    factor = min(1., 384 / max(w, h))
    size = (max(16, round(w * factor)), max(16, round(h * factor)))
    fx, fy = size[0] / w, size[1] / h
    def features(image):
        p = image.astype(np.float32) / 255
        p[:, :, :3] *= p[:, :, 3:4]
        p = cv2.GaussianBlur(p, (0, 0), .65)
        return cv2.resize(p, size, interpolation=cv2.INTER_AREA)
    left, right = features(source), features(target)
    cx, cy = (size[0] - 1) / 2, (size[1] - 1) / 2
    def cost(parameters):
        dx, dy, percent = parameters
        s = 1 + percent / 100
        matrix = np.float32([[s, 0, (1-s)*cx + dx*fx], [0, s, (1-s)*cy + dy*fy]])
        warped = cv2.warpAffine(left, matrix, size, flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        return float(np.mean((warped - right) ** 2))
    if cost(initial) > 1e-10:
        bounds = [(initial[0]-1.25, initial[0]+1.25), (initial[1]-1.25, initial[1]+1.25),
                  (max(-5., initial[2]-.7), min(5., initial[2]+.7))]
        fitted = minimize(cost, initial, method='Powell', bounds=bounds,
                          options=dict(maxiter=25, xtol=.005, ftol=1e-7)).x
        candidates = [initial, fitted]
        snapped = fitted.copy()
        for index in (0, 1):
            if abs(snapped[index] - round(snapped[index])) < .12:
                snapped[index] = round(snapped[index])
        if abs(snapped[2]) < .08:
            snapped[2] = 0
        candidates.append(snapped)
        # Prefer the simpler transform when the measured costs are indistinct.
        best = min(candidates, key=cost)
        if cost(snapped) <= cost(best) + 1e-9:
            best = snapped
    else:
        best = initial
    if factor < 1 and cost(initial) > 1e-10:
        # A 384px pyramid can bias a native subpixel shift by ~0.1px. Refine
        # against original pixel centers, using a deterministic foreground
        # sample rather than repeatedly warping an entire large image.
        from scipy import ndimage
        def full_features(image):
            p=image.astype(np.float32)/255;p[:,:,:3]*=p[:,:,3:4]
            return ndimage.gaussian_filter(p,(.65,.65,0))
        full_a,full_b=full_features(source),full_features(target)
        rows,columns=np.where((source[:,:,3]>32)|(target[:,:,3]>32))
        step=max(1,math.ceil(len(rows)/6000));rows,columns=rows[::step],columns[::step]
        ref=full_b[rows,columns]
        def native_cost(parameters):
            dx,dy,percent=parameters;s=1+percent/100
            u=(columns-center[0]-dx)/s+center[0]
            v=(rows-center[1]-dy)/s+center[1]
            sampled=np.column_stack([ndimage.map_coordinates(full_a[:,:,channel],[v,u],order=1,mode='constant') for channel in range(4)])
            return float(np.mean((sampled-ref)**2))
        native=minimize(native_cost,best,method='Powell',
                        bounds=[(best[0]-.35,best[0]+.35),(best[1]-.35,best[1]+.35),(best[2]-.08,best[2]+.08)],
                        options=dict(maxiter=15,xtol=.0005,ftol=1e-8)).x
        candidates=[best,initial,native]
        snapped=native.copy()
        for index in (0,1):
            if abs(snapped[index]-round(snapped[index]))<.04:snapped[index]=round(snapped[index])
        if abs(snapped[2])<.04:snapped[2]=0
        candidates.append(snapped)
        best=min(candidates,key=native_cost)
    return dict(dx=round(float(best[0]), 5), dy=round(float(best[1]), 5),
                scale=round(1 + float(best[2]) / 100, 7), refined=True)


def similarity_pixels(pixels, transform):
    """One premultiplied bilinear sample for a persisted similarity map."""
    h,w = pixels.shape[:2]
    yy,xx = np.mgrid[:h,:w].astype(np.float32)
    cx,cy = (w-1)/2,(h-1)/2
    scale = transform['scale']
    dx = (xx-cx-transform['dx'])/scale+cx-xx
    dy = (yy-cy-transform['dy'])/scale+cy-yy
    return resample_frame(pixels,dx,dy)


def similarity_morph(image, reference, transform, amount):
    """Registered blend with known rigid geometry, without optical flow.

    Both images are sampled once into the same intermediate geometry. The
    target-to-intermediate map is T(amount) @ inverse(T); applying an affine
    first and optical flow second would blur and deform an otherwise rigid move.
    """
    if image.size != reference.size:
        raise ValueError('共同基準與影格的畫布尺寸不同')
    if amount <= 0:return image.copy()
    if amount >= 1:return reference.copy()
    s = transform['scale']
    forward = dict(scale=1+amount*(s-1),dx=amount*transform['dx'],dy=amount*transform['dy'])
    backward_scale = forward['scale']/s
    backward = dict(scale=backward_scale,dx=(amount-backward_scale)*transform['dx'],
                    dy=(amount-backward_scale)*transform['dy'])
    source = similarity_pixels(np.asarray(image.convert('RGBA')),forward).astype(np.float32)
    target = similarity_pixels(np.asarray(reference.convert('RGBA')),backward).astype(np.float32)
    source[:,:,:3] *= source[:,:,3:4]/255
    target[:,:,:3] *= target[:,:,3:4]/255
    out = source*(1-amount)+target*amount
    alpha = out[:,:,3:4]
    out[:,:,:3] = np.divide(out[:,:,:3]*255,alpha,out=np.zeros_like(out[:,:,:3]),where=alpha>0)
    out = np.uint8(np.clip(np.rint(out),0,255));out[out[:,:,3]==0,:3]=0
    return Image.fromarray(out,'RGBA')

