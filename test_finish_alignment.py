"""Registration regressions use generated diagrams and temporary job folders."""
import copy
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

import transitions as t
import test_finish_registration_review as fixture


class FinishAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.FinishRegistrationIndependentTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def test_three_pixel_translation_is_a_parameter_correction(self):
        self.fx.pair(fixture.translate(self.fx.base, 3))
        plan = self.fx.analyze()
        row = plan['seams'][0]
        self.assertEqual(row['status'], 'FIX', row)
        self.assertTrue(row['candidate_available'])
        self.assertEqual(row['candidate']['kind'], 'similarity')
        result = self.fx.apply(plan)
        self.assertEqual(result['project']['clips'][0]['tail']['dx'], 0, 'do not double-apply a saved endpoint affine')
        self.assertAlmostEqual(result['project']['seam_closure']['links'][0]['registration']['tail'][-1]['dx'], -3, delta=.2)
        project,sources=t.validate_project(result['project'])
        with patch.object(t.ts.lc,'morph',side_effect=AssertionError('registered links must never call optical flow')):
            t.render_frame(project['clips'][0],sources[project['clips'][0]['key']],10,project=project)
        self.fx.assert_preserved_and_closed(plan)

    def test_single_self_loop_uses_the_same_registration_and_closure(self):
        frames = [self.fx.base] * 6 + [fixture.translate(self.fx.base, 1)] * 6
        clip = self.fx.source(0, frames)
        self.fx.project.update(clips=[clip], sequence=[clip['key']], route_mode='loop')
        self.fx.project = t.handle('save', dict(project=self.fx.project))
        plan = self.fx.analyze()
        self.assertEqual(plan['seams'][0]['status'], 'FIX', plan['seams'][0])
        result = self.fx.apply(plan)
        project, sources = t.validate_project(result['project'])
        a = project['clips'][0]
        np.testing.assert_array_equal(t.render_frame(a, sources[a['key']], a['start'], project=project)[0],
                                      t.render_frame(a, sources[a['key']], a['end'], project=project)[0])

    def test_candidate_preview_is_read_only_and_rejects_a_changed_draft(self):
        self.fx.pair(fixture.translate(self.fx.base, 1))
        plan = self.fx.analyze()
        original = copy.deepcopy(self.fx.project)
        args = dict(project=original, plan_id=plan['id'], signature=plan['signature'],
                    a=plan['seams'][0]['a'], b=plan['seams'][0]['b'])
        before_files = sorted(str(p) for p in self.fx.root.rglob('*'))
        preview = t.handle('finish_candidate_preview', args)
        self.assertIn('left', preview['before'])
        self.assertIn('right', preview['after'])
        self.assertEqual(original, self.fx.project)
        self.assertEqual(sorted(str(p) for p in self.fx.root.rglob('*')), before_files)
        args['project']['clips'][0]['tone'] = 1
        with self.assertRaisesRegex(ValueError, '重新分析'):
            t.handle('finish_candidate_preview', args)

    def test_large_translation_has_concrete_failed_checks_and_is_not_applied(self):
        self.fx.pair(fixture.translate(self.fx.base, -20))
        plan = self.fx.analyze()
        row = plan['seams'][0]
        self.assertEqual(row['status'], 'REVIEW')
        self.assertTrue(row['failed_checks'])
        self.assertEqual(self.fx.apply(plan)['applied']['seams'], 0)

    def test_continuous_four_pixel_subpixel_motion_uses_geometric_interpolation(self):
        # Same neutral robot geometry as the independently constructed GUI case.
        w,h = 480,864
        base=Image.new('RGBA',(w,h));d=ImageDraw.Draw(base)
        d.rounded_rectangle((148,50,332,250),35,fill='#d3d3d3',outline='#333333',width=3)
        d.rounded_rectangle((130,275,350,620),25,fill='#c9c9c9',outline='#333333',width=3)
        d.rectangle((204,248,276,275),fill='#a9a9a9',outline='#333333',width=3)
        for x in [76,350]:d.rounded_rectangle((x,310,x+54,585),20,fill='#d3d3d3',outline='#333333',width=3)
        for x in [150,275]:d.rounded_rectangle((x,616,x+54,806),12,fill='#b6b6b6',outline='#333333',width=3)
        for x in [186,274]:d.ellipse((x,124,x+22,146),fill='#555555')
        d.arc((190,160,290,210),0,180,fill='#555555',width=3)
        for y in [326,388,450,512]:
            for x in [156,246]:
                d.rounded_rectangle((x,y,x+54,y+38),8,outline='#555555',width=2)
                d.line((x+10,y+9,x+35,y+20,x+12,y+29),fill='#777777',width=2)
        frames=[base.transform((w,h),Image.Transform.AFFINE,(1,0,-4*i/23,0,1,0),
                               resample=Image.Resampling.BICUBIC) for i in range(24)]
        clip=self.fx.source(0,frames)
        clip.update(head_frames=6,tail_frames=6)
        self.fx.project.update(clips=[clip],sequence=[clip['key']],route_mode='loop')
        self.fx.project=t.handle('save',dict(project=self.fx.project))
        plan=self.fx.analyze()
        self.assertEqual(plan['seams'][0]['status'],'FIX',plan['seams'][0])
        result=self.fx.apply(plan)
        self.assertEqual(result['applied']['seams'],1)

    def test_registration_table_is_bound_and_source_mutation_invalidates_it(self):
        self.fx.pair(fixture.translate(self.fx.base,1))
        project=self.fx.apply(self.fx.analyze())['project']
        changed=copy.deepcopy(project)
        changed['seam_closure']['links'][0]['registration']['tail'][0]['dx']+=.1
        with self.assertRaises(ValueError):t.validate_project(changed)
        changed=copy.deepcopy(project)
        changed['seam_closure']['links'][0]['registration']['tail'][0]['scale']=float('nan')
        with self.assertRaises(ValueError):t.validate_project(changed)
        saved=t.handle('save',dict(project=project))
        loaded=t.handle('load',dict(project_id=project['id']))['project']
        self.assertEqual(saved['seam_closure'],loaded['seam_closure'])
        source=t.resolve_source(project['clips'][0]['job_id'])
        Image.new('RGBA',self.fx.base.size,(100,100,100,255)).save(source.files[-2])
        with self.assertRaises(ValueError):t.validate_project(project)

    def test_local_fallback_can_adopt_geometry_without_forcing_a_common_endpoint(self):
        base=Image.new('RGBA',(480,720));draw=ImageDraw.Draw(base)
        draw.rounded_rectangle((40,35,439,684),radius=25,fill=(180,180,180,255))
        for y in range(80,650,92):
            for x in range(78,420,74):
                draw.rectangle((x,y,x+22,y+30),outline=(20,20,20,255),width=2)
                draw.line((x+4,y+7,x+15,y+18,x+3,y+24),fill=(65,65,65,255),width=2)
        changed=np.array(base)
        # Preserve Alpha and all silhouette edges; displace only internal lines.
        changed[310:590,250:425]=np.array(base)[310:590,247:422]
        self.fx.pair(Image.fromarray(changed),base)
        plan=self.fx.analyze();row=plan['seams'][0]
        self.assertEqual(row['status'],'REVIEW',row)
        self.assertTrue(row['candidate_available'],row)
        self.assertEqual(row['candidate']['kind'],'local',row)
        self.assertTrue(any(check['code']=='local_candidate_needs_review' for check in row['failed_checks']))
        result=self.fx.apply(plan)
        self.assertEqual(row['apply_scope'],'geometry')
        self.assertEqual(result['applied']['seams'],0)
        self.assertEqual(result['applied']['geometry'],1)
        self.assertTrue(t.le.sa.active(result['project']['clips'][0]['tail']['alignment']))


if __name__ == '__main__':
    unittest.main()
