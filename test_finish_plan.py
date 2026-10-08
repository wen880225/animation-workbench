"""Conservative batch finishing plans: deterministic fixtures, no model calls."""
import copy
import hashlib
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
import test_transition_seams as fixture
import transitions as t


class FinishPlanTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.TransitionSeamTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = copy.deepcopy(self.fixture.project)
        self.project['clips'] = self.project['clips'][:3]
        self.project['sequence'] = [clip['key'] for clip in self.project['clips']]
        self.project['route_mode'] = 'open'
        self.project['shared_tone'] = dict(tone=0, contrast=0)
        y, x = np.indices((80, 64))
        self.base = np.zeros((80, 64, 4), dtype=np.uint8)
        mask = (x >= 14) & (x <= 49) & (y >= 10) & (y <= 69)
        self.base[mask, 3] = 255
        for channel in range(3):
            self.base[:, :, channel][mask] = (55 + x + y)[mask]
        for clip, bias in zip(self.project['clips'], (0, 2, 60)):
            clip.update(head={}, tail={}, tone=0, contrast=0)
            pixels = self.base.copy()
            pixels[:, :, :3][mask] += bias
            if bias == 2:
                pixels[22:26, 20:40, :3] += 2
            source = t.resolve_source(clip['job_id'])
            for path in source.files:
                Image.fromarray(pixels).save(path)
        self.project = t.handle('save', dict(project=self.project))

    def analyze(self, project=None):
        return t.handle('finish_analyze', dict(project=project or self.project))['plan']

    def apply(self, plan, project=None):
        return t.handle('finish_apply', dict(project=project or self.project,
            plan_id=plan['id'], signature=plan['signature']))

    def test_analyze_does_not_mutate_sources_or_project_and_partial_apply(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.fixture.jobs.glob('*/transparent_png/*.png')}
        saved = (t.projectdir(self.project['id']) / 'project.json').read_bytes()
        plan = self.analyze()
        self.assertTrue(plan['review_items'])
        self.assertEqual(sum(row['status'] == 'FIX' for row in plan['seams']), 1)
        result = self.apply(plan)
        p, sources = t.validate_project(result['project'])
        self.assertEqual(p['seam_closure']['schema'], 2)
        self.assertEqual(len(p['seam_closure']['links']), 1)
        tone_report = p['tone_match']['report']
        rejected = tone_report['clips'][2]
        self.assertFalse(rejected.get('applied'))
        self.assertEqual(rejected['tone'], 0)
        self.assertEqual(rejected['contrast'], 0)
        self.assertEqual(rejected['after'], rejected['before'])
        self.assertEqual(tone_report['adjusted_count'], 1)
        first, second, third = p['clips']
        np.testing.assert_array_equal(t.render_frame(first, sources[first['key']], first['end'], project=p)[0],
                                      t.render_frame(second, sources[second['key']], second['start'], project=p)[0])
        np.testing.assert_array_equal(t.render_frame(third, sources[third['key']], third['start'], project=p)[0],
                                      t._render_base_frame(third, sources[third['key']], third['start'], project=p)[0])
        self.assertEqual((t.projectdir(p['id']) / 'project.json').read_bytes(), saved)
        self.assertFalse(any(row.get('verdict') == 'pass' for row in p['reviews'].values()))
        for path, digest in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual(self.apply(plan)['project'], result['project'], 'retry returns the same fixed plan result')
        output = t._version_dir(p['id'], 'v001')
        output.mkdir(parents=True)
        t.RUNS[p['id']] = dict(state='running', version='v001')
        t.export_worker(p, sources, 'v001', t.project_signature(p, sources))
        self.assertEqual(t.RUNS[p['id']]['state'], 'complete', t.RUNS[p['id']].get('phase'))
        manifest = t._read_json(output / 'manifest.json')
        verification = manifest['seam_verification']
        self.assertEqual(len(verification['pairs']), 2)
        self.assertEqual([row['corrected'] for row in verification['pairs']], [True, False])
        self.assertFalse(verification['all_equal'])
        self.assertEqual(manifest['route_mode'], 'open')

    def test_old_plan_rejects_recipe_or_source_changes_and_forged_signature(self):
        plan = self.analyze()
        changed = copy.deepcopy(self.project)
        changed['clips'][0]['tone'] = 1
        with self.assertRaisesRegex(ValueError, '重新分析'):
            self.apply(plan, changed)
        forged = dict(plan, signature='0' * 64)
        with self.assertRaises(ValueError):
            self.apply(forged)
        source = t.resolve_source(self.project['clips'][0]['job_id'])
        Image.new('RGBA', (64, 80), (90, 90, 90, 255)).save(source.files[0])
        with self.assertRaisesRegex(ValueError, '重新分析'):
            self.apply(plan)

    def test_large_or_local_shape_changes_are_reviewed_not_anchored(self):
        p = copy.deepcopy(self.project)
        p['clips'] = p['clips'][:2]
        p['sequence'] = p['sequence'][:2]
        source = t.resolve_source(p['clips'][1]['job_id'])
        damaged = self.base.copy()
        damaged[25:45, 20:30, :3] = 0  # Local discontinuity, same silhouette.
        for path in source.files:
            Image.fromarray(damaged).save(path)
        plan = self.analyze(p)
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW')
        self.assertEqual(self.apply(plan, p)['applied']['seams'], 0)

    def test_existing_closure_is_preserved_without_silent_replacement(self):
        p = t.handle('build_seams', dict(project=self.project))['project']
        plan = self.analyze(p)
        result = self.apply(plan, p)
        self.assertEqual(result['project']['seam_closure'], p['seam_closure'])
        self.assertEqual(result['applied'], dict(appearance=0, seams=0, geometry=0, repairs=0))
        self.assertTrue(result['review_items'])

    def test_stale_generated_closure_can_be_reanalyzed_as_explicit_repair(self):
        p = t.handle('build_seams', dict(project=self.project))['project']
        p['sequence'] = list(reversed(p['sequence']))
        original = copy.deepcopy(p)
        saved = (t.projectdir(p['id']) / 'project.json').read_bytes()
        plan = self.analyze(p)
        self.assertEqual([row['kind'] for row in plan['repairs']], ['seam_closure'])
        self.assertEqual(plan['summary']['repair_count'], 1)
        self.assertEqual(p, original, 'Analyze leaves stale settings available for Undo')
        self.assertEqual((t.projectdir(p['id']) / 'project.json').read_bytes(), saved)
        result = self.apply(plan, p)
        repaired = result['project']
        self.assertEqual(result['applied']['repairs'], 1)
        self.assertEqual(repaired['sequence'], p['sequence'])
        t.validate_project(repaired)
        if not result['applied']['seams']:
            self.assertFalse(repaired['seam_closure']['enabled'])
            self.assertEqual(repaired['seam_closure']['links'], p['seam_closure']['links'])
        self.assertEqual(self.apply(plan, p), result)
        changed = copy.deepcopy(p)
        changed['clips'][0]['tone'] += 1
        with self.assertRaisesRegex(ValueError, '重新分析'):
            self.apply(plan, changed)

    def test_stale_tone_and_closure_repair_retains_manual_values_and_validates_source(self):
        p = self.apply(self.analyze())['project']
        p['clips'][0]['tone'] = 2
        before = copy.deepcopy(p)
        plan = self.analyze(p)
        self.assertEqual({row['kind'] for row in plan['repairs']}, {'tone_match', 'seam_closure'})
        self.assertEqual(p, before)
        result = self.apply(plan, p)
        self.assertEqual(result['applied']['repairs'], 2)
        self.assertEqual(result['project']['clips'][0]['tone'], 2)
        t.validate_project(result['project'])
        invalid = copy.deepcopy(p)
        invalid['clips'][0]['job_id'] = '0' * 32
        with self.assertRaises((ValueError, OSError)):
            self.analyze(invalid)
        invalid = copy.deepcopy(p)
        invalid['clips'][0]['tail']['alignment'] = dict(enabled=True)
        with self.assertRaisesRegex(ValueError, '對位'):
            self.analyze(invalid)

    def test_single_open_and_single_loop_use_same_plan_without_fake_seams(self):
        p = copy.deepcopy(self.project)
        p['clips'] = p['clips'][:1]
        p['sequence'] = p['sequence'][:1]
        plan = self.analyze(p)
        self.assertEqual(plan['seams'], [])
        p['route_mode'] = 'loop'
        plan = self.analyze(p)
        self.assertEqual(len(plan['seams']), 1)
        self.assertEqual(plan['seams'][0]['status'], 'OK')

    def test_sparse_foreground_is_reviewed_even_when_endpoints_match(self):
        p = copy.deepcopy(self.project)
        p['clips'] = p['clips'][:1]
        p['sequence'] = p['sequence'][:1]
        p['route_mode'] = 'loop'
        sparse = np.zeros_like(self.base)
        sparse[20:24, 20:24] = [100, 100, 100, 255]
        for path in t.resolve_source(p['clips'][0]['job_id']).files:
            Image.fromarray(sparse).save(path)
        plan = self.analyze(p)
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW')
        self.assertEqual(self.apply(plan, p)['applied']['seams'], 0)

    def test_bounded_boundary_candidate_is_review_only_and_does_not_trim(self):
        p = copy.deepcopy(self.project)
        p['clips'] = p['clips'][:2]
        p['sequence'] = p['sequence'][:2]
        source = t.resolve_source(p['clips'][0]['job_id'])
        changed = self.base.copy()
        changed[changed[:, :, 3] > 0, :3] += 30
        Image.fromarray(changed).save(source.files[-1])
        plan = self.analyze(p)
        self.assertTrue(plan['search'])
        row = plan['search'][0]
        self.assertEqual(row['trim_tail'], 1)
        self.assertLessEqual(row['trim_tail'], len(source.files) * .1)
        applied = self.apply(plan, p)['project']
        self.assertEqual([(c['start'], c['end']) for c in applied['clips']], [(c['start'], c['end']) for c in p['clips']])
        self.assertEqual(row['status'], 'REVIEW')

    def test_expired_plan_requires_reanalysis_and_existing_alignment_is_not_overwritten(self):
        import finish_plan as fp
        plan = self.analyze()
        with patch.object(fp, 'TTL', -1):
            with self.assertRaisesRegex(ValueError, '重新分析'):
                self.apply(plan)
        # A valid alignment's presence owns this decision; no parallel auto-tone
        # system may silently change its reference appearance.
        project = copy.deepcopy(self.project)
        project['clips'][0]['tail']['alignment'] = dict(enabled=True)
        canonical, sources = t.validate_project(self.project)
        corrected, rows, reviews, count = fp._appearance(project, sources)
        self.assertEqual(count, 0)
        self.assertEqual(corrected, project)
        self.assertEqual(reviews[0]['code'], 'existing_correction')

    def test_large_motion_inside_fade_window_is_reviewed_before_morph(self):
        p = copy.deepcopy(self.project)
        p['clips'] = p['clips'][:2]
        p['sequence'] = p['sequence'][:2]
        p['clips'][0]['tail_frames'] = 5
        motion = self.base.copy()
        motion[10:70, 5:14] = [130, 130, 130, 255]
        source = t.resolve_source(p['clips'][0]['job_id'])
        Image.fromarray(motion).save(source.files[-3])
        plan = self.analyze(p)
        self.assertEqual(plan['seams'][0]['status'], 'REVIEW')
        self.assertEqual(plan['seams'][0]['code'], 'transition_window_outside_safe_range')
        self.assertEqual(self.apply(plan, p)['applied']['seams'], 0)

    def test_self_loop_boundary_suggestions_respect_combined_fade_window(self):
        import finish_plan as fp
        clip = dict(key='x', label='X', start=1, end=30, head_frames=14, tail_frames=14)
        before = dict(silhouette=10, appearance=10, motion=10)
        def measure(a, b, *args):
            if a['end'] == 29 and b['start'] == 3:
                return dict(silhouette=0, appearance=0, motion=0)
            return before
        with patch.object(t, 'metrics', side_effect=measure):
            proposed = fp._boundary_candidate({}, {}, clip, clip, before, {'x': 3})
        self.assertIsNone(proposed, 'combined 27 frames cannot hold both 14-frame windows')

    def test_boundary_suggestion_keeps_room_for_other_end_already_suggested(self):
        import finish_plan as fp
        a = dict(key='a', label='A', start=1, end=30, head_frames=14, tail_frames=14)
        b = dict(key='b', label='B', start=1, end=30, head_frames=10, tail_frames=10)
        before = dict(silhouette=10, appearance=10, motion=10)
        def measure(left, right, *args):
            return dict(silhouette=0, appearance=0, motion=0) if left['end'] == 28 and right['start'] == 1 else before
        with patch.object(t, 'metrics', side_effect=measure):
            proposed = fp._boundary_candidate({}, {}, a, b, before, {'a': 2, 'b': 3})
        self.assertIsNone(proposed, 'A already lost one head frame in another suggested boundary')


if __name__ == '__main__':
    unittest.main()
