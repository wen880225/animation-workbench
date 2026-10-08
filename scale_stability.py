"""Small, coherent whole-clip scale drift; local motion is not a warp target.

No chained transforms: every observation is registered directly to one reference.
Only scale is corrected. Translation estimates place a moving pivot, preserving motion.
"""
import cv2
import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import savgol_filter
from seam_alignment import resample_frame


def _gray(a):
    f=min(1.,960/max(a.shape[:2]));h,w=a.shape[:2]
    size=(round(w*f),round(h*f));p=a.astype(np.float32)/255
    rgb=p[:,:,:3]*p[:,:,3:]+.5*(1-p[:,:,3:])
    return cv2.resize(np.uint8(np.rint(cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)*255)),size),cv2.resize(a[:,:,3],size),f


def _features(gray,alpha):
    mask=cv2.erode(np.uint8(alpha>200)*255,np.ones((3,3),np.uint8))
    pts=cv2.goodFeaturesToTrack(gray,1000,.015,5,mask=mask,blockSize=5)
    if pts is None:return np.empty((0,2),np.float32)
    h,w=gray.shape;counts={};chosen=[]
    for p in pts[:,0]:
        cell=(int(p[0]*6/w),int(p[1]*8/h))
        if counts.get(cell,0)<8:chosen.append(p);counts[cell]=counts.get(cell,0)+1
    return np.asarray(chosen,np.float32)


def _fit(x,y):
    # Spatially capped features prevent a textured mouth/garment dominating the fit.
    def solve(ids):
        return np.array([np.linalg.lstsq(np.column_stack([x[ids,k],np.ones(len(ids))]),y[ids,k],rcond=None)[0] for k in [0,1]])
    rng=np.random.default_rng(491);best=None;score=(-1,-1.)
    for _ in range(180):
        ids=rng.choice(len(x),3,replace=False)
        if np.min(np.ptp(x[ids],axis=0))<12:continue
        m=solve(ids)
        if np.any(np.abs(m[:,0]-1)>.06):continue
        error=np.linalg.norm(x*m[:,0]+m[:,1]-y,axis=1);inside=error<.5
        candidate=(int(inside.sum()),-float(np.minimum(error,.5).mean()))
        if candidate>score:best=inside;score=candidate
    if best is None or best.sum()<12:return None
    for _ in range(3):
        m=solve(np.flatnonzero(best));error=np.linalg.norm(x*m[:,0]+m[:,1]-y,axis=1);best=error<.5
        if best.sum()<12:return None
    return m,best,error


def _silhouette_support(reference,current,m,pivot):
    # Independent evidence: texture inside stationary anatomy is not global zoom.
    if np.max(np.abs(m[:,0]-1))<.0003:return True
    h,w=reference.shape;matrix=np.float32([[m[0,0],0,m[0,1]],[0,m[1,0],m[1,1]]])
    shift=pivot*(m[:,0]-1)+m[:,1]
    rigid=np.float32([[1,0,shift[0]],[0,1,shift[1]]])
    a=reference.astype(np.float32)/255;b=current.astype(np.float32)/255
    warped=cv2.warpAffine(a,matrix,(w,h));unscaled=cv2.warpAffine(a,rigid,(w,h))
    error=float(np.abs(warped-b).sum());baseline=float(np.abs(unscaled-b).sum())
    tolerance=max(2.,float(b.sum())*.0005)
    if error>baseline*1.03+tolerance:return False
    # Global error reduction alone can trade an unmoving head for a better torso.
    # Do not alter independent silhouette regions already matching without scale.
    ys,xs=np.where(b>.1)
    if not len(xs):return False
    for axis,low,high in [(0,int(ys.min()),int(ys.max()+1)),(1,int(xs.min()),int(xs.max()+1))]:
        cuts=np.linspace(low,high,4,dtype=int)
        for lo,hi in zip(cuts[:-1],cuts[1:]):
            section=(slice(lo,hi),slice(None)) if axis==0 else (slice(None),slice(lo,hi))
            mass=float(b[section].sum())
            if mass<32:continue
            base=float(np.abs(unscaled[section]-b[section]).sum())
            post=float(np.abs(warped[section]-b[section]).sum())
            if base<mass*.004 and post>base+max(1.,mass*.001):return False
    return True


def analyze(read,count,fps,reference=None,progress=lambda **kw:None):
    first=read(0);reference=first if reference is None else reference
    gray,alpha,factor=_gray(reference);pts=_features(gray,alpha)
    h,w=first.shape[:2];identity=dict(sx=1.,sy=1.,cx=(w-1)/2,cy=(h-1)/2)
    def review(reason):return dict(status='review',reason=reason,transforms=[identity.copy() for _ in range(count)],drift_percent=[0.,0.])
    if len(pts)<20:return review('可追蹤的穩定細節不足，已保留原比例')
    extent=np.ptp(pts,axis=0)
    ay,ax=np.where(alpha>32)
    silhouette_extent=np.array([np.ptp(ax),np.ptp(ay)])
    if np.any(extent<silhouette_extent*.4):return review('特徵集中在局部區域，缺少整體比例證據')
    pivot=np.array([(ax.min()+ax.max())/2,(ay.min()+ay.max())/2],dtype=np.float32)
    rows=[];good=[]
    for i in range(count):
        a=first if i==0 else read(i);current,current_alpha,_= _gray(a)
        q,status,_=cv2.calcOpticalFlowPyrLK(gray,current,pts[:,None],None,winSize=(31,31),maxLevel=3,criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,40,.001))
        back,reverse,_=cv2.calcOpticalFlowPyrLK(current,gray,q,None,winSize=(31,31),maxLevel=3,criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,40,.001))
        valid=(status[:,0]>0)&(reverse[:,0]>0)&(np.linalg.norm(back[:,0]-pts,axis=1)<.45)
        x,y=pts[valid],q[valid,0];fit=_fit(x,y) if len(x)>=20 else None
        valid_fit=False
        if fit is not None:
            m,inside,error=fit
            valid_fit=bool(inside.sum()>=max(16,.55*len(x),.4*len(pts)) and np.all(np.ptp(x[inside],axis=0)>=extent*.6) and np.all(np.abs(m[:,0]-1)<.04))
            if valid_fit:valid_fit=_silhouette_support(alpha,current_alpha,m,pivot)
            if valid_fit:
                center=(pivot*m[:,0]+m[:,1])/factor
                rows.append([*m[:,0],*center]);good.append(i)
        if not valid_fit:rows.append([np.nan,np.nan,*((pivot/factor).tolist())])
        progress(frame=i+1,frames=count)
    if len(good)<max(2,count*.85):return review('整段穩定特徵的共同縮放證據不足，已保留原比例')
    # Never bridge long unknown/occluded runs with a guessed correction curve.
    if max(np.diff([-1,*good,count]))>max(3,round(float(fps)*.2))+1:return review('部分影格追蹤不可靠，已保留原比例')
    values=np.array(rows);timeline=np.arange(count)
    for k in range(4):values[:,k]=np.interp(timeline,good,values[good,k])
    win=min(9,max(3,round(float(fps)/3)|1),count if count%2 else count-1)
    if win>=5:
        for k in range(2):values[:,k]=savgol_filter(median_filter(values[:,k],size=3,mode='nearest'),win,2,mode='interp')
    # A measurable subpixel change is retained; numerical noise is exact identity.
    values[:,:2][np.abs(values[:,:2]-1)<.00015]=1.
    drift=np.max(np.abs(values[:,:2]-1),axis=0)*100
    status='applied' if max(drift)>.03 else 'stable'
    if status=='stable':values[:,:2]=1.
    transforms=[dict(sx=float(v[0]),sy=float(v[1]),cx=float(v[2]),cy=float(v[3])) for v in values]
    return dict(status=status,reason='',transforms=transforms,drift_percent=drift.tolist(),reliable_frames=len(good),frames=count,reference_features=len(pts))


def apply(a,t):
    sx,sy=t['sx'],t['sy']
    if sx==1 and sy==1:return a.copy()
    h,w=a.shape[:2];yy,xx=np.mgrid[:h,:w].astype(np.float32)
    dx=(xx-t['cx'])*(sx-1);dy=(yy-t['cy'])*(sy-1)
    # Lock only the normal component on occupied edges. Tangential motion may
    # still correct scale, so an arm touching the left edge does not keep y drift.
    band=max(16,min(64,min(w,h)*.12))
    def axis_map(length,center,scale,low,high):
        axis=np.arange(length,dtype=np.float32);weight=np.ones(length,np.float32)
        for touched,distance in [(low,axis),(high,length-1-axis)]:
            if touched:
                u=np.clip(distance/band,0,1);weight*=u*u*(3-2*u)
        result=axis+(axis-center)*(scale-1)*weight
        if np.min(np.diff(result))<.5:raise ValueError('比例修正會擠壓邊緣，未套用此段穩定')
        return result
    u=axis_map(w,t['cx'],sx,np.any(a[:,0,3]>16),np.any(a[:,-1,3]>16))
    v=axis_map(h,t['cy'],sy,np.any(a[0,:,3]>16),np.any(a[-1,:,3]>16))
    # Monotone separable inverse maps provide the exact source support, including
    # blending bands. Never exempt pixels because an unrelated edge is touched.
    y,x=np.where(a[:,:,3]>16)
    if len(x) and (x.min()<u[0] or x.max()>u[-1] or y.min()<v[0] or y.max()>v[-1]):
        raise ValueError('比例修正會裁切角色，未套用此段穩定')
    return resample_frame(a,np.broadcast_to(u,(h,w))-xx,np.broadcast_to(v[:,None],(h,w))-yy)
