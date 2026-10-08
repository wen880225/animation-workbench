"""Immutable shared endpoints for single loops and explicit clip routes.

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


def route_mode(project):
    value = project.get('route_mode', 'loop')
    if value not in ('loop', 'open'):
        raise ValueError('播放路線必須是循環或播完停止')
    return value


def route_pairs(project):
    """Boundary pairs in playback order, including legacy repeated/subset routes."""
    order = project.get('sequence', [])
    mode = route_mode(project)
    return list(zip(order, order[1:] + (order[:1] if mode == 'loop' else [])))


def pairs(project):
    order = project.get('sequence', [])
    keys = [clip['key'] for clip in project.get('clips', [])]
    if not keys or len(order) != len(keys) or len(set(order)) != len(keys) or set(order) != set(keys):
        raise ValueError('共同接點需要每段一次的完整固定循環或開鏈；請先明確設定播放順序')
    return route_pairs(project)


def check_windows(project, selected=None):
    selected = pairs(project) if selected is None else selected
    required = {(a, 'tail') for a, b in selected} | {(b, 'head') for a, b in selected}
    for clip in project['clips']:
        count = clip['end'] - clip['start'] + 1
        if any((clip['key'], side) in required and clip[side + '_frames'] < 2 for side in ('head', 'tail')):
            raise ValueError('共同接點所連接的頭尾漸變各至少需要 2 幀，請調整漸變幀數')
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


def registration(value):
    if not isinstance(value,dict) or set(value) != {'kind','head','tail'} or value['kind'] != 'similarity':
        raise ValueError('幾何接點參數無效，請重新分析')
    result = dict(kind='similarity')
    for side in ('head','tail'):
        rows = value[side]
        if not isinstance(rows,list) or len(rows)>512:
            raise ValueError('幾何接點過渡表超出範圍，請重新分析')
        result[side] = []
        for row in rows:
            if not isinstance(row,dict) or set(row) != {'frame','dx','dy','scale'} or type(row['frame']) is not int or row['frame'] < 1:
                raise ValueError('幾何接點影格記錄無效，請重新分析')
            clean = dict(frame=row['frame'])
            for name,low,high in (('dx',-8.1,8.1),('dy',-8.1,8.1),('scale',.97999,1.02001)):
                number = row[name]
                if isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number) or not low <= number <= high:
                    raise ValueError('幾何接點修正量超出範圍，請重新分析')
                clean[name] = float(number)
            result[side].append(clean)
    return result


def validate(value, project, binding, root, skip=False):
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get('enabled', False)) is not bool:
        raise ValueError('共同接點設定無效')
    schema = value.get('schema', 1)
    if schema not in (1, 2, 3):
        raise ValueError('共同接點設定版本不支援')
    links = value.get('links', [])
    order = value.get('sequence', [])
    if not isinstance(links, list) or len(links) > 16 or not isinstance(order, list) or len(order) > 16:
        raise ValueError('共同接點記錄無效')
    result = dict(enabled=value.get('enabled', False), schema=schema, sequence=list(order), route_mode=route_mode(value), binding=value.get('binding',''), links=[])
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
        link = {key:raw[key] for key in ('a','b','reference','anchor_id','sha256','dimensions')}
        if 'registration' in raw:
            if schema != 3 or raw['reference'] != 'b_head':
                raise ValueError('幾何接點版本或參考端不符，請重新分析')
            link['registration'] = registration(raw['registration'])
        result['links'].append(link)
    if result['enabled'] and not skip:
        expected = pairs(project)
        actual = [(link['a'], link['b']) for link in result['links']]
        if schema in (2,3):
            if not actual or len(set(actual)) != len(actual) or any(pair not in expected for pair in actual):
                raise ValueError('部分共同接點不符合目前路線，請重新分析')
            expected = [pair for pair in expected if pair in actual]
        check_windows(project, expected)
        if result['route_mode'] != route_mode(project) or result['sequence'] != project['sequence'] or actual != expected or result['binding'] != binding:
            raise ValueError('來源、範圍、明暗、變形或順序已變更，請重新建立共同接點或先停用')
        if len({link['anchor_id'] for link in result['links']}) != len(expected):
            raise ValueError('每個接點必須使用各自的共同基準，請重新建立')
        clips = {clip['key']:clip for clip in project['clips']}
        for link in result['links']:
            if 'registration' in link:
                for side,key in (('tail',link['a']),('head',link['b'])):
                    clip = clips[key]
                    expected_indices = (list(range(clip['end']-clip['tail_frames']+2,clip['end']+1)) if side == 'tail'
                                        else list(range(clip['start'],clip['start']+clip['head_frames']-1)))
                    if [row['frame'] for row in link['registration'][side]] != expected_indices:
                        raise ValueError('幾何接點的範圍或過渡幀數已變更，請重新分析')
            read_anchor(root, link)
    return result


def link_for(project, key, side):
    field = 'b' if side == 'head' else 'a'
    values = [link for link in project['seam_closure']['links'] if link[field] == key]
    if not values and project['seam_closure'].get('schema') in (2,3):
        return None
    if not values and route_mode(project) == 'open':
        order = project.get('sequence', [])
        if order and ((side == 'head' and key == order[0]) or (side == 'tail' and key == order[-1])):
            return None
    if len(values) != 1:
        raise ValueError('共同接點無法確定此段的唯一基準，請重新建立')
    return values[0]


def frame_registration(link, side, index):
    value = link.get('registration')
    if value is None:return None
    matches = [row for row in value[side] if row['frame'] == index]
    if len(matches) != 1:
        raise ValueError('幾何接點沒有此影格的參數，請重新分析')
    return matches[0]


def status(project):
    value = project.get('seam_closure') or {}
    return dict(enabled=value.get('enabled') is True, schema=value.get('schema',1), links=value.get('links',[]), route_mode=route_mode(project), note=NOTE)


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
    links={(link['a'],link['b']):link for link in project['seam_closure']['links']}
    for a,b in pairs(project):
        link=links.get((a,b))
        acount=by_key[a]['frames']
        tail,head=read(a,acount),read(b,1)
        if tail.shape!=head.shape:
            raise ValueError('共同接點成品畫布尺寸不同')
        diff=np.abs(tail.astype(np.int16)-head.astype(np.int16))
        equal=not np.any(diff)
        # FPS equality is checked by rational values rather than display strings.
        from fractions import Fraction
        same_fps=Fraction(sources[a].fps)==Fraction(sources[b].fps)
        report.append(dict(a=a,b=b,anchor_id=link['anchor_id'] if link else None,corrected=bool(link),endpoints_equal=bool(equal),
            max_channel_error=int(diff.max()),changed_pixels=int(np.any(diff,axis=2).sum()),
            tail_adjacent_delta=delta(read(a,acount-1),tail),head_adjacent_delta=delta(head,read(b,2)),
            same_fps=same_fps,can_skip_duplicate_head=bool(equal and same_fps),dimensions=list(tail.shape[1::-1])))
        if link and not equal:
            raise ValueError('共同接點輸出驗證失敗：實際 PNG 首尾不一致，候選檔案已保留')
    return dict(enabled=True,pairs=report,all_equal=all(row['endpoints_equal'] for row in report),
                route_mode=route_mode(project),schema=project['seam_closure'].get('schema',1),note=NOTE)
