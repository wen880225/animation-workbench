"""Directed animation transitions, immutable exports and alpha-safe endpoint edits."""
import copy
import hashlib
import json
import math
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image

import engine as e
import loop_editor as le
import transition_tone as tt
import transition_seams as ts
from export_progress import sample, read_frame

LOCK = threading.RLock()
RUNS = {}
_FEATURES = OrderedDict()
_FEATURE_LOCK = threading.RLock()
_HEX = re.compile(r'^[0-9a-f]{32}$')
_AFFINE_DEFAULTS = dict(dx=0, dy=0, sx=100, sy=100, angle=0, cx=50, cy=50)
_AFFINE_LIMITS = dict(dx=(-100, 100), dy=(-100, 100), sx=(80, 120), sy=(80, 120), angle=(-10, 10), cx=(0, 100), cy=(0, 100))


def _id(value, label='專案'):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError(f'無效{label} ID')
    return value


def _beneath(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('檔案路徑超出素材資料夾')
    return path


def projectdir(pid):
    return _beneath(e.ROOT / 'transition_projects', _id(pid))


def _component(value, label='版本'):
    if not isinstance(value, str) or not value or len(value) > 100 or value in ('.', '..') or any(c in value for c in '/\\:\x00') or any(ord(c) < 32 for c in value):
        raise ValueError(f'無效{label}名稱')
    return value


def _read_json(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('記錄內容必須是物件')
    return value


def _name(value, fallback):
    if not isinstance(value, str):
        raise ValueError('名稱必須是文字')
    return ''.join(c for c in value if ord(c) >= 32).strip()[:100] or fallback


def _number(value, low, high, label, integer=False):
    if isinstance(value, bool):
        raise ValueError(f'{label}必須是數字')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label}必須是數字') from None
    if not math.isfinite(number) or not low <= number <= high or (integer and not number.is_integer()):
        raise ValueError(f'{label}超出範圍（{low}–{high}）')
    return int(number) if integer else number


def _fps(value):
    try:
        fps = Fraction(str(value))
        if not 0 < fps <= 1000:
            raise ValueError()
        return str(fps)
    except (ValueError, ZeroDivisionError):
        raise ValueError('來源 FPS 無效') from None


def _dimensions(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError('來源畫布尺寸無效')
    return tuple(_number(v, 1, 32768, '畫布尺寸', True) for v in value)


def _stamp(path):
    s = path.stat()
    if not path.is_file():
        raise ValueError('來源 PNG 不存在')
    return [s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino]


@dataclass
class Source:
    job_id: str
    version: str
    label: str
    dimensions: tuple
    fps: str
    files: list
    stamps: list

    def public(self):
        revision = _hash(dict(dimensions=self.dimensions, fps=self.fps,
                              files=[[p.name, *stamp] for p, stamp in zip(self.files, self.stamps)]))
        return dict(job_id=self.job_id, version=self.version, label=self.label, frames=len(self.files), dimensions=list(self.dimensions), fps=self.fps, revision=revision)


def resolve_source(job_id, version='original'):
    _id(job_id, '素材')
    j = e.read(job_id)
    if j.get('is_test'):
        raise ValueError('測試素材不能加入切換專案')
    root = e.jobdir(job_id).resolve()
    if not root.is_relative_to(e.JOBS.resolve()):
        raise ValueError('素材不在任務資料夾內')
    version = _component(version)
    if version == 'original':
        frames = j.get('frames', [])
        if j.get('state') == 'running' or len(frames) < 2 or any(f.get('status') != 'done' for f in frames):
            raise ValueError('請先完成整段去背，再加入原始去背版本')
        folder = _beneath(root, j.get('paths', {}).get('png', 'rgba'))
        files = [_beneath(folder, _component(f.get('file'), 'PNG')) for f in frames]
        dimensions = _dimensions(j.get('dimensions'))
        fps = _fps(j.get('config', {}).get('fps', 24))
        label = j.get('name', root.name) + ' · 原始去背'
    else:
        if not version.startswith('修整'):
            raise ValueError('只能選擇已完成的修整版本')
        folder = _beneath(root, 'loop_edits/' + version)
        status = _read_json(folder / 'status.json')
        if status.get('state') != 'complete':
            raise ValueError('此修整版本尚未完成')
        report = _read_json(folder / 'verification.json')
        count = _number(report.get('frames'), 2, 1000000, '版本幀數', True)
        dimensions = _dimensions(report.get('dimensions'))
        fps = _fps(report.get('fps'))
        png_root = _beneath(folder, 'transparent_png')
        files = [_beneath(png_root, f'frame_{i:08d}.png') for i in range(1, count + 1)]
        label = j.get('name', root.name) + ' · ' + version
    if any(path.suffix.lower() != '.png' for path in files):
        raise ValueError('來源必須是透明 PNG 圖序')
    stamps = [_stamp(path) for path in files]
    return Source(job_id, version, label, dimensions, fps, files, stamps)


def catalog():
    sources = []
    for path in sorted(e.JOBS.glob('*/job.json')):
        try:
            j = _read_json(path)
            if j.get('is_test'):
                continue
            jid = _id(j.get('id'), '素材')
            versions = ['original'] + [p.name for p in (path.parent / 'loop_edits').glob('修整*') if p.is_dir()]
            for version in versions:
                try:
                    sources.append(resolve_source(jid, version).public())
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return dict(sources=sources)


def _affine(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('首尾變形參數無效')
    result = {k: _number(value.get(k, default), *_AFFINE_LIMITS[k], k) for k, default in _AFFINE_DEFAULTS.items()}
    if 'region' in value:
        result['region'] = le.region(value['region'])
    if 'alignment' in value:
        result['alignment'] = le.sa.validate_alignment(value['alignment'])
    return result


def _clip(value, source):
    if not isinstance(value, dict):
        raise ValueError('動畫段落內容無效')
    start = _number(value.get('start', 1), 1, len(source.files), '起幀', True)
    end = _number(value.get('end', len(source.files)), start + 1, len(source.files), '尾幀', True)
    count = end - start + 1
    head_frames = _number(value.get('head_frames', min(6, count // 2)), 0, count, '開頭修正幀數', True)
    tail_frames = _number(value.get('tail_frames', min(6, count // 2)), 0, count, '結尾修正幀數', True)
    if head_frames + tail_frames > count:
        raise ValueError('開頭與結尾修正範圍不能重疊，請減少修正幀數')
    return dict(key=_id(value.get('key'), '動畫段落'), label=_name(value.get('label', source.label), source.label), job_id=source.job_id, version=source.version,
                start=start, end=end, head_frames=head_frames, tail_frames=tail_frames,
                tone=_number(value.get('tone', 0), -100, 100, '明暗'), contrast=_number(value.get('contrast', 0), -50, 50, '對比'),
                head=_affine(value.get('head')), tail=_affine(value.get('tail')))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _tonal_settings(project, clip):
    project = project or {}
    match = project.get('tone_match') or {}
    correction = match.get('adjustments', {}).get(clip['key'], {}) if match.get('enabled') else {}
    return dict(shared=project.get('shared_tone') or {}, correction=correction)


def clip_signature(clip, source, project=None):
    visual = {k: v for k, v in clip.items() if k not in ('label',)}
    value = dict(clip=visual, dimensions=source.dimensions, fps=source.fps,
                 files=[[p.name, *stamp] for p, stamp in zip(source.files[clip['start']-1:clip['end']], source.stamps[clip['start']-1:clip['end']])])
    tone = _tonal_settings(project, clip)
    if any(v for values in tone.values() for v in values.values()):
        value['tonal'] = tone
    if ts.active(project):
        value['seams'] = dict(binding=project['seam_closure']['binding'],
            links=[link for link in project['seam_closure']['links'] if clip['key'] in (link['a'],link['b'])])
    return _hash(value)


def pair_signature(a, b, sources, project=None):
    return _hash(dict(direction=[a['key'], b['key']], a=clip_signature(a, sources[a['key']], project), b=clip_signature(b, sources[b['key']], project), metric_version=1))


def _tone_binding(clip, source):
    return _hash(dict(key=clip['key'], source=source.public(), start=clip['start'], end=clip['end'],
                      tone=clip['tone'], contrast=clip['contrast']))


def _tone_values(value, label, tone_limit=100, contrast_limit=50):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(label + '設定無效')
    return dict(tone=_number(value.get('tone', 0), -tone_limit, tone_limit, label+'明暗'),
                contrast=_number(value.get('contrast', 0), -contrast_limit, contrast_limit, label+'對比'))


def _validate_tone_match(value, clips, sources, skip=False):
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get('enabled', False)) is not bool:
        raise ValueError('統一明暗設定無效')
    enabled = value.get('enabled', False)
    adjustments, bindings = value.get('adjustments', {}), value.get('bindings', {})
    if not isinstance(adjustments, dict) or not isinstance(bindings, dict) or len(adjustments)>16 or len(bindings)>16:
        raise ValueError('統一明暗校準記錄無效')
    result = dict(enabled=enabled, reference=value.get('reference', ''), adjustments={}, bindings={})
    for key, values in adjustments.items():
        result['adjustments'][_id(key, '校準動畫')] = _tone_values(values, '自動校準', 60, 35)
    for key, binding in bindings.items():
        if not isinstance(binding, str) or not re.fullmatch('[0-9a-f]{64}', binding):
            raise ValueError('明暗校準來源記錄無效')
        result['bindings'][_id(key, '校準动画')] = binding
    report = value.get('report', {})
    if not isinstance(report, dict) or len(json.dumps(report, ensure_ascii=False, allow_nan=False)) > 100000:
        raise ValueError('明暗校準報告無效')
    result['report'] = copy.deepcopy(report)
    keys = {c['key'] for c in clips}
    if enabled and not skip:
        if result['reference'] not in keys or set(adjustments) != keys or set(bindings) != keys:
            raise ValueError('動畫組成已變更，請重新統一明暗或先停用自動校準')
        for clip in clips:
            if bindings[clip['key']] != _tone_binding(clip, sources[clip['key']]):
                raise ValueError('來源、幀範圍或單段明暗已變更，請重新統一明暗或先停用自動校準')
    return result


def _seam_binding(project, sources):
    clips=[]
    for clip in project['clips']:
        source=sources[clip['key']].public()
        source.pop('label',None)
        clips.append(dict(clip={key:value for key,value in clip.items() if key not in ('label','head_frames','tail_frames')},
                          source=source,tonal=_tonal_settings(project,clip)))
    return _hash(dict(sequence=project['sequence'],clips=clips,renderer_version=1))


def _validate_seams(value, project, sources, skip=False):
    result=ts.validate(value,project,_seam_binding(project,sources),projectdir(project['id'])/'anchors',skip)
    if result and result['enabled'] and not skip:
        for link in result['links']:
            if link['dimensions'] != list(sources[link['a']].dimensions):
                raise ValueError('共同接點基準與來源畫布尺寸不同，請重新建立共同接點')
    return result


def _verify_seams(project,sources):
    if not ts.active(project):
        return
    for source in sources.values():
        if any(_stamp(path)!=stamp for path,stamp in zip(source.files,source.stamps)):
            raise ValueError('來源在共同接點處理期間变更，請更新來源後重新建立共同接點')
    _validate_seams(project['seam_closure'],project,sources)


def validate_project(value, skip_alignment=None, skip_tone=False, skip_seams=False):
    if not isinstance(value, dict):
        raise ValueError('專案內容無效')
    pid = _id(value.get('id'))
    saved = _read_json(projectdir(pid) / 'project.json')
    raw_clips = value.get('clips', [])
    if not isinstance(raw_clips, list) or len(raw_clips) > 16:
        raise ValueError('每個專案最多 16 段動畫')
    clips, sources, resolved, dimensions = [], {}, {}, None
    for raw in raw_clips:
        if not isinstance(raw, dict):
            raise ValueError('動畫段落內容無效')
        ref = (raw.get('job_id'), raw.get('version', 'original'))
        if not all(isinstance(v, str) for v in ref):
            raise ValueError('來源版本無效')
        if ref not in resolved:
            resolved[ref] = resolve_source(*ref)
        source = resolved[ref]
        clip = _clip(raw, source)
        if clip['key'] in sources:
            raise ValueError('動畫段落 ID 重複')
        if dimensions is not None and dimensions != source.dimensions:
            raise ValueError('所有段落必須使用相同畫布尺寸；請先統一解析度')
        dimensions = source.dimensions
        clips.append(clip)
        sources[clip['key']] = source
    by_key = {c['key']: c for c in clips}
    _verify_project_alignments(dict(clips=clips), sources, skip_alignment)
    sequence = value.get('sequence', [])
    if not isinstance(sequence, list) or len(sequence) > 64 or any(not isinstance(k, str) or k not in by_key for k in sequence):
        raise ValueError('切換順序包含不存在的段落，或超過 64 段')
    if not sequence:
        sequence = list(by_key)
    sequence_version = _number(value.get('sequence_version', 1), 1, 2, '播放順序版本', True)
    if sequence_version == 2 and (len(sequence) != len(clips) or set(sequence) != set(by_key)):
        raise ValueError('固定循環順序必須包含每段動畫一次；請更新播放順序')
    shared_tone = _tone_values(value.get('shared_tone'), '整組')
    tone_match = _validate_tone_match(value.get('tone_match'), clips, sources, skip_tone)
    tonal_project = dict(id=pid,clips=clips,sequence=sequence,shared_tone=shared_tone,tone_match=tone_match)
    seam_closure=_validate_seams(value.get('seam_closure'),tonal_project,sources,skip_seams)
    if seam_closure is not None:
        tonal_project['seam_closure']=seam_closure
    reviews = {}
    raw_reviews = value.get('reviews', {})
    if not isinstance(raw_reviews, dict) or len(raw_reviews) > 256:
        raise ValueError('接點檢查記錄無效')
    for key, review in raw_reviews.items():
        if not isinstance(key, str) or not isinstance(review, dict) or review.get('verdict') not in ('pass', 'needs_work'):
            continue
        pair = key.split('>')
        if len(pair) != 2 or any(k not in by_key for k in pair):
            continue
        signature = pair_signature(by_key[pair[0]], by_key[pair[1]], sources, tonal_project)
        if review.get('signature') != signature:
            continue
        reviews[key] = dict(signature=signature, verdict=review['verdict'], note=str(review.get('note', ''))[:2000])
    result = dict(id=pid, name=_name(value.get('name', saved.get('name', '表情切換')), '表情切換'), clips=clips, reviews=reviews,
                  sequence=sequence[:], sequence_version=sequence_version, shared_tone=shared_tone,
                  created=saved.get('created', time.time()), updated=time.time())
    if tone_match is not None:
        result['tone_match'] = tone_match
    if seam_closure is not None:
        result['seam_closure']=seam_closure
    return result, sources


def project_signature(project, sources):
    return _hash(dict(id=project['id'], name=project['name'], clips=[dict(key=c['key'], label=c['label'], signature=clip_signature(c, sources[c['key']], project)) for c in project['clips']], sequence=project['sequence'], renderer_version=1))


def endpoint(clip, index):
    local = index - clip['start']
    count = clip['end'] - clip['start'] + 1
    if not 0 <= local < count:
        raise ValueError('影格不在段落範圍內')
    if clip['head_frames'] and local < clip['head_frames']:
        n = clip['head_frames']
        return clip['head'], 1 if n == 1 else 1 - local / (n - 1)
    if clip['tail_frames'] and local >= count - clip['tail_frames']:
        n = clip['tail_frames']
        return clip['tail'], 1 if n == 1 else (local - (count - n)) / (n - 1)
    return _AFFINE_DEFAULTS, 0


def _read_frame(source, index):
    path = source.files[index - 1]
    if _stamp(path) != source.stamps[index - 1]:
        raise ValueError('來源圖片已變更，請重新載入來源後再輸出')
    with Image.open(path) as im:
        if im.format != 'PNG' or im.size != source.dimensions:
            raise ValueError('來源 PNG 格式或畫布尺寸與任務記錄不符')
        return im.convert('RGBA')


def _alignment_binding(clip, source, side, raw=True, project=None):
    if side not in ('head','tail'):
        raise ValueError('對位首尾端點無效')
    index=clip['start'] if side=='head' else clip['end']
    affine,position=endpoint(clip,index)
    appearance=dict(raw=True,frame=index) if raw else dict(tone=clip['tone'],contrast=clip['contrast'],affine=affine,position=position)
    if not raw and project:
        appearance['tonal'] = _tonal_settings(project, clip)
    return dict(job_id=source.job_id,version=source.version,clip_key=clip['key'],side=side,frame=index,
                fingerprint=le.sa.sequence_fingerprint(source.job_id,source.version,source.files,source.dimensions),
                appearance_hash=le.sa.digest(appearance))


def _verify_source_alignment(clip,source,side):
    alignment=clip[side].get('alignment')
    if not le.sa.active(alignment):return
    model=alignment['model'];provenance=model['provenance']
    if provenance['scope']!='transition' or model['dimensions']!=list(source.dimensions):raise ValueError('對位來源或畫布已變更，請重新計算')
    le.sa.verify_binding(provenance['source'],_alignment_binding(clip,source,side))


def _verify_project_alignments(project,sources,skip=None):
    by_key={c['key']:c for c in project['clips']}
    for clip in project['clips']:
        for side in ('head','tail'):
            if skip==(clip['key'],side):continue
            alignment=clip[side].get('alignment')
            if not le.sa.active(alignment):continue
            _verify_source_alignment(clip,sources[clip['key']],side)
            reference=alignment['model']['provenance']['reference']
            key=reference['clip_key']
            if key not in by_key:raise ValueError('對位參考段落已移除，請重新計算或先停用對位')
            current=_alignment_binding(by_key[key],sources[key],reference['side'],raw=False)
            # The map represents the reference appearance at calculation time.
            # Later manual edits do not recursively invalidate snapshot maps.
            current['appearance_hash']=reference['appearance_hash']
            le.sa.verify_binding(reference,current)


def _render_base_frame(clip, source, index, max_size=None, project=None):
    im = _read_frame(source, index)
    affine, position = endpoint(clip, index)
    affine = copy.deepcopy(affine)
    aligned=le.sa.active(affine.get('alignment'))
    if aligned:
        side='head' if index-clip['start']<clip['head_frames'] else 'tail'
        _verify_source_alignment(clip,source,side)
    # Nonlinear curves must precede preview downsampling, just as they do for
    # the exported PNG. Applying curves to already mixed pixels changes tone.
    tonal = _tonal_settings(project, clip)
    im = le.adjust_tone(im, clip)
    im = le.adjust_tone(im, tonal['correction'])
    im = le.adjust_tone(im, tonal['shared'])
    if max_size and not aligned:
        original = im.size
        im.thumbnail(max_size, Image.Resampling.LANCZOS)
        affine['dx'] *= im.width / original[0]
        affine['dy'] *= im.height / original[1]
        local_region = affine.get('region') or {}
        if local_region.get('mode') == 'split':
            local_region['outside']['dx'] *= im.width / original[0]
            local_region['outside']['dy'] *= im.height / original[1]
    r = dict(affine, protect=False, px=50, py=50, radius=15, ramp=0, end=1)
    image,clipped=le.transform(im, r, position)
    if max_size and aligned:image.thumbnail(max_size,Image.Resampling.LANCZOS)
    return image,clipped


def render_frame(clip, source, index, max_size=None, project=None):
    if not ts.active(project):
        return _render_base_frame(clip,source,index,max_size,project)
    # Closure is last and runs at full output resolution, after every manual,
    # tone and alignment edit. Both sides therefore reuse the same RGBA bytes.
    image,clipped=_render_base_frame(clip,source,index,project=project)
    side,amount=ts.weight(clip,index)
    if amount>0:
        link=ts.link_for(project,clip['key'],side)
        anchor=ts.read_anchor(projectdir(project['id'])/'anchors',link)
        image=ts.lc.morph(image,anchor,amount)
        if amount>=1:
            clipped=False
    if max_size:
        image.thumbnail(max_size,Image.Resampling.LANCZOS)
    return image,clipped


def _feature(clip, source, index, project=None):
    signature = clip_signature(clip, source, project)
    key = (signature, index)
    with _FEATURE_LOCK:
        if key in _FEATURES:
            _FEATURES.move_to_end(key)
            return _FEATURES[key]
    image, _ = render_frame(clip, source, index, (128, 192), project)
    pixels = np.asarray(image, dtype=np.float32) / 255
    alpha = pixels[:, :, 3:4]
    value = np.concatenate([pixels[:, :, :3] * alpha, alpha], axis=2)
    with _FEATURE_LOCK:
        _FEATURES[key] = value
        _FEATURES.move_to_end(key)
        while len(_FEATURES) > 128:
            _FEATURES.popitem(last=False)
    return value


def metrics(a, b, sources, project=None):
    sa, sb = sources[a['key']], sources[b['key']]
    tail, before = _feature(a, sa, a['end'], project), _feature(a, sa, a['end'] - 1, project)
    head, after = _feature(b, sb, b['start'], project), _feature(b, sb, b['start'] + 1, project)
    region = np.maximum.reduce([tail[:, :, 3], before[:, :, 3], head[:, :, 3], after[:, :, 3]]) > .01
    if not region.any():
        return dict(silhouette=0.0, appearance=0.0, motion=0.0)
    jump_velocity = (head - tail) * float(Fraction(sa.fps))
    tail_velocity = (tail - before) * float(Fraction(sa.fps))
    head_velocity = (after - head) * float(Fraction(sb.fps))
    return dict(silhouette=round(float(np.mean(np.abs(tail[:, :, 3][region] - head[:, :, 3][region]))) * 100, 3),
                appearance=round(float(np.mean(np.abs(tail[:, :, :3][region] - head[:, :, :3][region]))) * 100, 3),
                motion=round(float((np.mean(np.abs(jump_velocity[region] - tail_velocity[region])) + np.mean(np.abs(head_velocity[region] - jump_velocity[region]))) / 2) * 100, 3))


def _pair(project, a, b):
    by_key = {c['key']: c for c in project['clips']}
    if not isinstance(a,str) or not isinstance(b,str) or a not in by_key or b not in by_key:
        raise ValueError('請選擇兩個已加入的動畫段落')
    return by_key[a], by_key[b]


def compare(project, sources, a, b):
    ca, cb = _pair(project, a, b)
    left, lc = render_frame(ca, sources[a], ca['end'], project=project)
    right, rc = render_frame(cb, sources[b], cb['start'], project=project)
    return dict(left=le.png64(left), right=le.png64(right), metrics=metrics(ca, cb, sources, project), signature=pair_signature(ca, cb, sources, project),
                clipped=lc or rc, clipped_frames=[dict(clip_key=c['key'], frame=index) for c, index, clipped in [(ca, ca['end'], lc), (cb, cb['start'], rc)] if clipped], dimensions=list(left.size),seam_closure=ts.status(project))


def analyze(project, sources, scope='all'):
    if scope not in ('all', 'sequence'):
        raise ValueError('接點分析範圍無效')
    if scope == 'sequence':
        order = project['sequence']
        by_key = {c['key']: c for c in project['clips']}
        pairs = [(by_key[a], by_key[b]) for a, b in dict.fromkeys(zip(order, order[1:]+order[:1]))]
    else:
        pairs = [(a,b) for a in project['clips'] for b in project['clips']]
    return dict(scope=scope, pairs=[dict(a=a['key'], b=b['key'], metrics=metrics(a, b, sources, project), signature=pair_signature(a, b, sources, project)) for a,b in pairs],
                metric_note='數字越低越接近。使用縮小畫面估算，動作按各來源 FPS 換算；請播放確認，分數不代表自動合格。')


def render_preview(project, sources, a, b, span=6):
    ca, cb = _pair(project, a, b)
    span = _number(span, 1, 12, '接縫預覽幀數', True)
    frames, clipped = [], False
    for c, end in [(ca, True), (cb, False)]:
        count = min(span, c['end'] - c['start'] + 1)
        indices = range(c['end'] - count + 1, c['end'] + 1) if end else range(c['start'], c['start'] + count)
        for index in indices:
            image, edge = render_frame(c, sources[c['key']], index, (720, 720), project)
            clipped |= edge
            frames.append(dict(image=le.png64(image), label=c['label'], clip_key=c['key'], frame=index, duration_ms=1000 / float(Fraction(sources[c['key']].fps))))
    return dict(frames=frames, dimensions=list(image.size), preview=True, clipped=clipped, source_dimensions=list(sources[a].dimensions),seam_closure=ts.status(project))


def _version_dir(pid, version):
    return _beneath(projectdir(pid) / 'versions', _component(version))


def _versions(pid):
    root = projectdir(pid) / 'versions'
    result = []
    for path in sorted(root.glob('*/status.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            status = _read_json(_beneath(root, path.parent.name + '/status.json'))
            with LOCK:
                live = RUNS.get(pid, {})
                if live.get('version') == path.parent.name:
                    status = copy.deepcopy(live)
            if status.get('state') == 'running' and not (live.get('version') == path.parent.name and live.get('state') == 'running'):
                status.update(state='error', phase='上次輸出中斷，已保留該版本檔案')
            if status.get('state') == 'complete':
                manifest = _read_json(path.parent / 'manifest.json')
                status.update(clips=manifest.get('clips', []), signature=manifest.get('signature'), sequence=manifest.get('sequence', []),seam_verification=manifest.get('seam_verification'))
            status.update(name=path.parent.name, version=path.parent.name)
            result.append(status)
        except (OSError, ValueError, TypeError):
            result.append(dict(name=path.parent.name, version=path.parent.name, state='error', phase='版本記錄無法讀取；檔案仍保留'))
    return result


def _set_status(pid, **kwargs):
    with LOCK:
        RUNS[pid].update(kwargs)


def _run_ffmpeg(args, log, pid, total, stage, after=''):
    started = time.time()
    progress = log.parent / 'ffmpeg_progress.txt'
    progress.write_text('', encoding='utf-8')
    with log.open('ab') as stream:
        command = [e.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y', '-progress', str(progress), '-nostats', *map(str, args)]
        stream.write((subprocess.list2cmdline(command) + '\n').encode('utf-8'))
        stream.flush()
        process = subprocess.Popen(command, stdout=stream, stderr=stream, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        while process.poll() is None:
            _set_status(pid, phase=stage, progress=sample(stage, read_frame(progress), total, started, after))
            time.sleep(.25)
    if process.returncode:
        raise RuntimeError('FFmpeg 處理失敗，詳見該版本 ffmpeg.log')


def _identity(clip, index, project=None):
    if ts.active(project) and ts.weight(clip,index)[1]>0:
        return False
    if any(v for values in _tonal_settings(project, clip).values() for v in values.values()):
        return False
    affine, position = endpoint(clip, index)
    if le.sa.active(affine.get('alignment')):
        return clip['tone']==clip['contrast']==0 and (position==0 or affine['alignment']['strength']==0)
    local_region = affine.get('region') or {}
    outside_identity = (local_region.get('mode') != 'split' or
                        all(local_region['outside'][key] == default for key,default in _AFFINE_DEFAULTS.items()))
    return clip['tone'] == clip['contrast'] == 0 and (position == 0 or
        (outside_identity and all(affine[k] == default for k, default in _AFFINE_DEFAULTS.items())))


def export_worker(project, sources, version, signature):
    pid = project['id']
    root = _version_dir(pid, version)
    started = time.time()
    exported = []
    try:
        _verify_project_alignments(project,sources)
        _verify_seams(project,sources)
        e.atomic_json(root / 'project_snapshot.json', project)
        if ts.active(project):
            anchors=root/'anchors'
            anchors.mkdir()
            for link in project['seam_closure']['links']:
                source=projectdir(pid)/'anchors'/(link['anchor_id']+'.png')
                content=source.read_bytes()
                if hashlib.sha256(content).hexdigest()!=link['sha256']:
                    raise ValueError('共同接點基準在輸出期間變更，請重新建立')
                (anchors/source.name).write_bytes(content)
        for clip_index, clip in enumerate(project['clips'], 1):
            source = sources[clip['key']]
            count = clip['end'] - clip['start'] + 1
            dest = root / f'{clip_index:02d}_{e.safe_name(clip["label"])[:48]}'
            png_dir = dest / 'transparent_png'
            png_dir.mkdir(parents=True)
            clipped = []
            stage = f'處理 PNG · {clip["label"]}（{clip_index}/{len(project["clips"])}）'
            stage_started = time.time()
            for seq, index in enumerate(range(clip['start'], clip['end'] + 1), 1):
                target = png_dir / f'frame_{seq:08d}.png'
                if _identity(clip, index, project):
                    image = _read_frame(source, index)
                    shutil.copy2(source.files[index - 1], target)
                    if _stamp(source.files[index - 1]) != source.stamps[index - 1]:
                        raise ValueError('來源圖片於輸出期間變更，請重新載入後再試')
                    edge = False
                else:
                    image, edge = render_frame(clip, source, index, project=project)
                    image.save(target, format='PNG')
                if image.size != source.dimensions:
                    raise ValueError('固定畫布尺寸驗證失敗')
                if edge:
                    clipped.append(index)
                _set_status(pid, phase=stage, clip_key=clip['key'], clip_index=clip_index, clip_count=len(project['clips']), progress=sample(stage, seq, count, stage_started, '此段影片合成與透明度驗證'))
            if clipped:
                raise ValueError(f'「{clip["label"]}」第 {", ".join(map(str, clipped[:12]))} 幀可能超出畫布；候選 PNG 已保留，請減少變形')
            _verify_project_alignments(project,sources)
            _verify_seams(project,sources)
            log, video = dest / 'ffmpeg.log', dest / 'animation.webm'
            _run_ffmpeg(['-framerate', source.fps, '-i', png_dir / 'frame_%08d.png', '-frames:v', count, '-an', '-c:v', 'libvpx-vp9', '-pix_fmt', 'yuva420p', '-lossless', '1', '-auto-alt-ref', '0', '-row-mt', '1', video], log, pid, count, '合成影片 · ' + clip['label'], '透明度驗證')
            raw = dest / 'verify_alpha.raw'
            _run_ffmpeg(['-c:v', 'libvpx-vp9', '-i', video, '-vf', 'alphaextract', '-f', 'rawvideo', '-pix_fmt', 'gray', raw], log, pid, count, '回讀透明度 · ' + clip['label'], '逐幀核對')
            width, height = source.dimensions
            size = width * height
            if raw.stat().st_size != size * count:
                raise ValueError('影片回讀幀數或畫布尺寸不符')
            maximum_error = 0
            verify_started = time.time()
            with raw.open('rb') as stream:
                for seq in range(1, count + 1):
                    actual = np.frombuffer(stream.read(size), dtype=np.uint8).astype(np.int16)
                    with Image.open(png_dir / f'frame_{seq:08d}.png') as image:
                        expected = np.asarray(image.convert('RGBA').getchannel('A')).reshape(-1).astype(np.int16)
                    maximum_error = max(maximum_error, int(np.abs(actual - expected).max()))
                    _set_status(pid, phase='逐幀核對 · ' + clip['label'], progress=sample('逐幀核對', seq, count, verify_started, '其餘段落' if clip_index < len(project['clips']) else '寫入版本記錄'))
            raw.unlink()
            if maximum_error > 2:
                raise ValueError('影片透明度回讀差異超過 2/255')
            prefix = dest.relative_to(projectdir(pid)).as_posix()
            item = dict(key=clip['key'], label=clip['label'], frames=count, fps=source.fps, dimensions=list(source.dimensions), png_pattern=prefix + '/transparent_png/frame_%08d.png', video_path=prefix + '/animation.webm', source_range=[clip['start'], clip['end']], alpha_max_error=maximum_error)
            e.atomic_json(dest / 'verification.json', item)
            exported.append(item)
        seam_verification=ts.verify_exports(project,exported,projectdir(pid),sources) if ts.active(project) else None
        manifest = dict(project_id=pid, name=project['name'], version=version, signature=signature, state='complete', clips=exported, sequence=project['sequence'], sequence_version=project.get('sequence_version',1), shared_tone=project.get('shared_tone',{}), tone_match=project.get('tone_match'), seam_closure=project.get('seam_closure'),seam_verification=seam_verification,reviews=project['reviews'], created=started)
        e.atomic_json(root / 'manifest.json', manifest)
        _set_status(pid, state='complete', phase='所有段落輸出完成，畫布與透明度已驗證', clips=exported, sequence=project['sequence'],seam_verification=seam_verification,elapsed=time.time() - started)
    except Exception as exc:
        _set_status(pid, state='error', phase=str(exc), clips=exported, elapsed=time.time() - started)
    finally:
        with LOCK:
            e.atomic_json(root / 'status.json', RUNS[pid])


def match_tone(project, sources, reference=None):
    if len(project['clips']) < 2:
        raise ValueError('請加入至少兩段動畫後再統一明暗')
    reference = reference or project['sequence'][0]
    by_key = {c['key']: c for c in project['clips']}
    if reference not in by_key:
        raise ValueError('請選擇組內的明暗參考動畫')
    samples, bindings, frame_indices = {}, {}, {}
    for clip in project['clips']:
        key = clip['key']
        source = sources[key]
        bindings[key] = _tone_binding(clip, source)
        indices = sorted(set(np.linspace(clip['start'], clip['end'], 5).round().astype(int).tolist()))
        frame_indices[key] = indices
        # Analyze manual tone from source, excluding previous auto curves and
        # group styling, so clicking Match again never compounds the result.
        samples[key] = [tt.sample_pixels(le.adjust_tone(_read_frame(source, index), clip)) for index in indices]
    target = tt.distribution(samples[reference])
    if target[4] - target[0] < 12:
        raise ValueError('參考動畫的中間調層次不足，請選擇明暗層次較完整的參考')
    results, adjustments = [], {}
    for clip in project['clips']:
        key = clip['key']
        if key == reference:
            result = dict(tone=0., contrast=0., before=tt.summary(target,target), after=tt.summary(target,target), warnings=[], samples=sum(len(p) for p in samples[key]))
        else:
            result = tt.fit(samples[key], target)
        result.update(key=key, label=clip['label'], frames=frame_indices[key])
        adjustments[key] = dict(tone=result['tone'], contrast=result['contrast'])
        results.append(result)
        # Source stamps can change while the CPU fit is running.
        if any(_stamp(path) != stamp for path, stamp in zip(sources[key].files, sources[key].stamps)):
            raise ValueError('來源圖片在校準期間變更，請更新來源後重新統一明暗')
    report = dict(reference=reference, reference_label=by_key[reference]['label'], clips=results,
                  note=tt.NOTE, unit='0–255 亮度，誤差為前景分位數平均差', created=time.time())
    project = copy.deepcopy(project)
    project['tone_match'] = dict(enabled=True, reference=reference, bindings=bindings, adjustments=adjustments, report=report)
    project['reviews'] = {}
    return dict(project=project, report=report)


def build_seams(project,sources,references=None):
    pairs=ts.pairs(project)
    ts.check_windows(project)
    references={} if references is None else references
    allowed={a+'>'+b for a,b in pairs}
    if not isinstance(references,dict) or any(key not in allowed or value not in ('a_tail','b_head') for key,value in references.items()):
        raise ValueError('共同接點參考選擇與目前播放順序不符')
    by_key={clip['key']:clip for clip in project['clips']}
    links,report=[],[]
    binding=_seam_binding(project,sources)
    for a,b in pairs:
        ca,cb=by_key[a],by_key[b]
        # Always sample the edited base pipeline, never an older closure.
        left,lc=_render_base_frame(ca,sources[a],ca['end'],project=project)
        right,rc=_render_base_frame(cb,sources[b],cb['start'],project=project)
        reference=references.get(a+'>'+b,'b_head')
        target,clipped=(left,lc) if reference=='a_tail' else (right,rc)
        if clipped:
            raise ValueError('共同接點的參考畫面可能超出畫布，請先修正該端變形')
        snapshot=ts.save_anchor(projectdir(project['id'])/'anchors',target)
        link=dict(a=a,b=b,reference=reference,**snapshot)
        links.append(link)
        difference=np.abs(np.array(left,dtype=np.int16)-np.array(right,dtype=np.int16))
        report.append(dict(link,before_max_channel_error=int(difference.max()),
            before_changed_pixels=int(np.any(difference,axis=2).sum()),
            note='前後兩端將使用相同完整 RGBA 基準；附近漸變仍需播放檢查'))
    project=copy.deepcopy(project)
    project['seam_closure']=dict(enabled=True,schema=1,sequence=list(project['sequence']),binding=binding,links=links,created=time.time())
    _verify_seams(project,sources)
    project['reviews']={}
    return dict(project=project,report=dict(links=report,note=ts.NOTE))


def handle(action, data):
    if not isinstance(data, dict):
        raise ValueError('請求內容無效')
    if action == 'catalog':
        return catalog()
    if action == 'create':
        pid = uuid.uuid4().hex
        root = projectdir(pid)
        root.mkdir(parents=True)
        now = time.time()
        project = dict(id=pid, name=_name(data.get('name', '表情切換'), '表情切換'), clips=[], reviews={}, sequence=[], sequence_version=2, shared_tone=dict(tone=0,contrast=0), created=now, updated=now)
        with LOCK:
            e.atomic_json(root / 'project.json', project)
        return project
    if action == 'list':
        projects = []
        for path in (e.ROOT / 'transition_projects').glob('*/project.json'):
            try:
                project = _read_json(path)
                _id(project['id'])
                projects.append(dict(id=project['id'], name=project['name'], clip_count=len(project.get('clips', [])), created=project.get('created'), updated=project.get('updated')))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return dict(projects=sorted(projects, key=lambda v: v.get('updated') or 0, reverse=True))
    if action in ('load', 'status', 'open'):
        pid = _id(data.get('project_id'))
        root = projectdir(pid)
        project = _read_json(root / 'project.json')
        if action == 'load':
            source_error = ''
            try:
                canonical, _ = validate_project(project)
                canonical['updated'] = project.get('updated', canonical['updated'])
                project = canonical
            except (OSError, ValueError, KeyError, TypeError) as exc:
                project = dict(project, reviews={})
                source_error = '來源需重新確認：' + str(exc)
            return dict(project=project, versions=_versions(pid), source_error=source_error)
        if action == 'status':
            with LOCK:
                if pid in RUNS:
                    return copy.deepcopy(RUNS[pid])
            versions = _versions(pid)
            return versions[0] if versions else dict(project_id=pid, state='idle', phase='尚未輸出')
        version = data.get('version')
        target = _version_dir(pid, version) if version else root
        if not target.is_dir():
            raise ValueError('找不到輸出資料夾')
        e.os.startfile(str(target))
        return dict(ok=True)
    if action not in ('save', 'compare', 'analyze', 'render_preview', 'export','align','match_tone','build_seams'):
        raise ValueError('未知表情切換操作')
    skip=None
    if action=='align':
        if data.get('side') not in ('head','tail'):raise ValueError('請選擇修正 A 尾幀或 B 首幀')
        skip=(data.get('a') if data['side']=='tail' else data.get('b'),data['side'])
    project, sources = validate_project(data.get('project'),skip_alignment=skip,skip_tone=action=='match_tone',skip_seams=action in ('build_seams','match_tone','align'))
    pid = project['id']
    if action=='align':
        ca,cb=_pair(project,data.get('a'),data.get('b'))
        side=data['side'];selected,reference=(ca,cb) if side=='tail' else (cb,ca)
        reference_side='head' if side=='tail' else 'tail'
        if not selected[side+'_frames']:raise ValueError('請先將所選首尾修正幀數設為大於零')
        selected_source=sources[selected['key']];reference_source=sources[reference['key']]
        source_binding=_alignment_binding(selected,selected_source,side)
        reference_binding=_alignment_binding(reference,reference_source,reference_side,raw=False,project=project)
        raw=_read_frame(selected_source,source_binding['frame'])
        target,clipped=_render_base_frame(reference,reference_source,reference_binding['frame'],project=project)
        if clipped:raise ValueError('參考畫面目前可能超出畫布，請先調整參考端修正')
        provenance=dict(scope='transition',source=source_binding,reference=reference_binding)
        alignment,report=le.sa.build_alignment(np.asarray(raw),np.asarray(target),provenance)
        le.sa.verify_binding(source_binding,_alignment_binding(selected,selected_source,side))
        le.sa.verify_binding(reference_binding,_alignment_binding(reference,reference_source,reference_side,raw=False,project=project))
        return dict(alignment=alignment,report=report)
    if action == 'save':
        with LOCK:
            e.atomic_json(projectdir(pid) / 'project.json', project)
        return project
    if action == 'match_tone':
        return match_tone(project, sources, data.get('reference'))
    if action == 'build_seams':
        return build_seams(project,sources,data.get('references'))
    if action == 'compare':
        return compare(project, sources, data.get('a'), data.get('b'))
    if action == 'analyze':
        return analyze(project, sources, data.get('scope','all'))
    if action == 'render_preview':
        return render_preview(project, sources, data.get('a'), data.get('b'), data.get('span', 6))
    if len(project['clips']) < 2:
        raise ValueError('請加入至少兩段動畫後再輸出')
    signature = project_signature(project, sources)
    with le.LOCK, LOCK:
        if e.ACTIVE or any(v.get('state') == 'running' for v in le.RUNS.values()) or any(v.get('state') == 'running' for v in RUNS.values()):
            raise ValueError('已有處理中的任務，請等待完成後再輸出')
        needed = sum((c['end'] - c['start'] + 1) * math.prod(sources[c['key']].dimensions) * 8 for c in project['clips'])
        if shutil.disk_usage(projectdir(pid)).free < needed + 512 * 1024**2:
            raise ValueError('磁碟空間不足')
        versions_root = projectdir(pid) / 'versions'
        versions_root.mkdir(exist_ok=True)
        for index in range(1, 100000):
            version = f'v{index:03d}'
            root = _version_dir(pid, version)
            try:
                root.mkdir()
                break
            except FileExistsError:
                continue
        else:
            raise ValueError('輸出版本過多')
        e.atomic_json(projectdir(pid) / 'project.json', project)
        RUNS[pid] = dict(project_id=pid, version=version, name=version, state='running', phase='準備輸出', signature=signature, started=time.time(), clip_count=len(project['clips']))
        e.atomic_json(root / 'status.json', RUNS[pid])
        threading.Thread(target=export_worker, args=(copy.deepcopy(project), sources, version, signature), daemon=True).start()
        return copy.deepcopy(RUNS[pid])
