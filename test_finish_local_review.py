"""Independent local finishing acceptance; synthetic diagrams only.

Controlled-map tests isolate candidate gates. The real-solver test uses a
separately generated robot with analytic multi-part deformation and clipped
side arms; it does not fabricate solver metrics or inspect any user assets.
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

import finish_plan as f
import transitions as t
from experiments.alignment_synthetic_v37 import robot, warp, independent_metrics


class LocalFinishingIndependentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='finish-local-independent-')
        self.root = Path(self.tmp.name)
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        self.patches = [patch.object(t.e, 'ROOT', self.root), patch.object(t.e, 'JOBS', self.jobs),
                        patch.dict(t.e.PATHS, {}, clear=True), patch.dict(t.e.ACTIVE, {}, clear=True),
                        patch.dict(t.le.RUNS, {}, clear=True), patch.dict(t.RUNS, {}, clear=True)]
        for item in self.patches:
            item.start()
        self.addCleanup(self.cleanup)
        self.project = t.handle('create', dict(name='Independent local deformation', route_mode='open'))

    def cleanup(self):
        t.ts.lc.clear_cache()
        t.le.sa.clear_cache()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def source(self, ordinal, frames):
        jid, key = 'ab'[ordinal] * 32, '12'[ordinal] * 32
        folder = self.jobs / jid / 'transparent_png'
        folder.mkdir(parents=True)
        records = []
        for index, pixels in enumerate(frames, 1):
            name = f'frame_{index:08d}.png'
            Image.fromarray(np.asarray(pixels)).save(folder / name)
            records.append(dict(file=name, status='done'))
        h, w = np.asarray(frames[0]).shape[:2]
        t.e.atomic_json(folder.parent / 'job.json', dict(id=jid, name=f'Synthetic local {ordinal}',
            state='complete', frames=records, dimensions=[w, h], config=dict(fps=24), paths=dict(png='transparent_png')))
        return dict(key=key, job_id=jid, version='original', label=f'Local {ordinal}', start=1,
                    end=len(frames), head_frames=3, tail_frames=5, tone=0, contrast=0, head={}, tail={})

    def pair(self, source, target):
        frames = source if isinstance(source, list) else [source] * 12
        self.project['clips'] = [self.source(0, frames), self.source(1, [target] * 12)]
        self.project['sequence'] = [clip['key'] for clip in self.project['clips']]
        self.project = t.handle('save', dict(project=self.project))

    def hashes(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.jobs.glob('*/transparent_png/*.png')}

    def analyze(self):
        before, hashes = copy.deepcopy(self.project), self.hashes()
        saved = (t.projectdir(self.project['id']) / 'project.json').read_bytes()
        plan = t.handle('finish_analyze', dict(project=self.project))['plan']
        self.assertEqual(self.project, before)
        self.assertEqual(self.hashes(), hashes)
        self.assertEqual((t.projectdir(self.project['id']) / 'project.json').read_bytes(), saved)
        return plan

    def apply(self, plan):
        return t.handle('finish_apply', dict(project=self.project, plan_id=plan['id'], signature=plan['signature']))

    @staticmethod
    def decoded(value):
        with Image.open(io.BytesIO(base64.b64decode(value.split(',')[-1]))) as image:
            return np.asarray(image.convert('RGBA')).copy()

    def controlled_local(self, dx, dy, target_strength=1., public=False, middle_alpha=False):
        """Isolate selection with a valid persisted model and known dense field."""
        source = np.zeros((*dx.shape, 4), np.uint8)
        drawing = robot(width=200, height=500)
        source[70:570, 330:530] = drawing
        target = t.le.sa.resample_frame(source, dx, dy, target_strength)
        if middle_alpha:
            frames = [source.copy() for _ in range(12)]
            y, x = np.unravel_index(np.argmax(np.hypot(dx, dy)), dx.shape)
            frames[10][y-6:y+7, x-6:x+7] = [230, 30, 210, 255]
            self.pair(frames, target)
        else:
            self.pair(source, target)
        project, sources = t.validate_project(self.project)
        a, b = project['clips']
        t.le.sa._dependencies()
        stats = t.le.sa._guard(dx, dy)
        h, w = dx.shape

        def fitted(left, right, provenance):
            controls = [dict(source=[x, y], target=[x, y])
                        for y in (h * .25, h * .5, h * .75) for x in (w * .25, w * .5, w * .75)]
            model = dict(schema=1, dimensions=[w, h], controls=controls,
                         boundaries={side: dict(source=[0, h-1], target=[0, h-1]) for side in ('left', 'right')},
                         regularization=.00008, scale=1., provenance=provenance)
            report = dict(max_displacement_px=float(np.hypot(dx, dy).max()), jacobian=stats,
                          guard_scale=1., warnings=[], dimensions=[w, h])
            return dict(enabled=True, strength=100., model=model), report

        with patch.object(t.le.sa, 'build_alignment', side_effect=fitted), \
                patch.object(t.le.sa, 'maps', return_value=(dx, dy)):
            if public:
                plan = self.analyze()
                row = plan['seams'][0]
                self.assertEqual(row.get('apply_scope'), 'geometry', row)
                applied = self.apply(plan)
                self.assertEqual(applied['applied'].get('geometry'), 1)
                self.assertEqual(applied['applied']['seams'], 0)
                candidate, actual_sources = t.validate_project(applied['project'])
                clip = candidate['clips'][0]
                rendered = np.asarray(t.render_frame(clip, actual_sources[clip['key']], 12, project=candidate)[0])
                old_error = np.mean(np.abs(source.astype(float)-target))
                new_error = np.mean(np.abs(rendered.astype(float)-target))
                self.assertLess(new_error, old_error * .7, 'The selected partial strength must really reach the renderer')
                summary, checks = dict(row['candidate'], attempts=row['attempts']), row['failed_checks']
            else:
                candidate, summary, checks = f._local_review_candidate(project, sources, a, b,
                    t.metrics(a, b, sources, project), f._geometry(project, sources, a, b))
        return candidate, summary, checks, source, target

    def test_transparent_map_outlier_does_not_veto_small_visible_correction(self):
        yy, xx = np.mgrid[:640, :640].astype(np.float32)
        gate = np.sin(np.pi * xx / 639) ** 2 * np.sin(np.pi * yy / 639) ** 2
        transparent = 40 * np.exp(-((xx - 115) / 85) ** 2 - ((yy - 300) / 150) ** 2) * gate
        visible = 2 * np.sin(2 * np.pi * yy / 639) * gate
        dx, dy = np.asarray(transparent + visible, np.float32), np.zeros_like(xx)
        candidate, summary, checks, source, _ = self.controlled_local(dx, dy)
        self.assertGreater(float(np.hypot(dx, dy).max()), 12)
        self.assertLess(float(np.hypot(dx, dy)[source[:, :, 3] > 64].max()), 3)
        self.assertIsNotNone(candidate, checks)
        self.assertGreater(summary['improvement'], .30)
        self.assertGreater(summary['displacement']['canvas_max_px'], 12)
        self.assertLess(summary['displacement']['support_max_px'], 3)

    def test_lower_strength_is_tried_when_full_visible_warp_is_too_large(self):
        yy, xx = np.mgrid[:640, :640].astype(np.float32)
        dx = 28 * np.sin(np.pi * xx / 639) ** 2 * np.sin(2 * np.pi * yy / 639)
        dx *= np.sin(np.pi * yy / 639) ** 2
        dy = np.zeros_like(dx)
        candidate, summary, checks, source, _ = self.controlled_local(dx, dy, .5, public=True)
        self.assertGreater(float(np.abs(dx[source[:, :, 3] > 64]).max()), 12)
        self.assertIsNotNone(candidate, checks)
        strength = candidate['clips'][0]['tail']['alignment']['strength']
        self.assertGreater(strength, 0)
        self.assertLess(strength, 100)
        self.assertGreater(summary['improvement'], .30)
        trials = [attempt for attempt in summary['attempts'] if attempt['kind'] == 'local']
        chosen = [attempt for attempt in trials if attempt['selected']]
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]['strength'], strength)
        self.assertTrue(chosen[0]['rendered'])
        self.assertIsNotNone(chosen[0]['after'])
        full = next(attempt for attempt in trials if attempt['strength'] == 100)
        self.assertFalse(full['rendered'])
        self.assertIsNone(full['after'], 'Rejected maps must not display uncomputed after metrics')

    def test_new_foreground_inside_fade_reduces_previously_safe_endpoint_strength(self):
        yy, xx = np.mgrid[:640, :640].astype(np.float32)
        gate = np.sin(np.pi * xx / 639) ** 2 * np.sin(np.pi * yy / 639) ** 2
        transparent = 40 * np.exp(-((xx - 115) / 85) ** 2 - ((yy - 300) / 150) ** 2) * gate
        visible = 2 * np.sin(2 * np.pi * yy / 639) * gate
        dx, dy = np.asarray(transparent + visible, np.float32), np.zeros_like(xx)
        candidate, summary, checks, _, _ = self.controlled_local(dx, dy, middle_alpha=True)
        self.assertIsNotNone(candidate, checks)
        self.assertLess(candidate['clips'][0]['tail']['alignment']['strength'], 100,
                        'An endpoint-transparent outlier becomes relevant when a fade frame contains foreground there')
        full = next(attempt for attempt in summary['attempts'] if attempt['strength'] == 100)
        self.assertTrue(any(item['code'] == 'local_displacement' and item['stage'] == 'window'
                            for item in full['failed_checks']), full)

    def test_real_boundary_local_deformation_can_be_adopted_and_exported(self):
        target = robot()
        source = warp(target, amount=.25)
        self.assertTrue(np.any(target[:, 0, 3] > 0) and np.any(target[:, -1, 3] > 0))
        self.pair(source, target)
        originals, saved = self.hashes(), (t.projectdir(self.project['id']) / 'project.json').read_bytes()
        plan = self.analyze()
        row = plan['seams'][0]
        self.assertTrue(row['candidate_available'], row)
        self.assertEqual(row['candidate']['kind'], 'local')
        self.assertIn(row.get('apply_scope'), ('geometry', 'closure'), row)
        a, b = [clip['key'] for clip in self.project['clips']]
        candidate = t.handle('finish_candidate_preview', dict(project=self.project, plan_id=plan['id'],
                    signature=plan['signature'], a=a, b=b))
        after = self.decoded(candidate['after']['left'])
        before_metrics, after_metrics = independent_metrics(source, target), independent_metrics(after, target)
        self.assertLess(after_metrics['silhouette_mismatch_pixels'], before_metrics['silhouette_mismatch_pixels'])
        self.assertLess(after_metrics['interior_ink_chamfer_px'], before_metrics['interior_ink_chamfer_px'])
        result = self.apply(plan)
        self.assertEqual(result['applied']['seams'] + result['applied'].get('geometry', 0), 1)
        fixed, sources = t.validate_project(result['project'])
        left, right = fixed['clips']
        np.testing.assert_array_equal(t.render_frame(left, sources[a], 5, project=fixed)[0], source)
        if row['apply_scope'] == 'closure':
            np.testing.assert_array_equal(t.render_frame(left, sources[a], 12, project=fixed)[0],
                                          t.render_frame(right, sources[b], 1, project=fixed)[0])
        else:
            np.testing.assert_array_equal(t.render_frame(left, sources[a], 12, project=fixed)[0], after)
        for index in range(1, 13):
            rendered, clipped = t.render_frame(left, sources[a], index, project=fixed)
            self.assertFalse(clipped)
            self.assertEqual(rendered.size, (360, 640))
            self.assertTrue(np.any(np.asarray(rendered)[:, 0, 3] > 0))
            self.assertTrue(np.any(np.asarray(rendered)[:, -1, 3] > 0))
        self.assert_preview_export(fixed, sources, expect_closure=row['apply_scope'] == 'closure')
        self.assertEqual(self.hashes(), originals)
        self.assertEqual((t.projectdir(fixed['id']) / 'project.json').read_bytes(), saved)

    def assert_preview_export(self, fixed, sources, expect_closure):
        a, b = [clip['key'] for clip in fixed['clips']]
        preview = t.render_preview(fixed, sources, a, b, span=5)
        pid, version = fixed['id'], 'v001'
        output = t._version_dir(pid, version)
        output.mkdir(parents=True)
        t.RUNS[pid] = dict(state='running', version=version)
        t.export_worker(fixed, sources, version, t.project_signature(fixed, sources))
        self.assertEqual(t.RUNS[pid]['state'], 'complete', t.RUNS[pid].get('phase'))
        manifest = t._read_json(output / 'manifest.json')
        exported = {clip['key']: clip for clip in manifest['clips']}
        if expect_closure:
            self.assertTrue(manifest['seam_verification']['all_equal'])
        else:
            self.assertFalse(manifest.get('seam_verification'))
        for frame in preview['frames']:
            path = t.projectdir(pid) / (exported[frame['clip_key']]['png_pattern'] % frame['frame'])
            with Image.open(path) as image:
                np.testing.assert_array_equal(image, self.decoded(frame['image']))

    def test_real_partial_geometry_is_applied_without_claiming_closed_seam(self):
        target = robot()
        source = warp(target, amount=.5)
        self.pair(source, target)
        hashes = self.hashes()
        saved = (t.projectdir(self.project['id']) / 'project.json').read_bytes()
        plan = self.analyze()
        row = plan['seams'][0]
        self.assertEqual(row['status'], 'REVIEW', row)
        self.assertEqual(row.get('apply_scope'), 'geometry', row)
        self.assertEqual(plan['summary'].get('geometry_fixes'), 1)
        result = self.apply(plan)
        self.assertEqual(result['applied'].get('geometry'), 1)
        self.assertEqual(result['applied']['seams'], 0)
        self.assertTrue(result['review_items'])
        fixed, sources = t.validate_project(result['project'])
        self.assertFalse(t.ts.active(fixed))
        clip = fixed['clips'][0]
        corrected = np.asarray(t.render_frame(clip, sources[clip['key']], 12, project=fixed)[0])
        before, after = independent_metrics(source, target), independent_metrics(corrected, target)
        self.assertLess(after['silhouette_mismatch_pixels'], before['silhouette_mismatch_pixels'])
        self.assertLess(after['interior_ink_chamfer_px'], before['interior_ink_chamfer_px'])
        self.assertFalse(np.array_equal(corrected, target), 'Geometry-only must not replace source with the target')
        np.testing.assert_array_equal(t.render_frame(clip, sources[clip['key']], 5, project=fixed)[0], source)
        self.assert_preview_export(fixed, sources, expect_closure=False)
        self.assertEqual(self.hashes(), hashes)
        self.assertEqual((t.projectdir(fixed['id']) / 'project.json').read_bytes(), saved)

    def test_geometry_only_keeps_new_detail_in_middle_of_fade(self):
        target = robot()
        frames = [warp(target, amount=.5) for _ in range(12)]
        detail = Image.fromarray(frames[9])
        ImageDraw.Draw(detail).rectangle((172, 315, 192, 345), fill=(230, 30, 210, 255))
        frames[9] = np.asarray(detail).copy()
        self.pair(frames, target)
        plan = self.analyze()
        row = plan['seams'][0]
        self.assertEqual(row['status'], 'REVIEW', row)
        self.assertEqual(row.get('apply_scope'), 'geometry', row)
        result = self.apply(plan)
        self.assertEqual(result['applied'].get('geometry'), 1)
        self.assertEqual(result['applied']['seams'], 0)
        fixed, sources = t.validate_project(result['project'])
        clip = fixed['clips'][0]
        actual, clipped = t.render_frame(clip, sources[clip['key']], 10, project=fixed)
        pixels = np.asarray(actual)
        marker = (pixels[:, :, 0] > 180) & (pixels[:, :, 1] < 70) & (pixels[:, :, 2] > 180)
        self.assertGreater(np.count_nonzero(marker), 400, 'Geometry-only must retain genuine intermediate content')
        self.assertFalse(clipped)
        self.assertFalse(t.ts.active(fixed), 'A changing intermediate detail must not be blended to a static anchor')

    def test_new_thin_alpha_cannot_be_erased_by_local_candidate(self):
        target = robot()
        source = Image.fromarray(warp(target, amount=.25))
        ImageDraw.Draw(source).line((170, 43, 170, 12), fill=(30, 30, 30, 255), width=1)
        self.pair(np.asarray(source), target)
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW', plan['seams'][0])
        result = self.apply(plan)
        self.assertEqual(result['applied']['seams'], 0)
        fixed, sources = t.validate_project(result['project'])
        clip = fixed['clips'][0]
        actual = np.asarray(t.render_frame(clip, sources[clip['key']], 12, project=fixed)[0])
        self.assertGreater(np.count_nonzero(actual[:35, 155:185, 3] > 64), 10,
                           'The unmatched thin extension must remain after any geometry-only correction')

    def test_missing_thin_alpha_is_not_invented_by_local_candidate(self):
        base = robot()
        target = Image.fromarray(base)
        ImageDraw.Draw(target).line((170, 43, 170, 12), fill=(30, 30, 30, 255), width=1)
        self.pair(warp(base, amount=.25), np.asarray(target))
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW', plan['seams'][0])
        result = self.apply(plan)
        self.assertEqual(result['applied']['seams'], 0)
        fixed, sources = t.validate_project(result['project'])
        clip = fixed['clips'][0]
        actual = np.asarray(t.render_frame(clip, sources[clip['key']], 12, project=fixed)[0])
        self.assertEqual(np.count_nonzero(actual[:35, :, 3] > 64), 0,
                         'Geometry-only must not synthesize a missing extension from the target')

    def test_brightness_difference_is_not_adopted_as_local_geometry(self):
        target = robot()
        source = target.copy()
        region = source[:, :, 3] > 0
        source[region, :3] = np.clip(source[region, :3].astype(int) - 45, 0, 255)
        self.pair(source, target)
        plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW', plan['seams'][0])
        applied = self.apply(plan)['applied']
        self.assertEqual(applied['seams'], 0)
        self.assertEqual(applied.get('geometry', 0), 0, 'Brightness alone is not a geometry correction')

    def test_existing_manual_geometry_is_preserved_without_local_refit(self):
        target = robot()
        self.pair(warp(target, amount=.25), target)
        self.project['clips'][0]['tail']['dx'] = .25
        with patch.object(t.le.sa, 'build_alignment', side_effect=AssertionError('Manual geometry was overwritten')):
            plan = self.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW')
        fixed = self.apply(plan)['project']
        self.assertEqual(fixed['clips'][0]['tail']['dx'], .25)


if __name__ == '__main__':
    unittest.main()
