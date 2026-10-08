"""Independent acceptance fixtures drawn entirely in this test.

No model calls, user assets, web services, or production directories are used.
Tests exercise the public finish-plan API and its actual PNG renderer, not the
registration implementation's private estimators.
"""
import base64
import copy
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

import transitions as t


def line_drawing():
    """Asymmetric mechanical diagram: opaque gradient, silhouette, thin lines."""
    width, height = 160, 240
    y, x = np.indices((height, width))
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    mask = Image.new('L', (width, height))
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((32, 18, 125, 218), radius=18, fill=255)
    draw.polygon([(32, 62), (13, 79), (14, 106), (32, 102)], fill=255)
    draw.polygon([(125, 140), (146, 151), (143, 170), (125, 167)], fill=255)
    pixels[:, :, 3] = np.asarray(mask)
    shade = np.clip(150 + x * .35 + y * .18, 0, 255).astype(np.uint8)
    pixels[:, :, :3] = shade[:, :, None]
    pixels[pixels[:, :, 3] == 0, :3] = 0
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((34, 20, 123, 216), radius=16, outline=(25, 25, 25, 255), width=1)
    draw.ellipse((51, 37, 75, 65), outline=(12, 12, 12, 255), width=1)
    draw.line([(85, 35), (104, 42), (110, 65), (86, 71)], fill=(15, 15, 15, 255), width=1)
    draw.line([(43, 91), (62, 119), (95, 98), (114, 128)], fill=(20, 20, 20, 255), width=1)
    draw.line([(44, 162), (60, 141), (82, 154), (107, 183)], fill=(18, 18, 18, 255), width=1)
    draw.rectangle((52, 185, 68, 203), outline=(30, 30, 30, 255), width=1)
    draw.line((83, 195, 108, 195), fill=(15, 15, 15, 255), width=1)
    return image


def translate(image, dx=0, dy=0):
    # Integer paste has no filtering ambiguity and never wraps content.
    result = Image.new('RGBA', image.size)
    result.paste(image, (dx, dy))
    return result


def small_scale(image, scale):
    w, h = image.size
    cx, cy = (w - 1) / 2, (h - 1) / 2
    return image.transform(image.size, Image.Transform.AFFINE,
                           (1 / scale, 0, cx * (1 - 1 / scale),
                            0, 1 / scale, cy * (1 - 1 / scale)),
                           resample=Image.Resampling.BICUBIC)


def fractional_translate(image, dx=0, dy=0):
    return image.transform(image.size, Image.Transform.AFFINE,
                           (1, 0, -dx, 0, 1, -dy),
                           resample=Image.Resampling.BICUBIC)


def progressive_robot_drawing():
    """Native-resolution synthetic robot, also exercises thin inset outlines."""
    image = Image.new('RGBA', (480, 864))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((148, 50, 332, 250), 35, fill='#d3d3d3', outline='#333333', width=3)
    draw.rounded_rectangle((130, 275, 350, 620), 25, fill='#c9c9c9', outline='#333333', width=3)
    draw.rectangle((204, 248, 276, 275), fill='#a9a9a9', outline='#333333', width=3)
    for x in (76, 350):
        draw.rounded_rectangle((x, 310, x+54, 585), 20, fill='#d3d3d3', outline='#333333', width=3)
    for x in (150, 275):
        draw.rounded_rectangle((x, 616, x+54, 806), 12, fill='#b6b6b6', outline='#333333', width=3)
    for x in (186, 274):
        draw.ellipse((x, 124, x+22, 146), fill='#555555')
        draw.ellipse((x+5, 128, x+9, 132), fill='white')
    draw.arc((190, 160, 290, 210), 0, 180, fill='#555555', width=3)
    for y in (326, 388, 450, 512):
        for x in (156, 246):
            draw.rounded_rectangle((x, y, x+54, y+38), 8, outline='#555555', width=2)
            draw.line((x+10, y+9, x+35, y+20, x+12, y+29), fill='#777777', width=2)
    return image


class FinishRegistrationIndependentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='finish-registration-review-')
        self.root = Path(self.tmp.name)
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        self.patches = [patch.object(t.e, 'ROOT', self.root), patch.object(t.e, 'JOBS', self.jobs),
                        patch.dict(t.e.PATHS, {}, clear=True), patch.dict(t.e.ACTIVE, {}, clear=True),
                        patch.dict(t.le.RUNS, {}, clear=True), patch.dict(t.RUNS, {}, clear=True)]
        for item in self.patches:
            item.start()
        self.addCleanup(self.cleanup)
        self.base = line_drawing()
        self.project = t.handle('create', dict(name='Independent line drawing', route_mode='open'))

    def cleanup(self):
        t.ts.lc.clear_cache()
        t.le.sa.clear_cache()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def source(self, ordinal, frames):
        jid = 'abcdef'[ordinal] * 32
        key = '123456'[ordinal] * 32
        folder = self.jobs / jid / 'transparent_png'
        folder.mkdir(parents=True)
        records = []
        for index, image in enumerate(frames, 1):
            name = f'frame_{index:08d}.png'
            image.save(folder / name)
            records.append(dict(file=name, status='done'))
        t.e.atomic_json(folder.parent / 'job.json', dict(
            id=jid, name=f'Independent {ordinal}', state='complete', frames=records,
            dimensions=list(frames[0].size), config=dict(fps=24), paths=dict(png='transparent_png')))
        return dict(key=key, job_id=jid, version='original', label=f'Line drawing {ordinal}',
                    start=1, end=len(frames), head_frames=3, tail_frames=5,
                    tone=0, contrast=0, head={}, tail={})

    def pair(self, left, right=None):
        right = self.base if right is None else right
        a = left if isinstance(left, list) else [left] * 12
        b = right if isinstance(right, list) else [right] * 12
        self.project['clips'] = [self.source(0, a), self.source(1, b)]
        self.project['sequence'] = [c['key'] for c in self.project['clips']]
        self.project = t.handle('save', dict(project=self.project))
        return self.project

    def analyze(self):
        before = copy.deepcopy(self.project)
        path = t.projectdir(self.project['id']) / 'project.json'
        saved, sources = path.read_bytes(), self.hashes()
        plan = t.handle('finish_analyze', dict(project=self.project))['plan']
        self.assertEqual(self.project, before, 'Analyze must not mutate the submitted recipe')
        self.assertEqual(path.read_bytes(), saved, 'Analyze must not save a different project')
        self.assertEqual(self.hashes(), sources, 'Analyze must not alter source PNGs')
        return plan

    def apply(self, plan):
        return t.handle('finish_apply', dict(project=self.project,
            plan_id=plan['id'], signature=plan['signature']))

    def hashes(self):
        return {path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.jobs.glob('*/transparent_png/*.png')}

    def assert_preserved_and_closed(self, plan):
        original = copy.deepcopy(self.project)
        source_hashes = self.hashes()
        saved = (t.projectdir(self.project['id']) / 'project.json').read_bytes()
        canonical, sources = t.validate_project(original)
        a = canonical['clips'][0]
        reference = canonical['clips'][-1]
        original_head = t.render_frame(reference, sources[reference['key']], reference['start'], project=canonical)[0]
        untouched = t.render_frame(a, sources[a['key']], 5, project=canonical)[0]
        result = self.apply(plan)
        self.assertEqual(result['applied']['seams'], 1)
        corrected, corrected_sources = t.validate_project(result['project'])
        a, b = corrected['clips'][0], corrected['clips'][-1]
        tail = t.render_frame(a, corrected_sources[a['key']], a['end'], project=corrected)[0]
        head = t.render_frame(b, corrected_sources[b['key']], b['start'], project=corrected)[0]
        np.testing.assert_array_equal(np.asarray(tail), np.asarray(head))
        np.testing.assert_array_equal(np.asarray(head), np.asarray(original_head))
        np.testing.assert_array_equal(np.asarray(untouched), np.asarray(
            t.render_frame(a, corrected_sources[a['key']], 5, project=corrected)[0]))
        self.assertEqual(tail.size, self.base.size)
        self.assertEqual(self.project, original)
        self.assertEqual(self.hashes(), source_hashes)
        self.assertEqual((t.projectdir(original['id']) / 'project.json').read_bytes(), saved)
        self.assertFalse(any(v.get('verdict') == 'pass' for v in corrected['reviews'].values()))
        return corrected, corrected_sources

    def assert_preview_matches_real_export(self, project, sources):
        a, b = project['clips'][:2]
        preview = t.render_preview(project, sources, a['key'], b['key'], span=5)
        pid, version = project['id'], 'v001'
        output = t._version_dir(pid, version)
        output.mkdir(parents=True)
        t.RUNS[pid] = dict(state='running', version=version)
        t.export_worker(project, sources, version, t.project_signature(project, sources))
        self.assertEqual(t.RUNS[pid]['state'], 'complete', t.RUNS[pid].get('phase'))
        manifest = t._read_json(output / 'manifest.json')
        self.assertTrue(manifest['seam_verification']['all_equal'])
        exported = {clip['key']: clip for clip in manifest['clips']}
        clips = {clip['key']: clip for clip in project['clips']}
        for frame in preview['frames']:
            clip = clips[frame['clip_key']]
            local = frame['frame'] - clip['start'] + 1
            path = t.projectdir(pid) / (exported[clip['key']]['png_pattern'] % local)
            with Image.open(path) as actual, Image.open(io.BytesIO(
                    base64.b64decode(frame['image'].split(',')[-1]))) as shown:
                np.testing.assert_array_equal(actual, shown)

    def test_progressive_four_pixel_translation_keeps_animation_and_export_consistent(self):
        self.base = self.base.resize((480, 720), Image.Resampling.BICUBIC)
        detail = ImageDraw.Draw(self.base)
        for row in range(270, 390, 8):
            detail.line([(135, row), (200, row + 7), (280, row - 3), (334, row + 10)],
                        fill=(12, 12, 12, 255), width=1)
        frames = [fractional_translate(self.base, 4 * index / 23) for index in range(24)]
        self.pair(frames, [self.base] * 24)
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'FIX', plan['seams'][0])
        corrected, sources = self.assert_preserved_and_closed(plan)
        self.assert_preview_matches_real_export(corrected, sources)
        first = self.apply(plan)
        self.assertEqual(self.apply(plan)['project'], first['project'], 'Repeat Apply must reuse its first result')
        for key, value in (('tail_frames', 6), ('head_frames', 4), ('end', 23)):
            changed = copy.deepcopy(corrected)
            changed['clips'][0][key] = value
            with self.assertRaises(ValueError, msg=f'Changing {key} must invalidate the saved per-frame correction'):
                t.validate_project(changed)
        changed = copy.deepcopy(corrected)
        changed['seam_closure']['links'][0]['registration']['tail'][0]['dx'] += .1
        with self.assertRaises(ValueError, msg='Changing a saved transform must invalidate its verification binding'):
            t.validate_project(changed)
        changed = copy.deepcopy(corrected)
        missing = changed['seam_closure']['links'][0]['registration']['tail'].pop(0)
        with self.assertRaises(ValueError, msg='Missing one saved transform must not silently use optical flow'):
            t.validate_project(changed)
        with self.assertRaises(ValueError, msg='The renderer itself must fail closed on a missing transform'):
            t.render_frame(changed['clips'][0], sources[changed['clips'][0]['key']], missing['frame'], project=changed)
        changed = copy.deepcopy(corrected)
        changed['seam_closure']['links'][0].pop('registration')
        with self.assertRaises(ValueError, msg='Removing a whole table must invalidate its verification binding'):
            t.validate_project(changed)

    def test_progressive_one_percent_scale_keeps_unaffected_frames(self):
        frames = [small_scale(self.base, 1 + .01 * index / 23) for index in range(24)]
        self.pair(frames, [self.base] * 24)
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'FIX', plan['seams'][0])
        self.assert_preserved_and_closed(plan)

    def test_progressive_single_loop_checks_both_head_and_tail(self):
        self.base = progressive_robot_drawing()
        frames = [fractional_translate(self.base, 4 * index / 23) for index in range(24)]
        clip = self.source(0, frames)
        clip.update(head_frames=6, tail_frames=6)
        self.project.update(clips=[clip], sequence=[clip['key']], route_mode='loop')
        self.project = t.handle('save', dict(project=self.project))
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'FIX', plan['seams'][0])
        # The generic helper's unchanged frame 5 is in this fixture's head fade;
        # use a frame in the true unaffected interval explicitly instead.
        result = self.apply(plan)
        fixed, sources = t.validate_project(result['project'])
        source, original_sources = t.validate_project(self.project)
        before = t.render_frame(source['clips'][0], original_sources[clip['key']], 12, project=source)[0]
        after = t.render_frame(fixed['clips'][0], sources[clip['key']], 12, project=fixed)[0]
        np.testing.assert_array_equal(before, after)
        first = t.render_frame(fixed['clips'][0], sources[clip['key']], 1, project=fixed)[0]
        last = t.render_frame(fixed['clips'][0], sources[clip['key']], 24, project=fixed)[0]
        np.testing.assert_array_equal(first, last)

    def test_partial_registration_does_not_change_unselected_seam(self):
        self.pair(translate(self.base, 1, 0))
        different = self.base.copy()
        different.paste(different.crop((40, 28, 118, 207)).transpose(Image.Transpose.FLIP_LEFT_RIGHT), (40, 28))
        other = self.source(2, [different] * 12)
        self.project['clips'].append(other)
        self.project['sequence'].append(other['key'])
        self.project = t.handle('save', dict(project=self.project))
        plan = self.analyze()
        self.assertEqual([row['status'] for row in plan['seams']], ['FIX', 'REVIEW'])
        original, original_sources = t.validate_project(self.project)
        applied = self.apply(plan)
        self.assertEqual(applied['applied']['seams'], 1)
        fixed, sources = t.validate_project(applied['project'])
        for ci, indices in ((1, range(8, 13)), (2, range(1, 13))):
            before, after = original['clips'][ci], fixed['clips'][ci]
            for index in indices:
                np.testing.assert_array_equal(
                    t.render_frame(before, original_sources[before['key']], index, project=original)[0],
                    t.render_frame(after, sources[after['key']], index, project=fixed)[0])

    def test_one_pixel_translation_is_fixable_without_changing_sources_or_middle(self):
        self.pair(translate(self.base, 1, 0))
        before = self.hashes()
        plan = self.analyze()
        self.assertEqual(self.hashes(), before, 'Analyze must not alter any source PNG')
        self.assertEqual(plan['seams'][0]['status'], 'FIX', plan['seams'][0])
        self.assert_preserved_and_closed(plan)

    def test_one_percent_scale_is_fixable_without_changing_sources_or_middle(self):
        self.pair(small_scale(self.base, 1.01))
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'FIX', plan['seams'][0])
        self.assert_preserved_and_closed(plan)

    def test_single_loop_uses_the_same_registration_without_moving_its_head(self):
        frames = [self.base.copy() for _ in range(6)] + [translate(self.base, 1, 0) for _ in range(6)]
        clip = self.source(0, frames)
        self.project.update(clips=[clip], sequence=[clip['key']], route_mode='loop')
        self.project = t.handle('save', dict(project=self.project))
        plan = self.analyze()
        self.assertEqual(len(plan['seams']), 1)
        self.assertEqual(plan['seams'][0]['status'], 'FIX', plan['seams'][0])
        self.assert_preserved_and_closed(plan)

    def test_new_fine_detail_is_not_erased_as_a_global_registration_error(self):
        extra = self.base.copy()
        ImageDraw.Draw(extra).line([(70, 76), (96, 86), (72, 100)], fill=(0, 0, 0, 255), width=3)
        self.pair(extra)
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW', plan['seams'][0])
        result = self.apply(plan)
        self.assertEqual(result['applied']['seams'], 0)

    def test_thin_alpha_extension_is_not_erased_after_correct_registration(self):
        extra = self.base.copy()
        ImageDraw.Draw(extra).line((125, 75, 155, 75), fill=(20, 20, 20, 255), width=1)
        self.pair(translate(extra, 1, 0))
        plan = self.analyze()
        row = plan['seams'][0]
        self.assertEqual(row['status'], 'REVIEW', row)
        self.assertTrue(row.get('failed_checks'), 'Unmatched thin alpha content needs a concrete reason')
        self.assertEqual(self.apply(plan)['applied']['seams'], 0)

    def test_missing_fine_detail_is_not_synthesized_as_a_global_registration_error(self):
        extra = self.base.copy()
        ImageDraw.Draw(extra).line([(70, 76), (96, 86), (72, 100)], fill=(0, 0, 0, 255), width=3)
        self.pair(self.base, extra)
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW', plan['seams'][0])
        self.assertEqual(self.apply(plan)['applied']['seams'], 0)

    def test_local_line_displacement_has_explicit_candidate_or_review_evidence(self):
        pixels = np.array(self.base)
        # Move only one existing line/patch: a similarity cannot solve this exactly.
        pixels[138:183, 42:110] = np.array(self.base)[137:182, 42:110]
        self.pair(Image.fromarray(pixels))
        plan = self.analyze()
        row = plan['seams'][0]
        self.assertIn(row['status'], ('FIX', 'REVIEW'))
        if row['status'] == 'REVIEW':
            self.assertTrue(row.get('failed_checks'), 'Review must explain a concrete failed check')
        else:
            self.assertTrue(row.get('candidate_available') or row.get('window', {}).get('safe'))
            self.assert_preserved_and_closed(plan)

    def test_unreliable_correspondence_is_reviewed(self):
        changed = self.base.copy()
        tile = changed.crop((40, 28, 118, 207)).transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        changed.paste(tile, (40, 28))
        self.pair(changed)
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW', plan['seams'][0])
        self.assertEqual(self.apply(plan)['applied']['seams'], 0)

    def test_motion_inside_the_fade_window_is_not_hidden_by_aligned_endpoints(self):
        left = [translate(self.base, 1, 0) for _ in range(12)]
        ImageDraw.Draw(left[9]).rectangle((127, 52, 157, 107), fill=(180, 180, 180, 255))
        self.pair(left)
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW', plan['seams'][0])
        self.assertEqual(self.apply(plan)['applied']['seams'], 0)

    def test_canvas_edge_content_loss_is_not_hidden_by_registration(self):
        edge = translate(self.base, 13, 0)  # Right silhouette reaches x=159.
        self.pair(edge, translate(edge, 1, 0))
        self.project['clips'][0]['tail']['dx'] = 1
        # The current edit already cuts the silhouette. Matching the cut version
        # must not turn this into a successful safe correction.
        canonical, sources = t.validate_project(self.project)
        a = canonical['clips'][0]
        self.assertTrue(t.render_frame(a, sources[a['key']], a['end'], project=canonical)[1])
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW', plan['seams'][0])
        result = self.apply(plan)
        self.assertEqual(result['applied']['seams'], 0)
        self.assertEqual(result['project']['clips'][0]['tail']['dx'], 1)

    def test_automatic_registration_must_not_crop_edge_alpha_to_match_target(self):
        edge = self.base.copy()
        draw = ImageDraw.Draw(edge)
        draw.rectangle((125, 85, 159, 200), fill=(190, 190, 190, 255))
        draw.line((133, 95, 159, 163), fill=(12, 12, 12, 255), width=1)
        target = translate(edge, 1, 0)
        self.pair(edge, target)
        canonical, sources = t.validate_project(self.project)
        a = canonical['clips'][0]
        self.assertFalse(t.render_frame(a, sources[a['key']], a['end'], project=canonical)[1])
        self.assertGreater(np.sum(np.asarray(edge)[:, :, 3]), np.sum(np.asarray(target)[:, :, 3]))
        plan = self.analyze()
        row = plan['seams'][0]
        self.assertEqual(row['status'], 'REVIEW', row)
        # This fixture has a reliable global 1px correspondence. The bounded
        # registration must be evaluated and rejected for losing visible alpha,
        # rather than passing by matching the target's already-cropped content.
        self.assertTrue(row.get('candidate'), 'Report the attempted automatic candidate')
        self.assertTrue(row.get('failed_checks'), 'Report why its correction is unsafe')
        self.assertEqual(self.apply(plan)['applied']['seams'], 0)

    def test_already_similar_endpoints_do_not_hide_a_new_object_inside_fade(self):
        frames = [self.base.copy() for _ in range(12)]
        # Endpoint itself differs only by two gray levels in a small patch.
        pixels = np.asarray(frames[-1]).copy()
        pixels[110:125, 70:85, :3] += 2
        frames[-1] = Image.fromarray(pixels)
        ImageDraw.Draw(frames[9]).rectangle((128, 30, 153, 76), fill=(180, 180, 180, 255))
        self.pair(frames)
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW', plan['seams'][0])
        self.assertEqual(self.apply(plan)['applied']['seams'], 0)

    def test_sparse_alpha_is_not_mistaken_for_reliable_matching(self):
        sparse = Image.new('RGBA', self.base.size)
        ImageDraw.Draw(sparse).line((60, 60, 72, 72), fill=(80, 80, 80, 255), width=2)
        self.pair(translate(sparse, 1, 0), sparse)
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW', plan['seams'][0])
        self.assertEqual(self.apply(plan)['applied']['seams'], 0)


if __name__ == '__main__':
    unittest.main()
