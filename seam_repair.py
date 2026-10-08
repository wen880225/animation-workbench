"""Immutable locally reconstructed seams shared by preview and export."""
import copy, hashlib, json, math, os, subprocess, sys, threading, time, uuid
from pathlib import Path
from fractions import Fraction
import numpy as np
from PIL import Image
import transitions as t

RUNS={}
LOCK=threading.RLock()

def active(project):return bool(project and isinstance(project.get('seam_repair'),dict) and project['seam_repair'].get('enabled') is True)

def binding(project,sources):
    p=copy.deepcopy(project);p.pop('seam_repair',None)
    return t._hash(dict(base=t._seam_binding(p,sources),windows=[(c['key'],c['head_frames'],c['tail_frames']) for c in p['clips']],version=1))

def frame_path(project,entry):
    rid=t._id(project['seam_repair']['id'],'修復版本')
    return t._beneath(t.projectdir(project['id'])/'repairs'/rid,entry['file'])

def validate(value,project,sources,skip=False):
    if value is None:return None
    if not isinstance(value,dict) or value.get('schema')!=1 or value.get('enabled') is not True:raise ValueError('接縫修復記錄無效')
    rid=t._id(value.get('id'),'修復版本')
    path=t.projectdir(project['id'])/'repairs'/rid/'repair.json'
    if not path.is_file():raise ValueError('修復影格記錄遺失，請重新修復')
    saved=json.loads(path.read_text(encoding='utf-8'))
    if saved!=value:raise ValueError('修復影格記錄不符，請重新修復')
    if not skip and binding(project,sources)!=saved['binding']:raise ValueError('素材、順序或調整已變更，請重新修復接點')
    return saved

def entry(project,key,index):return (project.get('seam_repair') or {}).get('frames',{}).get(key,{}).get(str(index)) if project else None

def read(project,key,index):
    item=entry(project,key,index)
    if not item:return None
    path=frame_path(project,item)
    try:
        content=path.read_bytes()
        if hashlib.sha256(content).hexdigest()!=item['sha256']:raise ValueError('hash')
        import io
        with Image.open(io.BytesIO(content)) as im:
            if list(im.size)!=project['seam_repair']['dimensions']:raise ValueError('size')
            return im.convert('RGBA')
    except (OSError,ValueError) as exc:raise ValueError('修復影格遺失或損壞，請重新修復；未改播原始影格') from exc

def runtime():
    path=Path(__file__).with_name('repair_runtime.json')
    cfg=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    exe=cfg.get('python',sys.executable)
    weights=Path(__file__).parent/'models/rife425/flownet.pkl'
    if not Path(exe).is_file() or not weights.is_file():raise ValueError('尚未安裝本地接縫模型，請先執行 setup_seam_model.py')
    return str(exe),str(weights)

def build(project,sources,backend='rife',progress=lambda **kw:None):
    pairs=t.ts.pairs(project)
    if not pairs:raise ValueError('請加入至少一個循環接點或兩段接續動畫')
    p=copy.deepcopy(project);p.pop('seam_repair',None);p.pop('seam_closure',None);p['reviews']={}
    p['clips']=[t._clip(c,sources[c['key']]) for c in p['clips']]
    by={c['key']:c for c in p['clips']}
    for c in p['clips']:
        length=c['end']-c['start']+1
        if length<6:raise ValueError('每段至少需要 6 幀才能重建兩側過渡')
        n=max(2,min(12,round(float(Fraction(sources[c['key']].fps))*.25),length//3))
        # Reconstruct from original geometry and current tone, replacing old end warps.
        for side in ('head','tail'):c[side]=t._affine({})
        c['head_frames']=c['tail_frames']=n
    rid=uuid.uuid4().hex;root=t.projectdir(p['id'])/'repairs'/rid
    required=sum(math.prod(sources[c['key']].dimensions)*8*(c['end']-c['start']+1) for c in p['clips'])
    import shutil
    if shutil.disk_usage(t.projectdir(p['id'])).free<required+512*1024**2:raise ValueError('磁碟空間不足，尚未修復')
    root.mkdir(parents=True);(root/'inputs').mkdir();(root/'frames').mkdir()
    manifest=dict(pairs=[],progress=str(root/'progress.json'),weights='')
    result=dict(id=rid,schema=1,enabled=True,binding=binding(p,sources),dimensions=list(next(iter(sources.values())).dimensions),frames={},pairs=[],backend='RIFE 4.25 + shared RGBA flow',created=time.time())
    # Stabilize the entire retained clip before constructing seam bridge inputs.
    import scale_stability as stability
    reference_clip=by[p['sequence'][0]]
    reference=np.array(t._render_base_frame(reference_clip,sources[reference_clip['key']],reference_clip['start'],project=p)[0])
    result['stability']=dict(schema=1,reference_key=reference_clip['key'],clips={})
    for ordinal,c in enumerate(p['clips'],1):
        key=c['key'];source=sources[key];count=c['end']-c['start']+1
        def read_frame(i):return np.array(t._render_base_frame(c,source,c['start']+i,project=p)[0])
        def measured(frame,frames):progress(phase=f'量測整段比例（第 {ordinal}/{len(p["clips"])} 段）',done=frame,total=frames,unit='幀')
        plan=stability.analyze(read_frame,count,float(Fraction(source.fps)),reference=reference,progress=measured)
        plan['reference']='shared'
        if plan['status']=='review' and key!=reference_clip['key']:
            plan=stability.analyze(read_frame,count,float(Fraction(source.fps)),progress=measured)
            plan['reference']='local';plan['warning']='與其他表情缺少可靠共同特徵；僅穩定本段，跨段比例需檢查'
        result['stability']['clips'][key]=plan
        if plan['status']=='applied':
            try:
                frames={}
                for i,transform in enumerate(plan['transforms']):
                    pixels=stability.apply(read_frame(i),transform)
                    file=f'frames/{key}_{c["start"]+i:08d}.png'
                    Image.fromarray(pixels).save(root/file);frames[str(c['start']+i)]=dict(file=file)
                    progress(phase=f'穩定整段比例（第 {ordinal}/{len(p["clips"])} 段）',done=i+1,total=count,unit='幀')
                result['frames'][key]=frames
            except ValueError as error:
                # No partial per-clip stabilization: avoid a discontinuity at a rejected frame.
                plan['status']='review';plan['reason']=str(error)
        # Stable/review clips deliberately keep original pixels outside the seam windows.
    for ordinal,(a,b) in enumerate(pairs,1):
        ca,cb=by[a],by[b];na,nb=ca['tail_frames'],cb['head_frames']
        ai,bi=ca['end']-na+1,cb['start']+nb-1
        task=dict(ordinal=ordinal,outputs=[])
        for side,c,index in [('left',ca,ai),('right',cb,bi)]:
            cached=result['frames'].get(c['key'],{}).get(str(index))
            if cached:
                with Image.open(root/cached['file']) as stored:image=stored.convert('RGBA')
            else:image,_=t._render_base_frame(c,sources[c['key']],index,project=p)
            path=root/'inputs'/f'{ordinal}_{side}.png';image.save(path);task[side]=str(path)
        da=(na-1)/float(Fraction(sources[a].fps));db=(nb-1)/float(Fraction(sources[b].fps));anchor_t=da/(da+db)
        for side,c,n in [('tail',ca,na),('head',cb,nb)]:
            for j in range(n):
                index=c['end']-n+1+j if side=='tail' else c['start']+j
                dt=(j/float(Fraction(sources[a].fps)))/(da+db) if side=='tail' else (da+j/float(Fraction(sources[b].fps)))/(da+db)
                file=f'frames/{c["key"]}_{index:08d}.png'
                task['outputs'].append(dict(t=dt,path=str(root/file),anchor=side=='tail' and j==n-1,reuse_anchor=side=='head' and j==0))
                result['frames'].setdefault(c['key'],{})[str(index)]=dict(file=file)
        manifest['pairs'].append(task)
        result['pairs'].append(dict(a=a,b=b,tail_frames=na,head_frames=nb,anchor_t=anchor_t))
    progress(phase='本地模型重建接縫',done=0,total=len(pairs),unit='個接點')
    if backend=='classic-test':
        from seam_repair_core import run
        run(manifest,backend)
    else:
        exe,manifest['weights']=runtime()
        request=root/'request.json';t.e.atomic_json(request,manifest)
        with (root/'model.log').open('wb') as log:
            proc=subprocess.Popen([exe,str(Path(__file__).with_name('seam_repair_core.py')),str(request)],stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            deadline=time.monotonic()+1800
            while proc.poll() is None:
                if time.monotonic()>deadline:
                    proc.kill();proc.wait();raise ValueError('本地接縫模型逾時，原草稿未改變')
                try:progress(phase='本地模型重建接縫',**json.loads((root/'progress.json').read_text()))
                except (OSError,ValueError):pass
                time.sleep(.3)
            if proc.returncode:raise ValueError('本地接縫模型執行失敗，原草稿未改變。詳見 '+str(root/'model.log'))
    # Verify immutable PNGs and source freshness before returning anything to UI.
    for s in sources.values():
        if any(t._stamp(path)!=stamp for path,stamp in zip(s.files,s.stamps)):raise ValueError('修復期間來源已變更，請重新修復')
    for key,items in result['frames'].items():
        for item in items.values():
            path=root/item['file'];content=path.read_bytes();item['sha256']=hashlib.sha256(content).hexdigest()
            with Image.open(path) as im:
                if list(im.size)!=result['dimensions'] or im.mode!='RGBA':raise ValueError('修復影格尺寸或透明度格式不符')
    t.e.atomic_json(root/'repair.json',result);p['seam_repair']=result
    for pair in result['pairs']:
        ca,cb=by[pair['a']],by[pair['b']]
        if read(p,ca['key'],ca['end']).tobytes()!=read(p,cb['key'],cb['start']).tobytes():raise ValueError('接點輸出不一致')
    t.e.atomic_json(root/'result_project.json',p)
    return dict(project=p,report=dict(pairs=len(pairs),backend=result['backend'],note='已重建並套用；請慢播驗收動作。原始素材保留，復原可返回修復前。'))

def start(project,sources):
    runtime()
    pid=project['id'];token=uuid.uuid4().hex
    with LOCK:
        if any(v['state']=='running' for v in RUNS.values()) or t.e.ACTIVE or any(v.get('state')=='running' for v in t.RUNS.values()) or any(v.get('state')=='running' for v in t.le.RUNS.values()):raise ValueError('已有模型任務執行中，請稍後重試')
        RUNS[token]=dict(id=token,project_id=pid,state='running',phase='準備修復',done=0,total=len(t.ts.pairs(project)))
    def work():
        try:
            def update(**kw):
                with LOCK:RUNS[token].update(kw)
            value=build(project,sources,progress=update)
            with LOCK:RUNS[token].update(state='complete',phase='修復完成',result=value)
        except Exception as exc:
            with LOCK:RUNS[token].update(state='error',phase=str(exc))
    threading.Thread(target=work,daemon=True).start()
    return status(token,pid)

def status(token,pid):
    with LOCK:
        value=RUNS.get(t._id(token,'修復任務'))
        if not value or value['project_id']!=t._id(pid):raise ValueError('找不到修復任務；請重新修復')
        return copy.deepcopy(value)

def verify_exports(project,exported,root,sources):
    by={c['key']:c for c in exported};pairs=[]
    for a,b in t.ts.pairs(project):
        ca,cb=by[a],by[b]
        with Image.open(Path(root)/(ca['png_pattern']%ca['frames'])) as im:left=np.array(im.convert('RGBA'))
        with Image.open(Path(root)/(cb['png_pattern']%1)) as im:right=np.array(im.convert('RGBA'))
        equal=bool(np.array_equal(left,right))
        if not equal:raise ValueError('接縫修復輸出的實際 PNG 不一致')
        pairs.append(dict(a=a,b=b,endpoints_equal=True,corrected=True,can_skip_duplicate_head=Fraction(sources[a].fps)==Fraction(sources[b].fps)))
    return dict(enabled=True,all_equal=True,pairs=pairs,route_mode=t.ts.route_mode(project),note='端點與解析度已驗證，動作仍需視覺驗收')
