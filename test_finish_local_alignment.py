"""Local-candidate regressions; only self-drawn diagrams in temporary jobs."""
import copy
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

import finish_plan as fp
import seam_alignment as sa
import transitions as t
import test_finish_registration_review as fixture
from experiments.alignment_synthetic_v37 import robot, warp


class FinishLocalAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.FinishRegistrationIndependentTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def robot_pair(self, amount=.25):
        target = robot()
        self.fx.pair(Image.fromarray(warp(target, amount=amount)), Image.fromarray(target))

    def test_boundary_preserving_local_improvement_can_actually_be_applied(self):
        self.robot_pair()
        original = copy.deepcopy(self.fx.project)
        hashes = self.fx.hashes()
        plan = self.fx.analyze()
        row = plan['seams'][0]
        self.assertEqual(row['candidate']['kind'], 'local', row)
        self.assertIn(row.get('apply_scope'), ('geometry', 'closure'), row)
        self.assertTrue(row['candidate_available'])
        self.assertEqual(sum(a['selected'] for a in row['attempts']), 1)
        result = self.fx.apply(plan)
        self.assertGreater(result['applied']['geometry'] + result['applied']['seams'], 0)
        self.assertTrue(sa.active(result['project']['clips'][0]['tail']['alignment']))
        self.assertEqual(self.fx.project, original)
        self.assertEqual(hashes, self.fx.hashes())
        project, sources = t.validate_project(result['project'])
        c = project['clips'][0]
        p, ps = t.validate_project(original)
        np.testing.assert_array_equal(t.render_frame(c, sources[c['key']], 5, project=project)[0],
                                      t.render_frame(p['clips'][0], ps[c['key']], 5, project=p)[0])

    def test_visible_support_stats_ignore_pure_transparent_extrapolation(self):
        rgba = np.zeros((100, 120, 4), dtype=np.uint8)
        rgba[20:80, 15:55] = [180, 180, 180, 255]
        dx = np.zeros((100, 120), dtype=np.float32)
        dx[:, 80:] = 20
        dy = np.zeros_like(dx)
        stats = sa.support_map_stats(rgba, rgba, dx, dy, .7)
        self.assertAlmostEqual(stats['support_max_px'], 0)
        self.assertAlmostEqual(stats['canvas_max_px'], 14)
        self.assertGreater(stats['support_pixels'], 2400)

    def test_contour_difference_is_not_presented_as_proven_missing_content(self):
        self.robot_pair(.5)
        plan = self.fx.analyze()
        row = plan['seams'][0]
        self.assertTrue(row.get('attempts'), row)
        for attempt in row['attempts']:
            if not attempt['rendered']:
                self.assertIsNone(attempt['after'])
            for check in attempt['failed_checks']:
                if check['code'] == 'registered_unsupported_alpha_pixels':
                    self.assertNotIn('缺失', check['label'])

    def test_each_strength_has_its_own_measurements_and_never_tries_zero(self):
        self.robot_pair(.5)
        row=self.fx.analyze()['seams'][0]
        trials=[item for item in row['attempts'] if item['kind']=='local']
        self.assertEqual([item['strength'] for item in trials],list(fp.LOCAL_STRENGTHS))
        self.assertTrue(all(item['strength']>0 for item in trials))
        self.assertGreater(len({item['after']['residual']['mean'] for item in trials if item['rendered']}),1)
        selected=next(item for item in trials if item['selected'])
        self.assertEqual(row['candidate']['after'],selected['after'])
        self.assertEqual(row['failed_checks'],selected['failed_checks'])
        self.assertNotIn('canvas_edge_risk',[item['code'] for item in row['failed_checks']])

    def test_global_map_fold_guard_is_still_hard_even_outside_foreground(self):
        dx=np.zeros((100,120),np.float32);dy=np.zeros_like(dx)
        dx[40:60,95:105]=-50
        with self.assertRaises(ValueError):
            sa._guard(dx,dy)

    def test_visible_cell_compression_is_not_hidden_by_centered_gradients(self):
        rgba=np.zeros((100,120,4),np.uint8)
        rgba[20:80,20:100]=[190,190,190,255]
        dx=np.zeros((100,120),np.float32);dy=np.zeros_like(dx)
        dx[10:90,10:110]=np.where(np.arange(100)%2,.3,-.3)
        self.assertGreater(sa._guard(dx,dy)['all_strengths_min'],.2)
        stats=sa.support_map_stats(rgba,rgba,dx,dy,1)
        self.assertLess(stats['jacobian_min'],.65)
        self.assertIn('local_jacobian',[c['code'] for c in fp._local_map_failures(stats,12)])

    def test_source_sampling_support_detects_erased_thin_alpha(self):
        original=np.zeros((100,120,4),np.uint8)
        original[15:85,15:75]=[190,190,190,255]
        original[50,75:110]=[30,30,30,255]
        erased=original.copy();erased[50,75:110]=0
        dx=np.zeros((100,120),np.float32);dy=np.zeros_like(dx)
        self.assertGreater(sa.sampling_support_loss(original,erased,dx,dy,1),30)
        self.assertEqual(sa.sampling_support_loss(original,original,dx,dy,1),0)

    def test_three_clip_batch_mixes_local_geometry_and_similarity_closure(self):
        target=Image.new('RGBA',(440,680));target.paste(Image.fromarray(robot()),(40,20))
        local=Image.new('RGBA',target.size);local.paste(Image.fromarray(warp(robot(),amount=.5)),(40,20))
        a=self.fx.source(0,[local]*12)
        b=self.fx.source(1,[target]*6+[fixture.translate(target,1)]*6)
        c=self.fx.source(2,[target]*12)
        self.fx.project.update(clips=[a,b,c],sequence=[a['key'],b['key'],c['key']],route_mode='open')
        self.fx.project=t.handle('save',dict(project=self.fx.project))
        plan=self.fx.analyze()
        self.assertEqual([r['apply_scope'] for r in plan['seams']],['geometry','closure'],plan['seams'])
        self.assertEqual(plan['summary']['geometry_fixes'],1)
        self.assertEqual(plan['summary']['seam_fixes'],1)
        result=self.fx.apply(plan)
        project,sources=t.validate_project(result['project'])
        self.assertEqual(project['seam_closure']['schema'],3)
        self.assertTrue(sa.active(project['clips'][0]['tail']['alignment']))
        b,c=project['clips'][1:]
        np.testing.assert_array_equal(t.render_frame(b,sources[b['key']],b['end'],project=project)[0],
                                      t.render_frame(c,sources[c['key']],c['start'],project=project)[0])
        self.assertEqual(result,self.fx.apply(plan),'Apply retry is one idempotent transaction')


if __name__ == '__main__':
    unittest.main()
