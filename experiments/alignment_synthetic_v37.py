"""Independent, reproducible alignment diagnosis using only generated robot art.

Run from anywhere. Does not inspect or modify production jobs. The fixture jobs
and transition projects live under the specified diagnostic output directory.
Metrics deliberately do not call seam_alignment.metrics or its Canny helper.
"""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import argparse
import copy
import hashlib
import json
import math
import sys
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import transitions as t


def robot(width=360, height=640, mouth=0):
    """Supersampled geometric toy robot, with panels and clipped side arms."""
    scale = 3
    im = Image.new('RGBA', (360 * scale, 640 * scale))
    d = ImageDraw.Draw(im)
    def box(bounds, fill, radius=0):
        bounds = tuple(round(v * scale) for v in bounds)
        if radius:
            d.rounded_rectangle(bounds, radius=radius * scale, fill=fill,
                                outline=(30, 30, 30, 255), width=2 * scale)
        else:
            d.rectangle(bounds, fill=fill, outline=(30, 30, 30, 255), width=2 * scale)
    def line(points, fill=(40, 40, 40, 255), width=2):
        d.line([(round(x * scale), round(y * scale)) for x, y in points],
               fill=fill, width=width * scale)
    # Arms cross canvas edges; top/bottom have transparent safety margins.
    box((-25, 224, 95, 303), (165, 170, 173, 255), 12)
    box((265, 224, 385, 303), (175, 181, 185, 255), 12)
    box((97, 442, 153, 576), (198, 203, 210, 255), 10)
    box((207, 442, 263, 576), (187, 192, 200, 255), 10)
    box((85, 564, 160, 605), (132, 142, 156, 255), 10)
    box((200, 564, 275, 605), (132, 142, 156, 255), 10)
    box((68, 193, 292, 456), (209, 214, 221, 255), 24)
    box((147, 164, 213, 207), (116, 126, 137, 255), 5)
    box((86, 43, 274, 183), (223, 227, 232, 255), 26)
    box((110, 80, 151, 113), (40, 50, 68, 255), 6)
    box((209, 80, 250, 113), (40, 50, 68, 255), 6)
    box((119, 89, 130, 97), (245, 245, 245, 255))
    box((218, 89, 229, 97), (245, 245, 245, 255))
    line([(129, 143 - mouth), (151, 150 + mouth),
          (209, 150 + mouth), (231, 143 - mouth)])
    # Different panel motifs prevent an ambiguous repeated-texture fixture.
    rng = np.random.default_rng(20261004)
    for row, y in enumerate((218, 291, 369)):
        for col, x in enumerate((94, 164, 234)):
            val = int(rng.integers(104, 224))
            box((x, y, x + 31, y + 39), (val, val, val, 255), 4)
            line([(x + 6, y + 9), (x + 22, y + 9), (x + 22, y + 25)])
            box((x + 6, y + 26, x + 10 + row, y + 31 + col),
                (38, 38, 38, 255))
    for y in (480, 521, 558):
        line([(104, y), (140, y), (140, y + 9)])
        line([(219, y), (254, y), (254, y - 7)])
    for x in (10, 36, 305, 332):
        line([(x, 242), (x + 12, 242), (x + 12, 280)])
    im = im.resize((width, height), Image.Resampling.LANCZOS)
    arr = np.asarray(im).copy()
    arr[arr[:, :, 3] == 0, :3] = 0
    return arr


def warp(arr, kind='bend', amount=1.):
    """Known analytic inverse deformation, independent of fitted RBF maps."""
    h, w = arr.shape[:2]
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    if kind == 'translation':
        dx = np.full_like(xx, 3.5 * amount)
        dy = np.full_like(yy, -2.8 * amount)
    else:
        body = np.exp(-((yy / h - .57) / .22) ** 2)
        head = np.exp(-((yy / h - .20) / .15) ** 2)
        dx = amount * (7 * body - 3.5 * head) * np.sin(np.pi * xx / (w - 1))
        dy = amount * 4.2 * np.exp(-((yy / h - .42) / .18) ** 2) * (xx / w - .25)
    premul = arr.astype(np.float32)
    premul[:, :, :3] *= premul[:, :, 3:] / 255
    mapped = cv2.remap(premul, xx + dx, yy + dy, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    mapped[:, :, :3] = np.divide(mapped[:, :, :3] * 255, mapped[:, :, 3:],
                                 out=np.zeros_like(mapped[:, :, :3]), where=mapped[:, :, 3:] > 0)
    result = np.clip(np.rint(mapped), 0, 255).astype(np.uint8)
    result[result[:, :, 3] == 0, :3] = 0
    return result


def symmetric_distance(a, b):
    if not a.any() or not b.any():
        return None
    return float((ndi.distance_transform_edt(~a)[b].mean() +
                  ndi.distance_transform_edt(~b)[a].mean()) / 2)


def independent_metrics(candidate, target):
    """Threshold silhouettes and interior ink edges; not build-report metrics."""
    a, b = candidate[:, :, 3] >= 128, target[:, :, 3] >= 128
    contour_a = a ^ ndi.binary_erosion(a, border_value=0)
    contour_b = b ^ ndi.binary_erosion(b, border_value=0)
    inner = ndi.binary_erosion(a & b, iterations=8, border_value=0)
    ink_a = (candidate[:, :, :3].mean(axis=2) < 90) & inner
    ink_b = (target[:, :, :3].mean(axis=2) < 90) & inner
    ink_edge_a = ink_a ^ ndi.binary_erosion(ink_a)
    ink_edge_b = ink_b ^ ndi.binary_erosion(ink_b)
    return dict(silhouette_mismatch_pixels=int(np.count_nonzero(a ^ b)),
                silhouette_union_pixels=int(np.count_nonzero(a | b)),
                silhouette_chamfer_px=symmetric_distance(contour_a, contour_b),
                interior_ink_chamfer_px=symmetric_distance(ink_edge_a, ink_edge_b),
                alpha_mae=float(np.abs(candidate[:, :, 3].astype(float) - target[:, :, 3]).mean()),
                fixed_canvas=list(candidate.shape[1::-1]),
                transparent_pixels=int(np.count_nonzero(candidate[:, :, 3] == 0)),
                partial_alpha_pixels=int(np.count_nonzero((candidate[:, :, 3] > 0) & (candidate[:, :, 3] < 255))))


def jid(name):
    return hashlib.md5(('synthetic-v37:' + name).encode()).hexdigest()


def write_job(root, name, arrays, fps=12):
    identifier = jid(name)
    folder = root / 'jobs' / identifier
    png = folder / 'transparent_png'
    png.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, arr in enumerate(arrays, 1):
        filename = f'frame_{index:08d}.png'
        Image.fromarray(arr).save(png / filename)
        frames.append(dict(file=filename, status='done'))
    value = dict(id=identifier, name=name + '.mp4', state='complete', is_test=False,
                 synthetic_fixture=True, frames=frames, dimensions=list(arrays[0].shape[1::-1]),
                 config=dict(fps=fps, preview_frames=len(arrays)), paths=dict(png='transparent_png'),
                 created=time.time(), updated=time.time())
    (folder / 'job.json').write_text(json.dumps(value, indent=2), encoding='utf-8')
    return identifier


def isolated(root):
    stack = ExitStack()
    root.mkdir(parents=True, exist_ok=True)
    (root / 'jobs').mkdir(exist_ok=True)
    for obj, key, value in ((t.e, 'ROOT', root), (t.e, 'JOBS', root / 'jobs')):
        stack.enter_context(patch.object(obj, key, value))
    stack.enter_context(patch.dict(t.e.PATHS, {}, clear=True))
    return stack


def public_alignment(root, source, target, case):
    with isolated(root):
        a = write_job(root, case + '_source', [source, source])
        b = write_job(root, case + '_reference', [target, target])
        project = t.handle('create', dict(name='Synthetic diagnosis - ' + case))
        ak, bk = jid(case + '_source_clip'), jid(case + '_reference_clip')
        project['clips'] = [dict(key=ak, job_id=a, label='Source', start=1, end=2,
                                 head_frames=0, tail_frames=1),
                            dict(key=bk, job_id=b, label='Reference', start=1, end=2,
                                 head_frames=1, tail_frames=0)]
        project = t.handle('save', dict(project=project))
        result = t.handle('align', dict(project=project, a=ak, b=bk, side='tail'))
        outputs = {}
        for strength in (68, 100):
            current = copy.deepcopy(project)
            current['clips'][0]['tail']['alignment'] = dict(result['alignment'], strength=strength)
            canonical, sources = t.validate_project(current)
            frame, clipped = t.render_frame(canonical['clips'][0], sources[ak], 2)
            if clipped:
                raise AssertionError('Alignment unexpectedly reported cropped output')
            outputs[str(strength)] = np.asarray(frame).copy()
        return outputs, dict(selected_points=result['report']['selection']['selected'],
                             guard_scale=result['report']['guard_scale'],
                             warnings=result['report']['warnings'])


def silhouette_overlay(candidate, target):
    a, b = candidate[:, :, 3] >= 128, target[:, :, 3] >= 128
    out = np.full((*a.shape, 3), 23, np.uint8)
    out[a & b] = 125
    out[a & ~b] = [255, 60, 83]
    out[b & ~a] = [25, 220, 255]
    return Image.fromarray(out)


def contact_sheet(path, cases):
    cellw, cellh = 270, 480
    labelh = 63
    margin = 14
    width = 5 * cellw + 6 * margin
    sheet = Image.new('RGB', (width, (cellh + labelh + margin) * len(cases) + 50), '#101a25')
    d = ImageDraw.Draw(sheet)
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 15)
    small = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 13)
    d.text((margin, 12), 'Independent synthetic test | Red: candidate only; cyan: reference only | 9:16, fixed canvas', font=font, fill='white')
    for row, (name, source, target, outputs, report) in enumerate(cases):
        y = 45 + row * (cellh + labelh + margin)
        panels = [('Reference', Image.fromarray(target)), ('Uncorrected', Image.fromarray(source)),
                  ('Before: contour difference', silhouette_overlay(source, target))]
        for strength in ('68', '100'):
            panels.append((strength + '%: contour difference', silhouette_overlay(outputs.get(strength, source), target)))
        for col, (title, im) in enumerate(panels):
            x = margin + col * (cellw + margin)
            d.text((x, y), name + ' | ' + title, font=small, fill='white')
            if col >= 2:
                key = 'before' if col == 2 else ('68' if col == 3 else '100')
                metric = report.get('metrics', {}).get(key, {})
                d.text((x, y + 18), f"mask diff {metric.get('silhouette_mismatch_pixels', 'n/a')} px", font=small, fill='#b7cee0')
                v = metric.get('interior_ink_chamfer_px')
                d.text((x, y + 35), f"interior ink {v:.3f} px" if v is not None else 'Rejected / unavailable', font=small, fill='#b7cee0')
            tile = Image.new('RGB', (cellw, cellh), '#171717')
            scaled = im.resize((cellw, cellh), Image.Resampling.NEAREST)
            tile.paste(scaled, mask=scaled.getchannel('A') if scaled.mode == 'RGBA' else None)
            sheet.paste(tile, (x, y + labelh))
    sheet.save(path)


def make_sequence_fixture(root):
    """Four visible robot expressions; endpoints connect geometrically, tones vary."""
    jobs = []
    names = ['AA-AB', 'AB-BB', 'BB-BA', 'BA-AA']
    states = [0., .8, 1.5, -.6, 0.]
    tone_offsets = [0., 13., -11., 7.]
    contrasts = [1., .9, 1.1, .95]
    for i, name in enumerate(names):
        frames = []
        for n in range(12):
            ratio = n / 11
            state = states[i] * (1 - ratio) + states[i + 1] * ratio
            arr = warp(robot(270, 480, mouth=3 * state), amount=state)
            rgb = arr[:, :, :3].astype(float)
            rgb = (rgb - 127.5) * contrasts[i] + 127.5 + tone_offsets[i]
            arr[:, :, :3] = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
            arr[arr[:, :, 3] == 0, :3] = 0
            frames.append(arr)
        identifier = write_job(root, 'SYNTH-' + name, frames)
        jobs.append(dict(id=identifier, name='SYNTH-' + name, frames=12, fps=12,
                         dimensions=[270, 480], intended_tone_offset=tone_offsets[i],
                         intended_contrast=contrasts[i]))
    with isolated(root):
        project = t.handle('create', dict(name='合成機器人 - 四段順播與明暗測試'))
        project['clips'] = [dict(key=jid('sequence-clip-' + entry['name']), label=entry['name'],
                                  job_id=entry['id'], version='original', start=1, end=12,
                                  head_frames=4, tail_frames=4) for entry in jobs]
        project['sequence'] = [clip['key'] for clip in project['clips']]
        project = t.handle('save', dict(project=project))
    result = dict(description='Independently drawn geometric robots only; no user assets read.',
                  jobs=jobs, project_id=project['id'], sequence=names,
                  root=str(root), source_alpha_is_straight=True)
    (root / 'manifest.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    return result


def run(out):
    out.mkdir(parents=True, exist_ok=True)
    reference = robot()
    topology = warp(reference, amount=.7)
    # A genuine topology change: remove a hand and cut a hole in a torso panel.
    topology[224:305, :65] = 0
    hole = Image.fromarray(topology)
    ImageDraw.Draw(hole).ellipse((179, 321, 225, 352), fill=(0, 0, 0, 0))
    topology = np.asarray(hole).copy()
    definitions = [('local bend', warp(reference), reference),
                   ('translation', warp(reference, 'translation'), reference),
                   ('changed topology', topology, reference),
                   ('identity control', reference.copy(), reference)]
    reports, panels = {}, []
    for name, source, target in definitions:
        report = dict(metrics={'before': independent_metrics(source, target)})
        try:
            outputs, fit = public_alignment(out / 'audit_fixture', source, target, name)
            report.update(state='computed', fit=fit)
            for strength, pixels in outputs.items():
                report['metrics'][strength] = independent_metrics(pixels, target)
                base = report['metrics']['before']
                report['metrics'][strength]['silhouette_reduction_percent'] = (
                    100 * (1 - report['metrics'][strength]['silhouette_mismatch_pixels'] /
                           base['silhouette_mismatch_pixels']) if base['silhouette_mismatch_pixels'] else None)
                Image.fromarray(pixels).save(out / (name.replace(' ', '_') + '_' + strength + '.png'))
        except ValueError as exc:
            outputs = {}
            report.update(state='rejected', error=str(exc))
        Image.fromarray(source).save(out / (name.replace(' ', '_') + '_source.png'))
        reports[name] = report
        panels.append((name, source, target, outputs, report))
    Image.fromarray(reference).save(out / 'reference_robot.png')
    contact_sheet(out / 'alignment_contact_sheet.png', panels)
    manifest = make_sequence_fixture(out / 'synthetic_fixture')
    document = dict(method='Public transitions.handle(align), validate_project, render_frame. Independent SciPy binary morphology + Euclidean distance transforms; ink threshold < 90; no production metrics used.',
                    metrics_scope='Single endpoint. Does not measure temporal flow, expression fidelity, perceptual preference, or user material.',
                    dimensions=[360, 640], synthetic_only=True,
                    cases=reports, fixture=manifest)
    (out / 'alignment_report.json').write_text(json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    return document


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'backups/v37_multi_workflow')
    args = parser.parse_args()
    result = run(args.output.resolve())
    print(json.dumps(dict(cases=result['cases'], fixture=result['fixture']), ensure_ascii=False, indent=2))
