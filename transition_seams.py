"""Immutable shared endpoints for an explicit multi-clip loop.

Every joint has its own RGBA snapshot. Endpoint equality is independent from
whether the surrounding optical-flow morph looks natural to a human viewer.
"""
import hashlib
import io
import math
from pathlib import Path
import re
import uuid

import numpy as np
from PIL import Image

import loop_closure as lc

NOTE = '每個接點共用一張完整 RGBA 基準，首尾漸變各自通往該基準；端點一致不代表附近動作自然，請以慢速播放檢查。'
_ID = re.compile(r'^[0-9a-f]{32}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')


def active(project):
    return isinstance(project, dict) and isinstance(project.get('seam_closure'), dict) and project['seam_closure'].get('enabled') is True


def pairs(project):
    order = project.get('sequence', [])
    keys = [clip['key'] for clip in project.get('clips', [])]
    if len(keys) < 2 or len(order) != len(keys) or len(set(order)) != len(keys) or set(order) != set(keys):
        raise ValueError('共同接點需要每段一次的完整固定循環；請先明確設定播放順序')
    return list(zip(order, order[1:] + order[:1]))


def check_windows(project):
    for clip in project['clips']:
        count = clip['end'] - clip['start'] + 1
        if min(clip['head_frames'], clip['tail_frames']) < 2:
            raise ValueError('共同接點的頭尾漸變各至少需要 2 幀，請調整每段的漸變幀數')
        if clip['head_frames'] + clip['tail_frames'] > count:
            raise ValueError('共同接點的頭尾漸變不能重疊')


def weight(clip, index):
    local = index - clip['start']
    count = clip['end'] - clip['start'] + 1
    if not 0 <= local < count:
        raise ValueError('影格不在段落範圍內')
    if clip['head_frames'] >= 2 and local < clip['head_frames']:
        value = 1 - local / (clip['head_frames'] - 1)
        return 'head', value * value * (3 - 2 * value)
    if clip['tail_frames'] >= 2 and local >= count - clip['tail_frames']:
        value = (local - (count - clip['tail_frames'])) / (clip['tail_frames'] - 1)
        return 'tail', value * value * (3 - 2 * value)
    return None, 0.


def _path(root, anchor_id):
    if not isinstance(anchor_id, str) or not _ID.fullmatch(anchor_id):
        raise ValueError('共同接點基準 ID 無效')
    root = Path(root).resolve()
    path = (root / (anchor_id + '.png')).resolve()
    if not path.is_relative_to(root):
        raise ValueError('共同接點基準路徑超出資料夾')
    return path


def read_anchor(root, link):
    try:
        payload = _path(root, link['anchor_id']).read_bytes()
    except OSError as exc:
        raise ValueError('共同接點基準檔案遺失，請重新建立共同接點或先停用') from exc
    if hashlib.sha256(payload).hexdigest() != link['sha256']:
        raise ValueError('共同接點基準已被修改，請重新建立共同接點或先停用')
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != 'PNG' or image.mode != 'RGBA' or list(image.size) != link['dimensions']:
                raise ValueError('共同接點基準格式或尺寸不符')
            return image.copy()
    except (OSError, SyntaxError) as exc:
        raise ValueError('共同接點基準無法讀取，請重新建立') from exc


def save_anchor(root, image):
    image = image.convert('RGBA')
    lc._validate_dimensions(*image.size)
    payload = io.BytesIO()
    image.save(payload, format='PNG')
    content = payload.getvalue()
    anchor_id = uuid.uuid4().hex
    Path(root).mkdir(parents=True, exist_ok=True)
    # Unique immutable snapshots are never overwritten by a later rebuild.
    with _path(root, anchor_id).open('xb') as stream:
        stream.write(content)
    return dict(anchor_id=anchor_id, sha256=hashlib.sha256(content).hexdigest(), dimensions=list(image.size))


def validate(value, project, binding, root, skip=False):
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get('enabled', False)) is not bool:
        raise ValueError('共同接點設定無效')
    if value.get('schema', 1) != 1:
        raise ValueError('共同接點設定版本不支援')
    links = value.get('links', [])
    order = value.get('sequence', [])
    if not isinstance(links, list) or len(links) > 16 or not isinstance(order, list) or len(order) > 16:
        raise ValueError('共同接點記錄無效')
    result = dict(enabled=value.get('enabled', False), schema=1, sequence=list(order), binding=value.get('binding',''), links=[])
    if 'created' in value:
        if isinstance(value['created'], bool) or not isinstance(value['created'], (int,float)) or not math.isfinite(value['created']):
            raise ValueError('共同接點建立時間無效')
        result['created'] = value['created']
    for raw in links:
        if not isinstance(raw, dict) or any(not isinstance(raw.get(key),str) or not _ID.fullmatch(raw[key]) for key in ('a','b','anchor_id')):
            raise ValueError('共同接點記錄 ID 無效')
        if raw.get('reference') not in ('a_tail','b_head') or not isinstance(raw.get('sha256'),str) or not _SHA.fullmatch(raw['sha256']):
            raise ValueError('共同接點參考記錄無效')
        dimensions = raw.get('dimensions')
        if not isinstance(dimensions,list) or len(dimensions)!=2 or any(type(n) is not int for n in dimensions):
            raise ValueError('共同接點基準尺寸無效')
        lc._validate_dimensions(*dimensions)
        result['links'].append({key:raw[key] for key in ('a','b','reference','anchor_id','sha256','dimensions')})
    if result['enabled'] and not skip:
        expected = pairs(project)
        check_windows(project)
        if result['sequence'] != project['sequence'] or [(link['a'],link['b']) for link in result['links']] != expected or result['binding'] != binding:
            raise ValueError('來源、範圍、明暗、變形或順序已變更，請重新建立共同接點或先停用')
        if len({link['anchor_id'] for link in result['links']}) != len(expected):
            raise ValueError('每個接點必須使用各自的共同基準，請重新建立')
        for link in result['links']:
            read_anchor(root, link)
    return result


def link_for(project, key, side):
    field = 'b' if side == 'head' else 'a'
    values = [link for link in project['seam_closure']['links'] if link[field] == key]
    if len(values) != 1:
        raise ValueError('共同接點無法確定此段的唯一基準，請重新建立')
    return values[0]


def status(project):
    value = project.get('seam_closure') or {}
    return dict(enabled=value.get('enabled') is True, links=value.get('links',[]), note=NOTE)


def verify_exports(project, exported, root, sources):
    by_key = {item['key']:item for item in exported}
    def read(key,index):
        item=by_key[key]
        with Image.open(Path(root)/(item['png_pattern']%index)) as image:
            if list(image.size) != item['dimensions']:
                raise ValueError('共同接點成品 PNG 尺寸不符')
            return np.array(image.convert('RGBA'))
    def delta(a,b):
        return round(float(np.mean(np.abs(lc._associated(a)-lc._associated(b)))),6)
    report=[]
    for link in project['seam_closure']['links']:
        a,b=link['a'],link['b']
        acount=by_key[a]['frames']
        tail,head=read(a,acount),read(b,1)
        if tail.shape!=head.shape:
            raise ValueError('共同接點成品畫布尺寸不同')
        diff=np.abs(tail.astype(np.int16)-head.astype(np.int16))
        equal=not np.any(diff)
        # FPS equality is checked by rational values rather than display strings.
        from fractions import Fraction
        same_fps=Fraction(sources[a].fps)==Fraction(sources[b].fps)
        report.append(dict(a=a,b=b,anchor_id=link['anchor_id'],endpoints_equal=bool(equal),
            max_channel_error=int(diff.max()),changed_pixels=int(np.any(diff,axis=2).sum()),
            tail_adjacent_delta=delta(read(a,acount-1),tail),head_adjacent_delta=delta(head,read(b,2)),
            same_fps=same_fps,can_skip_duplicate_head=bool(equal and same_fps),dimensions=list(tail.shape[1::-1])))
        if not equal:
            raise ValueError('共同接點輸出驗證失敗：實際 PNG 首尾不一致，候選檔案已保留')
    return dict(enabled=True,pairs=report,all_equal=True,note=NOTE)
