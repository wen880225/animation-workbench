"""Conservative, non-destructive finishing transactions over existing renderers.

Rules measure small, well-bounded differences. They do not certify animation
quality or infer whether a character's expression/motion is semantically right.
"""
import copy
import hashlib
from collections import OrderedDict
import math
import threading
import time
import uuid

import numpy as np
from scipy import ndimage
from PIL import Image

import transitions as t

_PLANS = OrderedDict()
_LOCK = threading.RLock()
TTL = 15 * 60
LIMITS = dict(tone=8, contrast=5, appearance_error=1.5, silhouette=1.5,
              appearance=1.5, motion=8, iou=.985, centroid=.005,
              foreground_samples=512, changed_detail_ratio=.005, crop_fraction=.10,
              window_mean_delta=1.5, window_large_delta_ratio=.005)
REGISTRATION_LIMITS = dict(translation_fraction=.006, translation_min_px=3., translation_max_px=8.,
                           scale_fraction=.02, displacement_px=12., improvement=.30,
                           residual_mean=1.5, residual_detail_ratio=.005, local_residual_ratio=.12)
NOTE = '保守規則只篩選可小幅修正的差異；不是語義判斷或合格保證。套用後仍需播放驗收。'


def _active_alignment(clip):
    return any((clip.get(side, {}).get('alignment') or {}).get('enabled') for side in ('head', 'tail'))


def _review(kind, code, reason, **fields):
    return dict(kind=kind, code=code, status='REVIEW', reason=reason, **fields)


def _prepare_generated(project, sources):
    """Propose retiring invalid derived records without changing the draft."""
    prepared, repairs = copy.deepcopy(project), []
    for kind, label in (('tone_match', '明暗校準'), ('seam_closure', '共同接點')):
        correction = prepared.get(kind) or {}
        if not correction.get('enabled'):
            continue
        try:
            if kind == 'tone_match':
                t._validate_tone_match(correction, prepared['clips'], sources)
            else:
                t._validate_seams(correction, prepared, sources)
        except ValueError as exc:
            # Initial validation already checked record structure and sources.
            # Keep the historical record disabled, so it remains recoverable.
            correction['enabled'] = False
            repairs.append(dict(kind=kind, status='FIX', code='stale_generated_correction',
                                reason='停用過期' + label + '並重新計算', detail=str(exc)))
    if repairs:
        prepared['reviews'] = {}
    return prepared, repairs


def _appearance(project, sources):
    rows, reviews, corrected = [], [], copy.deepcopy(project)
    if len(project['clips']) < 2:
        return corrected, rows, reviews, 0
    if t.ts.active(project) or project.get('tone_match', {}).get('enabled') or any(_active_alignment(c) for c in project['clips']):
        reason = '保留既有對位、共同接點或明暗校準；本次不覆寫其設定'
        reviews.append(_review('appearance', 'existing_correction', reason))
        return corrected, rows, reviews, 0
    try:
        match = t.match_tone(project, sources)
    except (ValueError, OSError) as exc:
        reviews.append(_review('appearance', 'insufficient_appearance_data', str(exc)))
        return corrected, rows, reviews, 0
    candidate = match['project']['tone_match']
    accepted = 0
    for result in candidate['report']['clips']:
        key = result['key']
        before, after = result['before']['error'], result['after']['error']
        warnings = result.get('warnings', [])
        delta = before - after
        reference = key == candidate['reference']
        safe = (not warnings and result.get('samples', 0) >= LIMITS['foreground_samples']
                and abs(result['tone']) <= LIMITS['tone'] and abs(result['contrast']) <= LIMITS['contrast']
                and after <= LIMITS['appearance_error'] and delta >= .5 and after <= before * .8)
        status = 'OK' if reference or before <= .5 else 'FIX' if safe else 'REVIEW'
        code = 'reference' if reference else 'already_close' if status == 'OK' else 'small_appearance_correction' if safe else 'appearance_outside_safe_range'
        reason = '參考動畫' if reference else '明暗已接近' if status == 'OK' else '套用小幅固定明暗曲線' if safe else '差異、樣本或建議修正量超出保守範圍'
        row = dict(clip_id=key, label=result['label'], status=status, code=code, reason=reason,
                   tone=result['tone'], contrast=result['contrast'], before=result['before'], after=result['after'], warnings=warnings)
        rows.append(row)
        result['applied'] = status == 'FIX'
        if status == 'FIX':
            accepted += 1
        else:
            candidate['adjustments'][key] = dict(tone=0., contrast=0.)
            if status == 'REVIEW':
                result['proposal'] = dict(tone=result['tone'], contrast=result['contrast'], after=copy.deepcopy(result['after']))
            result.update(tone=0., contrast=0., after=copy.deepcopy(result['before']))
        if status == 'REVIEW':
            reviews.append(_review('appearance', code, reason, clip_id=key, label=result['label']))
    if accepted:
        candidate['report']['adjusted_count'] = accepted
        candidate['report']['note'] = '僅套用計畫列為 FIX 的小幅校準；REVIEW 段落保留原設定。'
        corrected['tone_match'] = candidate
    return corrected, rows, reviews, accepted


def _geometry(project, sources, a, b):
    left, lc = t.render_frame(a, sources[a['key']], a['end'], project=project)
    right, rc = t.render_frame(b, sources[b['key']], b['start'], project=project)
    return _image_geometry(left, right, lc or rc)


def _image_geometry(left, right, clipped=False):
    equal = np.array_equal(left, right)
    left, right = left.copy(), right.copy()
    left.thumbnail((256, 256))
    right.thumbnail((256, 256))
    x, y = np.asarray(left, dtype=np.float32), np.asarray(right, dtype=np.float32)
    ma, mb = x[:, :, 3] >= 128, y[:, :, 3] >= 128
    union, overlap = ma | mb, ma & mb
    foreground = min(int(ma.sum()), int(mb.sum()))
    iou = float(overlap.sum() / max(1, union.sum()))
    centroid = float(np.linalg.norm(np.array(ndimage.center_of_mass(ma)) - np.array(ndimage.center_of_mass(mb))) / math.hypot(*ma.shape)) if foreground else 1.
    area_ratio = float(ma.sum() / max(1, mb.sum()))
    def components(mask):
        labels, _ = ndimage.label(mask)
        sizes = np.bincount(labels.ravel())[1:]
        return int(np.sum(sizes >= 4))
    topology_same = (components(ma), components(ndimage.binary_fill_holes(ma) & ~ma)) == (components(mb), components(ndimage.binary_fill_holes(mb) & ~mb))
    detail = np.max(np.abs(x[:, :, :3] - y[:, :, :3]), axis=2)
    changed_detail_ratio = float(np.sum((detail > 24) & overlap) / max(1, overlap.sum()))
    return dict(iou=round(iou, 6), centroid=round(centroid, 6), area_ratio=round(area_ratio, 6),
                foreground_samples=foreground, topology_same=topology_same,
                changed_detail_ratio=round(changed_detail_ratio, 6), clipped=bool(clipped), endpoints_equal=bool(equal))


def _failure(code, label, value, limit, description, stage='boundary'):
    return dict(code=code, label=label, value=value, limit=limit, description=description, stage=stage)


def _boundary_failures(value, guard, motion=True):
    checks = []
    for code, label, actual, limit, passes, description in (
        ('clipped', '畫布裁切', guard['clipped'], False, not guard['clipped'], '修正不能遺失畫布邊緣內容'),
        ('foreground_samples', '有效角色區域', guard['foreground_samples'], 512, guard['foreground_samples'] >= 512, '透明背景不列入匹配樣本'),
        ('topology_changed', '輪廓結構', guard['topology_same'], True, guard['topology_same'], '新增或消失的輪廓區塊需要檢查'),
        ('silhouette_iou', '輪廓重合率', guard['iou'], .985, guard['iou'] >= .985, '對位後仍須有足夠輪廓重合'),
        ('centroid', '中心偏差', guard['centroid'], .005, guard['centroid'] <= .005, '相對於畫布對角線的偏差'),
        ('area_ratio', '角色面積比例', guard['area_ratio'], [.96, 1.04], .96 <= guard['area_ratio'] <= 1.04, '尺寸修正不能掩蓋大幅輪廓改變'),
        ('detail_residual', '細節殘差比例', guard['changed_detail_ratio'], .005, guard['changed_detail_ratio'] <= .005, '註冊後仍有明顯差異的角色像素比例')):
        if not passes:
            checks.append(_failure(code, label, actual, limit, description))
    for name, label in (('silhouette', '透明輪廓差異'), ('appearance', '外觀差異'), ('motion', '接點動作差異')):
        if name == 'motion' and not motion:
            continue
        if value[name] > LIMITS[name]:
            checks.append(_failure(name, label, value[name], LIMITS[name], '影像差分指標；不是語義品質評分'))
    return checks


def _manual_transform(clip, side):
    values = clip[side]
    return (any(values.get(key, default) != default for key, default in t._AFFINE_DEFAULTS.items())
            or bool(values.get('region') and values['region'].get('mode') != 'all'))


def _similarity_image(image, transform):
    values = dict(t._AFFINE_DEFAULTS, dx=transform['dx'], dy=transform['dy'],
                  sx=transform['scale']*100, sy=transform['scale']*100,
                  protect=False, px=50, py=50, radius=15, ramp=0, end=1)
    return t.le.transform(image, values, 1)


def _similarity_failures(image, transform, clipped=False, stage='registration'):
    w, h = image.size
    allowed = min(REGISTRATION_LIMITS['translation_max_px'], max(REGISTRATION_LIMITS['translation_min_px'],
                  math.hypot(w, h)*REGISTRATION_LIMITS['translation_fraction']))
    distance = math.hypot(transform['dx'], transform['dy'])
    scale_delta = abs(transform['scale']-1)
    alpha = np.asarray(image)[:, :, 3]
    yy, xx = np.where(alpha > 2)
    movement = 0.
    if len(xx):
        movement = float(np.hypot((xx-(w-1)/2)*(transform['scale']-1)+transform['dx'],
                                  (yy-(h-1)/2)*(transform['scale']-1)+transform['dy']).max())
    contact = bool(np.any(alpha[[0,-1], :] > 2) or np.any(alpha[:, [0,-1]] > 2))
    failures = []
    for code, label, actual, limit, passes, description in (
        ('translation_limit', '平移量', round(distance, 5), round(allowed, 5), distance <= allowed + .05, '只自動採用少量整體位移'),
        ('scale_limit', '尺寸修正量', round(scale_delta, 6), .02, scale_delta <= .02001, '只自動採用 2% 以內的等比例尺寸修正'),
        ('displacement_limit', '最大像素位移', round(movement, 5), 12, movement <= 12, '包含縮放造成的角色外緣位移'),
        ('canvas_edge_risk', '貼邊內容', contact and movement > .05, False, not contact or movement <= .05, '貼邊角色不可用整體搬移或收窄換取空白；需要保留邊界的候選'),
        ('clipped', '畫布裁切', bool(clipped), False, not clipped, '候選變形將內容移出畫布')):
        if not passes:
            failures.append(_failure(code, label, actual, limit, description, stage))
    return failures


def _residual(left, right):
    """Compare registered content at the same sampling bandwidth.

    A subpixel bilinear warp changes sharp-line samples, even for an exact
    correspondence. Symmetric subpixel smoothing separates that sampling error
    from missing/new content; alpha topology and local residual remain guarded.
    """
    a, b = t.ts.lc._associated(np.asarray(left)), t.ts.lc._associated(np.asarray(right))
    a = ndimage.gaussian_filter(a, (.8, .8, 0))
    b = ndimage.gaussian_filter(b, (.8, .8, 0))
    region = (a[:, :, 3] > 128) & (b[:, :, 3] > 128)
    difference = np.abs(a-b)
    high = (difference.max(axis=2) > 24) & region
    mean = float(difference[region].mean()) if region.any() else 255.
    ratio = float(high.sum()/max(1, region.sum()))
    # Concentrated damage must not disappear in a large foreground average.
    count = ndimage.uniform_filter(region.astype(float), size=16, mode='constant')
    local = ndimage.uniform_filter(high.astype(float), size=16, mode='constant')
    valid = count >= .75
    hotspot = float(np.max(np.divide(local, count, out=np.zeros_like(local), where=valid)))
    native_a,native_b=np.asarray(left)[:,:,3]>64,np.asarray(right)[:,:,3]>64
    unsupported=((native_a & (ndimage.distance_transform_edt(~native_b)>1.25))
                 | (native_b & (ndimage.distance_transform_edt(~native_a)>1.25)))
    return dict(mean=round(mean, 6), detail_ratio=round(ratio, 6), local_ratio=round(hotspot, 6),
                unsupported_alpha_pixels=int(unsupported.sum()))


def _residual_failures(residual, stage='registration'):
    result = []
    for key, label, limit in (('mean', '對位後內容差異', 1.5), ('detail_ratio', '對位後細節差異', .005),
                               ('local_ratio', '局部視窗差異', .12),
                               ('unsupported_alpha_pixels','尚未對上的輪廓像素',3)):
        if residual[key] > limit:
            result.append(_failure('registered_'+key, label, residual[key], limit,
                                   ('位置、形狀或透明度差異都可能造成，不能單憑此值判定缺件' if key == 'unsupported_alpha_pixels'
                                    else '局部 16×16 視窗的差異比例，不是整體角色百分比' if key == 'local_ratio'
                                    else '此次對位後仍有差異；不能單憑此值判斷是形變、正常動作或缺損'), stage))
    return result


def _registered_window(original, candidate, sources, a, b):
    """Fit and verify the exact deterministic renderer on every fade frame."""
    frames=max(0,a['tail_frames']-1)+max(0,b['head_frames']-1)
    if frames>512:
        return dict(safe=False,checked_frames=0,failed_checks=[_failure('registration_window_budget','過渡分析範圍',frames,512,
                    '此接點的過渡過長，請減少過渡幀數後再分析','window')])
    original_clips = {c['key']: c for c in original['clips']}
    anchor, edge = t._render_base_frame(b, sources[b['key']], b['start'], project=candidate)
    checked, worst = 0, dict(mean=0., detail_ratio=0., local_ratio=0.,unsupported_alpha_pixels=0)
    registration = dict(kind='similarity',tail=[],head=[])
    for clip, side in ((a, 'tail'), (b, 'head')):
        indices = (range(clip['end']-clip['tail_frames']+1, clip['end']+1) if side == 'tail'
                   else range(clip['start'], clip['start']+clip['head_frames']))
        for index in indices:
            actual_side, amount = t.ts.weight(clip, index)
            if actual_side != side or amount <= 0:
                continue
            source, old_edge = t._render_base_frame(original_clips[clip['key']], sources[clip['key']], index, project=original)
            transform = t.le.sa.estimate_similarity(np.asarray(source), np.asarray(anchor))
            registered, moved_edge = _similarity_image(source, transform)
            failures = _similarity_failures(source, transform, edge or old_edge or moved_edge, 'window')
            # Residual is evaluated after removing measured geometry. It is not
            # an allowance to replace a moving arm or to erase a new detail.
            guard = _image_geometry(registered, anchor)
            residual = _residual(registered, anchor)
            failures += _residual_failures(residual, 'window')
            final = t.le.sa.similarity_morph(source,anchor,transform,amount)
            partial = dict(scale=1+amount*(transform['scale']-1),
                           dx=amount*transform['dx'],dy=amount*transform['dy'])
            relocated, partial_edge = _similarity_image(source,partial)
            actual_residual = _residual(relocated,final)
            failures += _residual_failures(actual_residual,'window')
            if partial_edge:
                failures.append(_failure('window_clipped','過渡畫布裁切',True,False,'幾何過渡不可遺失畫布內容','window'))
            for name, actual, limit, good in (('iou',guard['iou'],.985,guard['iou']>=.985),
                                             ('topology',guard['topology_same'],True,guard['topology_same'])):
                if not good:
                    failures.append(_failure('window_'+name, '過渡幀輪廓', actual, limit, '整個過渡範圍都必須保留輪廓結構', 'window'))
            checked += 1
            worst = {k:max(worst[k],residual[k]) for k in worst}
            if failures:
                return dict(safe=False, checked_frames=checked, clip_id=clip['key'], label=clip['label'],
                            frame=index, residual=residual, failed_checks=failures)
            registration[side].append(dict(frame=index,**{k:transform[k] for k in ('dx','dy','scale')}))
    return dict(safe=True, checked_frames=checked, residual=worst, failed_checks=[],registration=registration)


def _similarity_candidate(project, sources, a, b, value, guard):
    left, left_edge = t._render_base_frame(a, sources[a['key']], a['end'], project=project)
    right, right_edge = t._render_base_frame(b, sources[b['key']], b['start'], project=project)
    transform = t.le.sa.estimate_similarity(np.asarray(left), np.asarray(right))
    warped, moved_edge = _similarity_image(left, transform)
    failures = _similarity_failures(left, transform, left_edge or right_edge or moved_edge)
    candidate = copy.deepcopy(project)
    ca = next(c for c in candidate['clips'] if c['key'] == a['key'])
    cb = next(c for c in candidate['clips'] if c['key'] == b['key'])
    ca['tail'].update(dx=transform['dx'], dy=transform['dy'], sx=transform['scale']*100, sy=transform['scale']*100)
    after_value = t.metrics(ca, cb, sources, candidate)
    after_guard = _geometry(candidate, sources, ca, cb)
    residual = _residual(warped, right)
    # Motion at an unclosed boundary is not the final transition. Every actual
    # morphed frame is checked below, rather than reusing unregistered velocity.
    geometry_checks = _boundary_failures(dict(silhouette=0, appearance=0, motion=0),
                                        dict(after_guard,changed_detail_ratio=residual['detail_ratio']),motion=False)
    failures += geometry_checks + _residual_failures(residual)
    before_residual = _residual(left, right)
    improvement = 1-residual['mean']/max(before_residual['mean'], 1e-9)
    if improvement < REGISTRATION_LIMITS['improvement']:
        failures.append(_failure('registration_improvement', '對位改善幅度', round(improvement, 6), .30,
                                 '候選必須有明顯改善，不能把不同內容誤當成位置偏差', 'registration'))
    window = None
    if not failures:
        window = _registered_window(project, candidate, sources, ca, cb)
        failures += window['failed_checks']
    rejected = any(c['code'] in ('translation_limit','scale_limit','displacement_limit','canvas_edge_risk','clipped') for c in failures)
    summary = dict(kind='similarity', status='rejected' if rejected else 'review' if failures else 'safe',
                   transform={k:transform[k] for k in ('dx','dy','scale')},
                   before=dict(metrics=value, geometry=guard, residual=before_residual),
                   after=dict(metrics=after_value, geometry=after_guard, residual=residual),
                   improvement=round(improvement, 6), window=window)
    return candidate, summary, failures, window


LOCAL_STRENGTHS = (100, 85, 70, 55, 40, 25)


def _local_map_failures(stats, limit, stage='registration'):
    checks=[]
    for code,label,actual,bound,good in (
        ('local_displacement','角色及採樣範圍最大位移',stats['support_max_px'],limit,stats['support_max_px']<=limit),
        ('local_jacobian','局部擠壓保護',stats['jacobian_min'],.65,stats['jacobian_min']>=.65),
        ('local_expansion','局部伸展保護',stats['jacobian_max'],1.5,stats['jacobian_max']<=1.5)):
        if not good:
            checks.append(_failure(code,label,round(actual,6),bound,'量測有效角色與其映射採樣支援範圍；全畫布折返與越界硬保護仍然有效',stage))
    return checks


def _local_temporal_support(project,sources,a,endpoint,provenance):
    """Explain each original fade frame before adopting a common endpoint.

    This is measured once, independently of strength. A new intermediate object
    cannot pass just because a fixed endpoint field is mathematically smooth.
    """
    cache={};checked=0
    if a['tail_frames']>512:
        return dict(safe=False,checked_frames=0,failed_checks=[_failure('local_window_budget','局部過渡分析範圍',a['tail_frames'],512,'過渡範圍過長，請縮短後重新分析','window')])
    limit=min(12.,max(3.,math.hypot(*endpoint.size)*.012))
    for index in range(a['end']-a['tail_frames']+2,a['end']+1):
        frame,edge=t._render_base_frame(a,sources[a['key']],index,project=project)
        key=hashlib.sha256(np.asarray(frame).tobytes()).digest()
        if key not in cache:
            raw=_residual(frame,endpoint)
            checks=_residual_failures(raw,'window')
            if checks:
                transform=t.le.sa.estimate_similarity(np.asarray(frame),np.asarray(endpoint))
                aligned,cropped=_similarity_image(frame,transform)
                checks=_similarity_failures(frame,transform,edge or cropped,'window')+_residual_failures(_residual(aligned,endpoint),'window')
                if checks:
                    try:
                        alignment,_=t.le.sa.build_alignment(np.asarray(frame),np.asarray(endpoint),provenance)
                        dx,dy=t.le.sa.maps(alignment['model'],frame.size)
                        stats=t.le.sa.support_map_stats(frame,endpoint,dx,dy)
                        aligned,_=t.le.sa.apply(frame,alignment,1)
                        checks=_local_map_failures(stats,limit,'window')+_residual_failures(_residual(aligned,endpoint),'window')
                    except (ValueError,OSError) as exc:
                        checks=[_failure('local_window_correspondence','過渡幀對應',False,True,str(exc),'window')]
            if edge:
                checks.append(_failure('local_window_clipped','過渡畫布裁切',True,False,'原過渡幀已超出畫布','window'))
            cache[key]=checks
        checked+=1
        if cache[key]:
            return dict(safe=False,checked_frames=checked,frame=index,failed_checks=cache[key])
    return dict(safe=True,checked_frames=checked,failed_checks=[])


def _local_geometry_window(project,candidate,sources,a,ca,alignment):
    """Check the exact persisted alignment renderer, with no target blending."""
    dx,dy=t.le.sa.maps(alignment['model'],sources[a['key']].dimensions)
    limit=min(12.,max(3.,math.hypot(*sources[a['key']].dimensions)*.012))
    checked=0;worst=0.;lost=0
    for index in range(a['end']-a['tail_frames']+2,a['end']+1):
        original,old_edge=t._render_base_frame(a,sources[a['key']],index,project=project)
        actual,edge=t._render_base_frame(ca,sources[a['key']],index,project=candidate)
        _,position=t.ts.weight(ca,index)
        strength=position*alignment['strength']/100
        stats=t.le.sa.support_map_stats(original,actual,dx,dy,strength)
        checks=_local_map_failures(stats,limit,'window')
        loss=t.le.sa.sampling_support_loss(original,actual,dx,dy,strength)
        lost=max(lost,loss);worst=max(worst,stats['support_max_px'])
        if loss>3:
            checks.append(_failure('local_sampling_support','局部採樣後輪廓保留',loss,3,'原角色的細小 Alpha 範圍不可在變形採樣後消失','window'))
        if old_edge or edge:
            checks.append(_failure('local_window_clipped','局部過渡画布裁切',True,False,'過渡範圍不可裁掉原角色內容','window'))
        # Verify that the actual renderer is exactly the one-map sampler, not
        # an accidentally combined affine, target blend or double correction.
        expected=t.le.sa.resample_frame(np.asarray(original),dx,dy,strength)
        if not np.array_equal(expected,np.asarray(actual)):
            checks.append(_failure('local_renderer_mismatch','局部參數渲染一致性',False,True,'實際渲染與單次局部採樣不一致','window'))
        checked+=1
        if checks:return dict(safe=False,checked_frames=checked,frame=index,failed_checks=checks)
    return dict(safe=True,checked_frames=checked,support_max_px=worst,sampling_lost_alpha_pixels=lost,failed_checks=[])


def _local_review_candidate(project, sources, a, b, value, guard):
    """Bounded nonzero trials; geometry can be adopted without erasing residual.

    All trials reuse the existing guarded local map and actual saved renderer.
    A common endpoint additionally needs strict content/full-closure validation.
    """
    left, left_edge = t._render_base_frame(a, sources[a['key']], a['end'], project=project)
    right, right_edge = t._render_base_frame(b, sources[b['key']], b['start'], project=project)
    provenance = dict(scope='transition', source=t._alignment_binding(a,sources[a['key']],'tail'),
                      reference=t._alignment_binding(b,sources[b['key']],'head',raw=False,project=project))
    alignment, report = t.le.sa.build_alignment(np.asarray(left), np.asarray(right), provenance)
    max_move=min(12.,max(3.,math.hypot(*left.size)*.012))
    dx,dy=t.le.sa.maps(alignment['model'],left.size)  # retains all-canvas hard guard
    before_residual=_residual(left,right)
    before=dict(metrics=value,geometry=guard,residual=before_residual)
    temporal=None;attempts=[];choices=[];rendered_candidates={}
    for strength in LOCAL_STRENGTHS:
        stats=t.le.sa.support_map_stats(left,right,dx,dy,strength/100)
        checks=_local_map_failures(stats,max_move)
        if report['warnings']:
            checks.append(_failure('local_boundaries','畫布邊界對應',report['warnings'],[],'沒有可靠的貼邊對應，不能自動採用','registration'))
        if left_edge or right_edge:
            checks.append(_failure('local_clipped','畫布裁切',True,False,'原端點已超出畫布','registration'))
        summary=dict(kind='local',strength=strength,rendered=False,selected=False,status='rejected',
                     before=before,after=None,improvement=None,window=None,failed_checks=checks,
                     displacement=dict(support_max_px=round(stats['support_max_px'],6),
                                       canvas_max_px=round(stats['canvas_max_px'],6),limit_px=max_move),
                     transform=dict(max_displacement_px=stats['support_max_px'],guard_scale=report['guard_scale'],strength=strength))
        attempts.append(summary)
        if checks:continue
        trial=copy.deepcopy(project)
        ca=next(c for c in trial['clips'] if c['key']==a['key'])
        cb=next(c for c in trial['clips'] if c['key']==b['key'])
        current=copy.deepcopy(alignment);current['strength']=float(strength)
        current=t.le.sa.validate_alignment(current)
        ca['tail']['alignment']=current
        registered,clipped=t._render_base_frame(ca,sources[a['key']],a['end'],project=trial)
        residual=_residual(registered,right)
        improvement=1-residual['mean']/max(before_residual['mean'],1e-9)
        after_guard=_geometry(trial,sources,ca,cb)
        summary.update(rendered=True,after=dict(metrics=t.metrics(ca,cb,sources,trial),geometry=after_guard,residual=residual),
                       improvement=round(improvement,6))
        rendered_candidates[strength]=trial
        if clipped or improvement<.30 or stats['support_max_px']<.05:
            checks.append(_failure('local_improvement','局部對位改善幅度',round(improvement,6),.30,
                                   '非零局部候選必須有明顯改善；微小或無效採樣不算成功','registration'))
            continue
        geometry_window=_local_geometry_window(project,trial,sources,a,ca,current)
        checks.extend(geometry_window['failed_checks'])
        summary['window']=dict(geometry=geometry_window,temporal=None,safe=False)
        if checks:
            summary['status']='review'
            continue
        # Geometry-only adoption preserves source content. Endpoint differences
        # remain Review; only the existing real closure renderer may close them.
        content_checks=_residual_failures(residual)
        endpoint_checks=_boundary_failures(dict(silhouette=0,appearance=0,motion=0),
                          dict(after_guard,changed_detail_ratio=residual['detail_ratio']),motion=False)
        closure=None
        if not content_checks and not endpoint_checks:
            if temporal is None:temporal=_local_temporal_support(project,sources,a,left,provenance)
            if temporal['safe']:closure=_transition_window(trial,sources,ca,cb)
        summary['window'].update(closure=closure,temporal=temporal,safe=bool(closure and closure['safe']))
        if closure and closure['safe']:
            summary['status']='safe';scope='closure'
        else:
            summary['status']='safe_geometry';scope='geometry'
            checks.extend(content_checks+endpoint_checks)
            if temporal and not temporal['safe']:checks.extend(temporal['failed_checks'])
            if closure and not closure['safe']:
                checks.append(_failure('local_closure_window','共同端點過渡',closure.get('frame'),'small_change',
                                       '局部幾何可採用，但共用端點會改動過渡內容；保留殘差供驗收','window'))
            checks.append(_failure('local_candidate_needs_review','局部改善後仍需驗收',False,True,
                                   '可套用安全的局部對位；尚未共用端點，不會抹平剩餘差異','window'))
        summary['apply_scope']=scope
        choices.append((improvement,scope=='closure',trial,summary))
    if choices:
        _,_,candidate,chosen=max(choices,key=lambda item:(item[0],item[1]))
        chosen['selected']=True
        return candidate,dict(chosen,attempts=attempts),chosen['failed_checks']
    rendered=[item for item in attempts if item['rendered']]
    chosen=max(rendered,key=lambda item:item['improvement']) if rendered else attempts[-1]
    chosen['selected']=True
    # A bounded endpoint may still be inspected if a later fade frame failed;
    # apply_scope none prevents it from entering the transaction.
    preview=rendered_candidates.get(chosen.get('strength')) if chosen['status']=='review' else None
    return preview,dict(chosen,attempts=attempts,apply_scope='none'),chosen['failed_checks']


def _transition_window(project, sources, a, b):
    """Check every changed fade frame against its actual candidate morph."""
    anchor, clipped = t._render_base_frame(b, sources[b['key']], b['start'], project=project)
    checked, worst_mean, worst_large = 0, 0., 0.
    for clip, side in ((a, 'tail'), (b, 'head')):
        indices = (range(clip['end'] - clip['tail_frames'] + 1, clip['end'] + 1) if side == 'tail'
                   else range(clip['start'], clip['start'] + clip['head_frames']))
        for index in indices:
            actual_side, amount = t.ts.weight(clip, index)
            if actual_side != side or amount <= 0:
                continue
            original, edge = t._render_base_frame(clip, sources[clip['key']], index, project=project)
            candidate = t.ts.lc.morph(original, anchor, amount)
            first, second = np.asarray(original), np.asarray(candidate)
            region = (first[:, :, 3] > 2) | (second[:, :, 3] > 2)
            difference = np.abs(t.ts.lc._associated(first) - t.ts.lc._associated(second))
            mean = float(np.mean(difference[region])) if region.any() else 255.
            large = float(np.mean(np.max(difference, axis=2)[region] > 24)) if region.any() else 1.
            geometry = _image_geometry(original, candidate, clipped or edge)
            checked += 1
            worst_mean, worst_large = max(worst_mean, mean), max(worst_large, large)
            safe = (not geometry['clipped'] and geometry['topology_same'] and geometry['iou'] >= LIMITS['iou']
                    and geometry['centroid'] <= LIMITS['centroid'] and .96 <= geometry['area_ratio'] <= 1.04
                    and mean <= LIMITS['window_mean_delta'] and large <= LIMITS['window_large_delta_ratio'])
            if not safe:
                return dict(safe=False, checked_frames=checked, clip_id=clip['key'], label=clip['label'], frame=index,
                            mean_delta=round(mean, 6), large_delta_ratio=round(large, 6), geometry=geometry)
    return dict(safe=True, checked_frames=checked, mean_delta=round(worst_mean, 6), large_delta_ratio=round(worst_large, 6))


def _score(metrics):
    return metrics['silhouette'] + metrics['appearance'] + .02 * metrics['motion']


def _boundary_candidate(project, sources, a, b, before, remaining):
    """Suggestions only; total suggested cropping per clip remains <= 10%."""
    if _score(before) <= .5:
        return None
    def allowance(clip):
        count = clip['end'] - clip['start'] + 1
        prior_trim = math.floor(count * LIMITS['crop_fraction']) - remaining[clip['key']]
        window_room = count - clip['head_frames'] - clip['tail_frames'] - prior_trim
        return max(0, min(5, remaining[clip['key']], window_room, count - 2))
    tail, head = allowance(a), allowance(b)
    best = None
    for trim_tail in range(tail + 1):
        for trim_head in range(head + 1):
            if not trim_tail + trim_head:
                continue
            if a['key'] == b['key'] and trim_tail + trim_head > allowance(a):
                continue
            ca, cb = dict(a, end=a['end'] - trim_tail), dict(b, start=b['start'] + trim_head)
            # Retain both fade windows: a candidate must also be a legal recipe.
            if any(c['end'] - c['start'] + 1 < c['head_frames'] + c['tail_frames'] for c in (ca, cb)):
                continue
            try:
                value = t.metrics(ca, cb, sources, project)
            except ValueError:
                continue
            score = _score(value)
            if score <= _score(before) * .7 and _score(before) - score >= .5 and (best is None or score < best['score']):
                best = dict(a=a['key'], b=b['key'], a_label=a['label'], b_label=b['label'],
                            a_end=ca['end'], b_start=cb['start'], trim_tail=trim_tail, trim_head=trim_head,
                            score=round(score, 4), before=before, after=value, status='REVIEW',
                            code='candidate_needs_motion_review', reason='候選差異較小；裁切可能改變動作，僅建議，未套用')
    if best:
        remaining[a['key']] -= best['trim_tail']
        remaining[b['key']] -= best['trim_head']
    return best


def analyze(project, sources):
    if not project['clips']:
        raise ValueError('請先加入至少一段動畫')
    signature = t.project_signature(project, sources)
    prepared, repairs = _prepare_generated(project, sources)
    corrected, appearance, reviews, appearance_count = _appearance(prepared, sources)
    by_key = {c['key']: c for c in corrected['clips']}
    original_by_key = {c['key']: c for c in prepared['clips']}
    seams, selected, searches, previews, registrations = [], [], [], {}, {}
    geometry_count = 0
    remaining = {c['key']: math.floor((c['end'] - c['start'] + 1) * LIMITS['crop_fraction']) for c in corrected['clips']}
    try:
        t.ts.pairs(corrected)
        unique_route = True
    except ValueError:
        unique_route = False
    existing = t.ts.active(prepared)
    for ak, bk in dict.fromkeys(t.ts.route_pairs(corrected)):
        a, b = by_key[ak], by_key[bk]
        base = dict(a=ak, b=bk, a_label=a['label'], b_label=b['label'])
        value = t.metrics(a, b, sources, corrected)
        guard = _geometry(corrected, sources, a, b)
        # Newly proposed tail maps must not masquerade as pre-existing manual
        # settings on an adjacent head later in this same batch.
        original_a,original_b=original_by_key[ak],original_by_key[bk]
        preserved = (existing or _active_alignment(original_a) or _active_alignment(original_b)
                     or _manual_transform(original_a, 'tail') or _manual_transform(original_b, 'head'))
        enough_window = a['tail_frames'] >= 2 and b['head_frames'] >= 2
        safe = (unique_route and not preserved and enough_window and not guard['clipped']
                and guard['foreground_samples'] >= LIMITS['foreground_samples'] and guard['topology_same']
                and guard['iou'] >= LIMITS['iou'] and guard['centroid'] <= LIMITS['centroid']
                and .96 <= guard['area_ratio'] <= 1.04 and guard['changed_detail_ratio'] <= LIMITS['changed_detail_ratio']
                and all(value[name] <= LIMITS[name] for name in ('silhouette', 'appearance', 'motion')))
        exact = guard['endpoints_equal'] and not guard['clipped'] and value['motion'] <= LIMITS['motion'] and guard['foreground_samples'] >= LIMITS['foreground_samples']
        window = _transition_window(corrected, sources, a, b) if safe and not exact else None
        if window and not window['safe']:
            safe = False
        failed_checks = _boundary_failures(value, guard)
        proposal = None
        attempts=[];apply_scope='none'
        if preserved:
            failed_checks.append(_failure('existing_correction', '既有修正', True, False, '保留既有對位、共同接點或手動變形；不自動覆寫'))
        if not unique_route:
            failed_checks.append(_failure('ambiguous_route', '播放路線', 'repeated_or_subset', 'unique', '重複或部分路線請先確認共享修正影響'))
        if not enough_window:
            failed_checks.append(_failure('fade_window', '過渡幀數', [a['tail_frames'],b['head_frames']], 2, '所連接的頭尾各需至少 2 幀'))
        if window and not window['safe']:
            failed_checks.append(_failure('transition_window', '過渡範圍', window.get('frame'), 'small_change', '中間過渡幀仍有超出保守範圍的差異', 'window'))
        if not safe and not exact and not preserved and unique_route and enough_window and guard['foreground_samples'] >= 512:
            try:
                trial, proposal, checks, candidate_window = _similarity_candidate(corrected, sources, a, b, value, guard)
                failed_checks = checks
                proposal.update(rendered=True,selected=True,failed_checks=copy.deepcopy(checks))
                attempts.append(proposal)
                if candidate_window is not None:
                    window = candidate_window
                elif window and not window['safe']:
                    failed_checks.append(_failure('transition_window','過渡範圍',window.get('frame'),'small_change',
                                                  '原過渡幀仍有無法用小幅對位解釋的差異','window'))
                if proposal['status'] != 'rejected':
                    previews[ak+'>'+bk] = dict(before=copy.deepcopy(corrected), after=trial, candidate=proposal)
                if not failed_checks:
                    # Parameters live in a bound per-frame registration table.
                    # Do not also apply the endpoint affine before registered
                    # blending: that would sample and correct the image twice.
                    registrations[ak+'>'+bk] = window['registration']
                    safe = True
            except (ValueError, OSError) as exc:
                failed_checks=[_failure('registration_unavailable', '對位估計', False, True, str(exc), 'registration')]
                attempts.append(dict(kind='similarity',status='rejected',rendered=False,selected=True,
                                     before=dict(metrics=value,geometry=guard),after=None,window=None,failed_checks=failed_checks))
            if not safe and not guard['clipped']:
                try:
                    local, local_summary, local_checks = _local_review_candidate(corrected,sources,a,b,value,guard)
                    local_attempts=local_summary.pop('attempts',[]) if local_summary else []
                    for attempt in attempts:attempt['selected']=False
                    attempts.extend(local_attempts)
                    if local_summary is not None:
                        proposal=local_summary
                        failed_checks=local_checks
                        window=proposal.get('window')
                        previews.pop(ak+'>'+bk,None)
                    if local is not None:
                        previews[ak+'>'+bk] = dict(before=copy.deepcopy(corrected),after=local,candidate=proposal)
                        apply_scope=proposal.get('apply_scope','none')
                        if apply_scope in ('geometry','closure'):
                            corrected=local
                            by_key={c['key']:c for c in corrected['clips']}
                            a,b=by_key[ak],by_key[bk]
                            corrected['reviews']={}
                            safe=apply_scope=='closure'
                            if apply_scope=='geometry':geometry_count+=1
                            window=proposal['window']
                except (ValueError,OSError) as exc:
                    local_checks=[_failure('local_registration_unavailable','局部對位估計',False,True,str(exc),'registration')]
                    # An unavailable local solver cannot relabel an already
                    # rendered similarity candidate or mix its failure values.
                    selected_attempt=not previews.get(ak+'>'+bk)
                    attempts.append(dict(kind='local',status='rejected',rendered=False,selected=selected_attempt,
                                         before=dict(metrics=value,geometry=guard),after=None,window=None,failed_checks=local_checks))
                    if selected_attempt:
                        for attempt in attempts[:-1]:attempt['selected']=False
                        proposal=attempts[-1];failed_checks=local_checks
        if safe:apply_scope='closure'
        status = 'REVIEW' if preserved else 'OK' if exact else 'FIX' if safe else 'REVIEW'
        code = 'existing_correction' if preserved else 'already_close' if exact else 'registered_boundary_correction' if safe and proposal else 'small_boundary_correction' if safe else 'transition_window_outside_safe_range' if window else 'boundary_outside_safe_range'
        reason = '保留既有對位／共同接點或手動變形，請播放驗收' if preserved else '端點已相同，仍需播放確認動作' if exact else '小幅對位後殘差與完整過渡均通過，建議套用' if safe and proposal else '端點與整段過渡的修正量均小，建議共用端點' if safe else '過渡範圍內仍有無法用安全幾何搬移解釋的差異，保留原樣' if window else '候選尚未通過安全檢查，請查看具體原因'
        if apply_scope=='geometry':
            code='local_geometry_improvement';reason='可套用安全的局部對位；首尾仍有差異，保留原內容並請播放驗收'
        row = dict(base, status=status, code=code, reason=reason, metrics=value, geometry=guard, window=window,
                   failed_checks=[] if status in ('OK','FIX') else failed_checks,
                   candidate=proposal, candidate_available=ak+'>'+bk in previews,attempts=attempts,apply_scope=apply_scope)
        seams.append(row)
        if status == 'FIX':
            selected.append((ak, bk))
        elif status == 'REVIEW':
            reviews.append(_review('seam', code, reason, **base))
            if not preserved:
                candidate = _boundary_candidate(corrected, sources, a, b, value, remaining)
                if candidate:
                    searches.append(candidate)
    # Reject source changes during measurement; no stale plan enters the cache.
    current_sources = {c['key']: t.resolve_source(c['job_id'], c['version']) for c in project['clips']}
    if t.project_signature(project, current_sources) != signature:
        raise ValueError('分析期間來源已變更，請重新分析')
    plan = dict(id=uuid.uuid4().hex, signature=signature, route_mode=t.ts.route_mode(project),
                appearance=appearance, seams=seams, search=searches, review_items=reviews, repairs=repairs,
                summary=dict(appearance_fixes=appearance_count, seam_fixes=len(selected), geometry_fixes=geometry_count,
                             needs_review=len(reviews), repair_count=len(repairs)),
                measurement_scope='proposed_safe_appearance', limits=dict(LIMITS), registration_limits=dict(REGISTRATION_LIMITS), note=NOTE)
    with _LOCK:
        _PLANS[plan['id']] = dict(created=time.monotonic(), public=copy.deepcopy(plan), corrected=corrected,
                                 appearance_count=appearance_count, selected=selected, candidates=previews,
                                 registrations=registrations,geometry_count=geometry_count,result=None)
        while len(_PLANS) > 16:
            _PLANS.popitem(last=False)
    return dict(plan=plan)


def _candidate_for_preview(project, sources, plan_id, signature, a, b):
    with _LOCK:
        entry = _PLANS.get(plan_id)
        if not entry or time.monotonic()-entry['created'] > TTL:
            raise ValueError('分析計畫已過期或服務已重啟，請重新分析')
        if signature != entry['public']['signature'] or t.project_signature(project, sources) != signature:
            raise ValueError('草稿或來源已變更，請重新分析')
        candidate = copy.deepcopy(entry['candidates'].get(str(a)+'>'+str(b)))
    if candidate is None:
        raise ValueError('此接點沒有可預覽的安全範圍候選')
    return candidate


def _verify_preview_sources(project, signature):
    live = {c['key']:t.resolve_source(c['job_id'], c['version']) for c in project['clips']}
    if t.project_signature(project, live) != signature:
        raise ValueError('預覽期間來源已變更，請重新分析')


def candidate_preview(project, sources, plan_id, signature, a, b):
    candidate = _candidate_for_preview(project, sources, plan_id, signature, a, b)
    before = t.compare(candidate['before'], sources, a, b)
    after = t.compare(candidate['after'], sources, a, b)
    _verify_preview_sources(project, signature)
    return dict(before=before, after=after, candidate=candidate['candidate'],
                note='對位試算尚未套用；前後皆可能包含計畫內的明暗校準。尚未套用共同端點，不能視為已驗收的成品。')


def candidate_playback(project, sources, plan_id, signature, a, b, view, span=6):
    if view not in ('before', 'after'):
        raise ValueError('請選擇修正前或修正後試算')
    candidate = _candidate_for_preview(project, sources, plan_id, signature, a, b)
    # Render the inspected candidate in memory, including its actual fade.
    # Never save it or route a rejected proposal through the Apply operation.
    result = t.render_preview(candidate[view], sources, a, b, span)
    _verify_preview_sources(project, signature)
    return dict(result, candidate_view=view, applied=False,
                note='修正預覽・未套用。尚未通過的檢查仍保留；播放不會變更草稿。')


def apply(project, sources, plan_id, signature):
    with _LOCK:
        entry = _PLANS.get(plan_id)
        if not entry or time.monotonic() - entry['created'] > TTL:
            raise ValueError('分析計畫已過期或服務已重啟，請重新分析')
        plan = entry['public']
        if signature != plan['signature'] or t.project_signature(project, sources) != signature:
            raise ValueError('草稿或來源已變更，舊計畫未套用；請重新分析')
        if entry['result'] is not None:
            return copy.deepcopy(entry['result'])
        corrected = copy.deepcopy(entry['corrected'])
        if entry['appearance_count']:
            corrected['tone_match'] = copy.deepcopy(entry['corrected']['tone_match'])
            corrected['reviews'] = {}
        if entry['selected']:
            corrected = t.build_seams(corrected,sources,selected_pairs=entry['selected'],registrations=entry['registrations'])['project']
        corrected, current_sources = t.validate_project(corrected)
        if t.project_signature(project, current_sources) != signature:
            raise ValueError('套用期間來源已變更，計畫未套用；請重新分析')
        result = dict(project=corrected, review_items=copy.deepcopy(plan['review_items']),
                      applied=dict(appearance=entry['appearance_count'], seams=len(entry['selected']), geometry=entry['geometry_count'], repairs=len(plan['repairs'])),
                      plan_id=plan_id, note=NOTE)
        entry['result'] = copy.deepcopy(result)
        return result
