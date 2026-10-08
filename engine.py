from export_progress import sample,read_frame
"""Local animation extraction, ComfyUI orchestration, and alpha-safe export."""
import copy
import fractions
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.parse
import uuid
import re

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
JOBS = ROOT / 'jobs'
JOBS.mkdir(exist_ok=True)
LOCK = threading.RLock()
ACTIVE = {}
STOP = {}
PATHS = {}


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def jobdir(jid):
    if len(jid) != 32 or any(c not in '0123456789abcdef' for c in jid):
        raise ValueError('無效任務 ID')
    # A deleted/re-created folder may now belong to a different job. Never
    # resolve an old ID to new assets merely because its cached path matches.
    cached = PATHS.get(jid)
    if cached is not None:
        try:
            if json.loads((cached / 'job.json').read_text(encoding='utf-8')).get('id') == jid:
                return cached
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        PATHS.pop(jid, None)
    for p in JOBS.glob('*/job.json'):
        try:
            matches = json.loads(p.read_text(encoding='utf-8')).get('id') == jid
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if matches:
            PATHS[jid] = p.parent
            return p.parent
    return JOBS / jid


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')[:100] or 'animation'
    if value.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(10)],*[f'LPT{i}' for i in range(10)]}:
        value = '_' + value
    return value


def unique_folder(root, stem):
    stem = safe_name(stem)
    for n in range(1,100000):
        p = root / (stem if n == 1 else f'{stem}_{n:03d}')
        try:
            p.mkdir(parents=True)
            return p
        except FileExistsError:
            continue
    raise ValueError('同名任務過多')


def directory(j, key):
    return jobdir(j['id']) / j.get('paths', {}).get(key, {'frames':'frames','png':'rgba','cache':'candidates','output':'exports'}[key])


def preview_count(j):
    return min(int(j['config'].get('preview_frames',48)),len(j['frames']))


def save_clean_png(image, path):
    # Clear hidden RGB only: alpha, white foreground, and visible colors are preserved.
    pixels = np.array(image.convert('RGBA'))
    pixels[pixels[:,:,3] <= 2, :3] = 0
    Image.fromarray(pixels).save(path, format='PNG')


def read(jid):
    with LOCK:
        value = json.loads((jobdir(jid) / 'job.json').read_text(encoding='utf-8'))
        if value.get('id') != jid:
            raise ValueError('素材已被替換，請重新選取來源')
        return value


def update(jid, **fields):
    with LOCK:
        j = read(jid)
        j.update(fields)
        j['updated'] = time.time()
        atomic_json(jobdir(jid) / 'job.json', j)
        return j


def source_freshness(j, source_revision, exported_at=None):
    """Describe provenance without changing or deleting an immutable artifact."""
    if any(f.get('status') != 'done' for f in j.get('frames', [])):
        return dict(state='stale', reason='來源影格正在重跑或尚未完成；此為先前輸出', source_revision=source_revision)
    if source_revision is None:
        if exported_at is not None and j.get('media_changed_at', 0) > exported_at:
            return dict(state='stale', reason='來源 PNG 在此版本輸出後已更新；此為先前輸出', source_revision=None)
        return dict(state='unknown', reason='舊版本未記錄來源版本；可檢視，但無法確認與目前來源一致', source_revision=None)
    if source_revision != j.get('media_revision', 0):
        return dict(state='stale', reason='來源 PNG 已更新；此為先前輸出，請重新輸出目前結果', source_revision=source_revision)
    return dict(state='current', reason='與目前去背來源一致', source_revision=source_revision)


def public_job(j):
    """Read-time compatibility for old jobs; do not migrate their JSON silently."""
    result = copy.deepcopy(j)
    names = result.get('video_files') or {
        key: name for key, name in [('full', 'animation.webm'), ('preview', 'preview.webm')]
        if name in result.get('exports', [])}
    freshness = result.setdefault('export_freshness', {})
    for kind, name in names.items():
        if not name:
            continue
        recorded = freshness.get(kind, {})
        # Pending retries invalidate outputs even before replacement pixels exist.
        relevant = result
        if kind == 'preview':
            count = (recorded.get('report') or {}).get('verified_frames', result.get('config', {}).get('preview_frames', 48))
            relevant = dict(result, frames=result.get('frames', [])[:int(count)])
        state = source_freshness(relevant, recorded.get('source_revision'))
        if recorded.get('state') == 'stale':
            state.update(state='stale', reason=recorded.get('reason') or state['reason'])
        freshness[kind] = dict(recorded, **state)
    report = result.get('alpha_report')
    if report and not any(names.get(kind) == report.get('file') and value.get('state') == 'current'
                          for kind, value in freshness.items()):
        result['alpha_report'] = None
    result.setdefault('media_revision', 0)
    return result


def invalidate_exports(jid, reason, media_changed=False):
    """Retain historical video identity while invalidating current-source claims."""
    with LOCK:
        j = read(jid)
        freshness = public_job(j).get('export_freshness', {})
        for kind, value in freshness.items():
            value.update(state='stale', reason=reason)
            if (j.get('alpha_report') or {}).get('file') == j.get('video_files', {}).get(kind):
                value['report'] = j['alpha_report']
        fields = dict(export_freshness=freshness, alpha_report=None)
        if media_changed:
            fields.update(media_revision=j.get('media_revision', 0) + 1, media_changed_at=time.time())
        return update(jid, **fields)


def request(url, payload=None, timeout=30, raw=False):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read()
    return data if raw else json.loads(data)


def comfy_url(value):
    url = value.rstrip('/')
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost'):
        raise ValueError('此版本僅連接本機 ComfyUI（http://127.0.0.1:8188）')
    return url


def tool(name):
    local = ROOT / "tools" / "ffmpeg" / "bin" / (name + ".exe")
    result = str(local) if local.is_file() else shutil.which(name)
    if not result:
        raise ValueError(f'找不到 {name}，請加入 PATH 後重開工具')
    return result


def probe(path):
    p = subprocess.run([tool('ffprobe'), '-v', 'error', '-show_streams', '-show_format',
                        '-of', 'json', str(path)], capture_output=True, timeout=60)
    if p.returncode:
        raise ValueError('無法讀取影片：' + p.stderr.decode(errors='replace')[-1000:])
    info = json.loads(p.stdout)
    streams = [s for s in info['streams'] if s['codec_type'] == 'video'
               and not s.get('disposition', {}).get('attached_pic')]
    if not streams:
        raise ValueError('檔案沒有影片串流')
    v = streams[0]
    fps = v.get('avg_frame_rate', '0/0')
    if fps == '0/0':
        fps = v.get('r_frame_rate', '24/1')
    duration = float(v.get('duration') or info['format'].get('duration') or 0)
    if duration <= 0:
        raise ValueError('無法確認影片長度')
    return dict(width=v['width'], height=v['height'], fps=fps, duration=duration,
                codec=v['codec_name'], stream_index=v['index'])


def preflight(url):
    info = request(comfy_url(url) + '/object_info')
    graph = json.loads((ROOT / 'qwen_original.json').read_text(encoding='utf-8'))
    needed = {v['class_type'] for k, v in graph.items() if k != '472'}
    missing = sorted(needed - info.keys())
    models = []
    for node, field in [('459:451', 'unet_name'), ('459:453', 'clip_name'), ('459:454', 'vae_name')]:
        n = graph[node]
        try:
            spec = info[n['class_type']]['input']['required'][field]
            choices = spec[0] if isinstance(spec[0], list) else spec[1].get('options', [])
            if choices and n['inputs'][field] not in choices:
                models.append(n['inputs'][field])
        except KeyError:
            pass
    return {'ffmpeg': tool('ffmpeg'), 'ffprobe': tool('ffprobe'), 'missing_nodes': missing,
            'missing_models': models, 'comfy': not missing and not models,
            'free_gb': round(shutil.disk_usage(ROOT).free / 1024**3, 1)}


def create(source, cfg, owned=False):
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError('找不到影片檔案')
    info = probe(source)
    fps = fractions.Fraction(str(cfg.get('fps') or info['fps']))
    if not 1 <= float(fps) <= 120:
        raise ValueError('輸出 FPS 必須介於 1–120')
    if 'start_frame' in cfg or 'frame_count' in cfg:
        first = int(1 if cfg.get('start_frame') in ('',None) else cfg['start_frame'])
        count = int(cfg['frame_count']) if cfg.get('frame_count') not in ('',None) else None
        if first < 1 or (count is not None and count < 1):
            raise ValueError('起始幀與處理幀數必須為正整數')
        cfg = dict(cfg, start_frame=first, frame_count=count, start=(first-1)/float(fps), end=info['duration'])
        if cfg['start'] >= info['duration']: raise ValueError('起始幀超出影片範圍')
    start = float(cfg.get('start') or 0)
    end = float(cfg.get('end') or info['duration'])
    if not all(math.isfinite(x) for x in (start, end)) or not 0 <= start < end <= info['duration'] + .05:
        raise ValueError('請確認起訖秒數在影片範圍內')
    cfg = dict(cfg, fps=str(fps), start=start, end=end, comfy=comfy_url(cfg.get('comfy', 'http://127.0.0.1:8188')))
    steps = int(cfg.get('steps', 25))
    if not 1 <= steps <= 100:
        raise ValueError('步數必須介於 1–100')
    cfg['steps'] = steps
    cfg['preview_frames'] = int(cfg.get('preview_frames') or 48)
    if not 1 <= cfg['preview_frames'] <= 10000: raise ValueError('試跑幀數必須介於 1–10000')
    cfg['seed'] = int(cfg.get('seed', 899063591435756))
    if not 0 <= cfg['seed'] < 2**64:
        raise ValueError('Seed 超出範圍')
    jid = uuid.uuid4().hex
    display_name = safe_name(cfg.pop('original_name', '') or source.name)
    folder = unique_folder(JOBS, Path(display_name).stem)
    PATHS[jid] = folder
    if owned:
        target = folder / ('input' + source.suffix)
        shutil.move(str(source), target)
        source = target
    paths = dict(frames='_cache/source_frames',cache='_cache/model_output',png='transparent_png',output='output')
    for name in paths.values():
        (folder / name).mkdir(parents=True,exist_ok=True)
    shutil.copy2(ROOT / 'qwen_original.json', folder / 'workflow.json')
    j = dict(id=jid, name=display_name, folder_name=folder.name,paths=paths,source=str(source), config=cfg, video=info,
             source_size=source.stat().st_size, source_mtime=source.stat().st_mtime_ns,
             created=time.time(), updated=time.time(), state='ready', phase='等待試跑',
             frames=[], extracted=False, approved=False, error='', exports=[], preview_indices=[])
    atomic_json(folder / 'job.json', j)
    return j


class Paused(Exception):
    pass


def ffmpeg(jid, args, total=0, stage="", after=""):
    started=time.time()
    folder = jobdir(jid)
    progress=folder/'_ffmpeg_progress.txt'
    if total:progress.write_text('');update(jid,export_progress=sample(stage,0,total,started,after))
    last_update=0
    with (folder / 'ffmpeg.log').open('ab') as log:
        command = [tool('ffmpeg'), '-hide_banner', '-loglevel', 'warning', '-y', *(['-progress',str(progress),'-nostats'] if total else []), *args]
        log.write(('\n'+time.strftime('%Y-%m-%d %H:%M:%S')+' '+subprocess.list2cmdline(command)+'\n').encode('utf-8'));log.flush()
        p = subprocess.Popen(command,
                             stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        while p.poll() is None:
            if total and time.time()-last_update>=1:
                update(jid,export_progress=sample(stage,read_frame(progress),total,started,after));last_update=time.time()
            if STOP[jid].wait(.2):
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill(); p.wait()
                raise Paused()
        if p.returncode:
            raise RuntimeError('FFmpeg 失敗：' + (folder / 'ffmpeg.log').read_text(errors='replace')[-1400:])


def extract(jid):
    j = read(jid)
    if j['extracted']:
        return
    cfg, folder = j['config'], jobdir(jid)
    source = Path(j['source'])
    if source.stat().st_size != j['source_size'] or source.stat().st_mtime_ns != j['source_mtime']:
        raise ValueError('來源影片已變更，請建立新任務')
    expected = math.ceil((cfg['end'] - cfg['start']) * float(fractions.Fraction(cfg['fps'])))
    if cfg.get('frame_count'): expected=min(expected,cfg['frame_count'])
    needed = expected * j['video']['width'] * j['video']['height'] * 8
    if shutil.disk_usage(folder).free < needed + 512 * 1024**2:
        raise ValueError('磁碟空間不足：請縮短片段或降低 FPS')
    # Only clean partial extracted frames in this task's owned frames directory.
    frames_dir = directory(j,'frames')
    for f in frames_dir.glob('frame_*.png'):
        f.unlink()
    update(jid, phase='拆幀中（固定幀率，無音軌）')
    if 'start_frame' in cfg:
        vf=f"fps={cfg['fps']},trim=start_frame={cfg['start_frame']-1}"
        if cfg.get('frame_count'):vf+=f":end_frame={cfg['start_frame']-1+cfg['frame_count']}"
        vf+=',setpts=PTS-STARTPTS'
        args=['-i',j['source'],'-map',f"0:{j['video']['stream_index']}",'-an','-vf',vf]
        if cfg.get('frame_count'):args+=['-frames:v',str(cfg['frame_count'])]
    else:
        args=['-ss',str(cfg['start']),'-i',j['source'],'-t',str(cfg['end']-cfg['start']),'-map',f"0:{j['video']['stream_index']}",'-an','-vf','fps='+cfg['fps']]
    ffmpeg(jid,args+['-start_number','1',str(frames_dir/'frame_%08d.png')])
    files = sorted(frames_dir.glob('frame_*.png'))
    if not files:
        raise ValueError('拆幀結果為空，請增加片段長度')
    frames = [dict(index=i, file=f.name, status='pending', prompt_id=None, error='', warnings=[])
              for i, f in enumerate(files)]
    sample = list(range(min(cfg.get('preview_frames',48),len(frames))))
    with Image.open(files[0]) as first_image: dimensions=list(first_image.size)
    update(jid, frames=frames, extracted=True, preview_indices=sample,
           dimensions=dimensions, phase='拆幀完成')


def change_frame(jid, idx, **fields):
    with LOCK:
        j = read(jid)
        j['frames'][idx].update(fields)
        update(jid, frames=j['frames'])


def upload(url, path):
    boundary = uuid.uuid4().hex
    name = uuid.uuid4().hex + '.png'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            'Content-Type: image/png\r\n\r\n').encode() + path.read_bytes()
    body += f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(url+'/upload/image', data=body,
                                 headers={'Content-Type': 'multipart/form-data; boundary='+boundary})
    with urllib.request.urlopen(req, timeout=60) as r:
        result = json.load(r)
    return '/'.join(filter(None, [result.get('subfolder'), result['name']]))


def inspect_alpha(path, size):
    with Image.open(path) as img:
        if img.format != 'PNG' or 'A' not in img.getbands():
            raise ValueError('結果不是具有 Alpha 的 PNG；白底或棋盤格不能視為去背成功')
        if img.size != tuple(size):
            raise ValueError(f'結果尺寸 {img.size} 與原幀 {tuple(size)} 不一致，請檢查工作流解析度')
        a = np.asarray(img.getchannel('A'))
        if a.min() == 255:
            raise ValueError('結果完全不透明；Qwen 沒有輸出實際透明背景')
        if a.max() == 0:
            raise ValueError('結果完全透明；角色可能被移除')
        ratio = float(np.mean(a > 16))
        warnings = []
        if ratio < .02 or ratio > .98:
            warnings.append('前景面積異常，請人工確認')
        return dict(foreground=round(ratio, 5), warnings=warnings,
                    alpha_min=int(a.min()), alpha_max=int(a.max()))


def process_frame(jid, idx):
    j = read(jid); f = j['frames'][idx]; folder = jobdir(jid); cfg = j['config']; url = cfg['comfy']
    dest = directory(j,'png') / f['file']
    if f['status'] == 'done' and dest.exists():
        inspect_alpha(dest, j['dimensions'])
        return
    started = time.time()
    pid = f.get('prompt_id')
    if not pid:
        if f['status'] == 'submitting':
            raise RuntimeError('上次提交結果不明，請先在 ComfyUI 確認任務，再用「重跑指定幀」；工具不會重複提交')
        source = directory(j,'frames') / f['file']
        with Image.open(source) as im:
            w, h = im.size
            padded = (math.ceil(w/32)*32, math.ceil(h/32)*32)
            if padded != im.size:
                canvas = Image.new('RGB', padded, 'white')
                canvas.paste(im, (0, 0))
                source = directory(j,'cache') / ('input_'+f['file'])
                canvas.save(source)
        name = upload(url, source)
        graph = json.loads((folder / 'workflow.json').read_text(encoding='utf-8'))
        graph.pop('472', None)  # UI-only comparison; source workflow is preserved separately.
        graph['470']['inputs']['image'] = name
        graph['461']['inputs']['filename_prefix'] = f'animation_cutout/{jid}/{idx:08d}'
        graph['459:458']['inputs'].update(seed=cfg['seed'], steps=cfg['steps'])
        graph['459:474']['inputs']['prompt'] = cfg.get('prompt') or 'Remove the background, and output a PNG image'
        change_frame(jid, idx, status='submitting', error='')
        result = request(url+'/prompt', {'prompt': graph, 'client_id': 'animation-cutout-'+jid}, timeout=60)
        if 'prompt_id' not in result or result.get('node_errors'):
            change_frame(jid, idx, status='failed')
            raise RuntimeError('ComfyUI 拒絕工作流：'+str(result)[:1600])
        pid = result['prompt_id']
        change_frame(jid, idx, prompt_id=pid, status='running')
    deadline = time.monotonic()+3600
    while True:
        if STOP[jid].is_set():
            raise Paused()
        history = request(url+'/history/'+pid)
        if pid in history:
            h = history[pid]
            if h.get('status', {}).get('status_str') == 'error':
                change_frame(jid, idx, prompt_id=None, status='failed')
                raise RuntimeError('ComfyUI 執行失敗：'+str(h.get('status', {}).get('messages', []))[-1400:])
            images = h.get('outputs', {}).get('461', {}).get('images', [])
            if not images:
                raise RuntimeError('儲存節點 461 沒有回傳圖片，請檢查節點輸出')
            item = images[0]
            data = request(url+'/view?'+urllib.parse.urlencode({k:item[k] for k in ('filename','subfolder','type') if k in item}), raw=True)
            candidate = directory(j,'cache') / f['file']
            candidate.write_bytes(data)
            with Image.open(candidate) as im:
                w, h = j['dimensions']
                padded = (math.ceil(w/32)*32, math.ceil(h/32)*32)
                if im.size == padded and padded != (w,h):
                    cropped = im.crop((0,0,w,h))
                    cropped.save(candidate, format='PNG')
            metrics = inspect_alpha(candidate, j['dimensions'])
            with LOCK:
                replacement = dest.with_suffix('.partial.png')
                try:
                    with Image.open(candidate) as img:
                        save_clean_png(img,replacement)
                    os.replace(replacement,dest)
                    invalidate_exports(jid, '來源 PNG 已更新；此為先前輸出，請重新輸出目前結果', media_changed=True)
                finally:
                    replacement.unlink(missing_ok=True)
            candidate.unlink(missing_ok=True)
            (directory(j,'cache')/('input_'+f['file'])).unlink(missing_ok=True)
            # Adjacent-frame area jumps are alerts, not automatic rejection.
            if idx and j['frames'][idx-1].get('foreground') is not None:
                if abs(metrics['foreground']-j['frames'][idx-1]['foreground']) > .20:
                    metrics['warnings'].append('與前幀面積差異較大，可能是動作或邊緣閃動')
            change_frame(jid, idx, status='done', error='', seconds=round(time.time()-started,2), **metrics)
            return
        queue = request(url+'/queue')
        ids = [q[1] for key in ('queue_running','queue_pending') for q in queue.get(key, [])]
        if pid not in ids:
            # History can race queue completion; fetch once more before marking orphaned.
            if pid not in request(url+'/history/'+pid):
                raise RuntimeError('ComfyUI 已找不到此任務。確認佇列後可重跑該幀')
        if time.monotonic() > deadline:
            raise RuntimeError('等待超過 60 分鐘；保留任務 ID，稍後按繼續即可追蹤')
        STOP[jid].wait(1)


def export(jid, preview=False):
    j=read(jid); folder=jobdir(jid); fps=j['config']['fps']
    selected = list(range(preview_count(j))) if preview else list(range(len(j['frames'])))
    if not selected or any(j['frames'][i]['status']!='done' for i in selected):
        raise ValueError('仍有未完成幀，不能合成影片')
    for i in selected:
        inspect_alpha(directory(j,'png')/j['frames'][i]['file'],j['dimensions'])
    name=safe_name(Path(j['name']).stem)+('_preview' if preview else '_transparent')
    out=directory(j,'output')
    target=out/f'{name}.webm'
    base_name=name
    version=2
    while target.exists():
        name=f'{base_name}_v{version:03d}'
        target=out/f'{name}.webm'
        version+=1
    temp=out/f'{name}.partial.webm'
    update(jid,phase='合成透明 WebM（VP9，無音軌）')
    ffmpeg(jid,['-framerate',fps,'-start_number','1','-i',str(directory(j,'png')/'frame_%08d.png'),
                '-frames:v',str(len(selected)),'-an','-c:v','libvpx-vp9','-pix_fmt','yuva420p',
                '-lossless','1','-auto-alt-ref','0','-row-mt','1',str(temp)],len(selected),'合成 WebM','透明度驗證')
    # Decode alpha explicitly with libvpx: the native VP9 decoder can discard alpha.
    raw=directory(j,'cache')/f'{name}.alpha.raw'
    ffmpeg(jid,['-c:v','libvpx-vp9','-i',str(temp),'-vf','alphaextract','-f','rawvideo','-pix_fmt','gray',str(raw)],len(selected),'回讀透明度','逐幀核對')
    w,h=j['dimensions']; size=w*h
    if raw.stat().st_size != size*len(selected):
        raise ValueError('WebM 回讀幀數或 Alpha 尺寸錯誤')
    update(jid,phase='逐幀核對透明度',export_progress=sample('逐幀核對',0,len(selected),time.time()))
    with raw.open('rb') as stream:
        max_error=0
        for idx in selected:
            a=np.frombuffer(stream.read(size),np.uint8)
            if a.min()==255 or a.max()==0:
                raise ValueError('WebM 回讀透明度驗證失敗')
            with Image.open(directory(j,'png')/j['frames'][idx]['file']) as im:
                original=np.asarray(im.getchannel('A')).reshape(-1)
            error=int(np.abs(a.astype(np.int16)-original.astype(np.int16)).max())
            max_error=max(max_error,error)
            if error>2: raise ValueError(f'第 {idx+1} 幀 WebM Alpha 與 PNG 差異過大：{error}')
    raw.unlink()
    metadata=probe(temp)
    expected=len(selected)/float(fractions.Fraction(fps))
    if abs(metadata['duration']-expected) > max(.1,1/float(fractions.Fraction(fps))):
        raise ValueError('WebM 時長驗證失敗')
    os.replace(temp,target)
    if not preview:
        atomic_json(out/(name+'.json'),dict(fps=fps,frames=len(selected),size=j['dimensions'],
                    frame_pattern='../transparent_png/frame_%08d.png',start_number=1,loop=True,audio=False,
                    timing='CFR normalized from source; duration preserved within one output frame',
                    godot='Godot 4 requires a WebM playback extension. PNG frames can use AnimatedSprite2D.'))
    report=dict(file=target.name,verified_frames=len(selected),alpha_max_error=max_error,duration=metadata['duration'],audio=False,decoder='libvpx-vp9',verified_at=time.time(),source_revision=j.get('media_revision',0))
    atomic_json(out/(name+'_verification.json'),report)
    videos=dict(j.get('video_files',{}));videos['preview' if preview else 'full']=target.name
    current=[]
    for video in videos.values():
        stem=Path(video).stem
        current.extend(p.name for p in (out/video,out/(stem+'.json'),out/(stem+'_verification.json')) if p.exists())
    current.extend(p.name for p in out.glob('atlas*') if p.is_file())
    freshness=public_job(j).get('export_freshness',{})
    freshness['preview' if preview else 'full']=dict(state='current',reason='與輸出時去背來源一致',source_revision=j.get('media_revision',0),report=report)
    update(jid,video_files=videos,export_version=time.time(),alpha_report=report,exports=sorted(set(current)),export_freshness=freshness)


def worker(jid, mode):
    idx=None
    try:
        j=read(jid)
        if mode!='export':
            check=preflight(j['config']['comfy'])
            if not check['comfy']:
                raise ValueError('缺少節點或模型：'+str(check))
        update(jid,state='running',error='',export_progress=None,run_mode=mode)
        extract(jid)
        j=read(jid)
        indices=[] if mode=='export' else [j['retry_index']] if mode=='single' else j['preview_indices'] if mode=='preview' else range(len(j['frames']))
        for idx in indices:
            if STOP[jid].is_set(): raise Paused()
            update(jid,phase=f'去背第 {idx+1} / {len(j["frames"])} 幀',current=idx)
            process_frame(jid,idx)
        if mode=='single':
            update(jid,state='review',phase='指定幀重跑完成；請檢查後再次確認處理全部')
            return
        idx=None  # Encoding errors belong to the job, not the last processed frame.
        export(jid,preview=(mode=='preview'))
        update(jid,state='review' if mode=='preview' else 'complete',phase=f'試跑 {preview_count(j)} 幀完成。檢查下方循環影片後，按「確認效果，繼續處理全部」。' if mode=='preview' else '完成：透明 WebM 與 PNG 序列')
    except Paused:
        update(jid,state='paused',phase='已暫停；已送出的 ComfyUI 任務可在背景完成')
    except Exception as e:
        if idx is not None:
            change_frame(jid,idx,error=str(e))
        update(jid,state='attention',error=str(e),phase='需要檢查，可修正後繼續')
    finally:
        with LOCK: ACTIVE.pop(jid,None)


def start(jid, mode):
    with LOCK:
        if ACTIVE:
            raise ValueError('已有任務執行中，請先暫停，避免同時佔用顯存')
        j=read(jid)
        if mode=='full' and not j['approved']:
            raise ValueError('請先確認試跑效果')
        STOP[jid]=threading.Event()
        update(jid,state='running',last_mode=mode,error='')
        t=threading.Thread(target=worker,args=(jid,mode),daemon=True)
        ACTIVE[jid]=t; t.start()


def recover():
    for p in JOBS.glob('*/job.json'):
        j=json.loads(p.read_text(encoding='utf-8'))
        PATHS[j['id']]=p.parent
        if j['state']=='running':
            update(j['id'],state='paused',phase='工具已重新啟動，可繼續；保留 ComfyUI 任務 ID')


def spritesheet(jid):
    j=read(jid); folder=jobdir(jid)
    if j['state']!='complete': raise ValueError('完整處理完成後才能輸出圖集')
    w,h=j['dimensions']
    cols=min(8,4096//w); rows=min(8,4096//h)
    if not cols or not rows: raise ValueError('單幀超過 4096，請使用 PNG 序列')
    per=cols*rows; pages=[]
    for offset in range(0,len(j['frames']),per):
        subset=j['frames'][offset:offset+per]
        sheet=Image.new('RGBA',(cols*w,math.ceil(len(subset)/cols)*h))
        for i,f in enumerate(subset):
            with Image.open(directory(j,'png')/f['file']) as im:
                sheet.paste(im.convert('RGBA'),((i%cols)*w,(i//cols)*h))
        name=f'atlas_{offset//per:03d}.png'
        sheet.save(directory(j,'output')/name)
        pages.append(dict(file=name,first_frame=offset,frames=len(subset),columns=cols))
    atomic_json(directory(j,'output')/'atlas.json',dict(fps=j['config']['fps'],cell_size=[w,h],pages=pages))
    update(jid,exports=sorted(p.name for p in directory(j,'output').iterdir()))
