import copy
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
import transitions as t
import seam_repair as r
from test_finish_registration_review import FinishRegistrationIndependentTests, translate

class RepairTests(unittest.TestCase):
    def setUp(self):
        self.fx=FinishRegistrationIndependentTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.fx.pair(self.fx.base,translate(self.fx.base,3))
        self.p,self.sources=t.validate_project(self.fx.project)

    def build(self,p=None):
        p=p or self.p
        # Test orchestration using the real classic reference backend, no GPU required.
        return r.build(p,self.sources,backend='classic-test')

    def test_pair_saved_renderer_preview_and_export_identity(self):
        hashes=self.fx.hashes();before=copy.deepcopy(self.p)
        result=self.build();p=result['project'];ca,cb=p['clips']
        t.validate_project(p)
        left,_=t.render_frame(ca,self.sources[ca['key']],ca['end'],project=p)
        right,_=t.render_frame(cb,self.sources[cb['key']],cb['start'],project=p)
        self.assertEqual(left.tobytes(),right.tobytes())
        self.assertFalse(t._identity(ca,ca['end'],p))
        self.assertEqual(t.compare(p,self.sources,ca['key'],cb['key'])['dimensions'],list(left.size))
        self.assertEqual(len(t.render_preview(p,self.sources,ca['key'],cb['key'],6)['frames']),12)
        self.assertEqual(hashes,self.fx.hashes());self.assertEqual(before,self.p)
        self.assertTrue(p['seam_repair']['pairs']);self.assertNotIn('seam_closure',p)
        self.assertEqual(t.handle('save',dict(project=p))['seam_repair'],p['seam_repair'])

    def test_self_loop_and_middle_untouched(self):
        p=copy.deepcopy(self.p);p['clips']=p['clips'][:1];p['sequence']=p['sequence'][:1];p['route_mode']='loop'
        p=self.build(p)['project'];c=p['clips'][0];s=self.sources[c['key']]
        self.assertEqual(t.render_frame(c,s,c['start'],project=p)[0].tobytes(),t.render_frame(c,s,c['end'],project=p)[0].tobytes())
        self.assertEqual(t.render_frame(c,s,6,project=p)[0].tobytes(),t._read_frame(s,6).tobytes())

    def test_setting_change_rejects_stale_output_but_can_repair_again(self):
        p=self.build()['project'];p['clips'][0]['tone']=5
        with self.assertRaisesRegex(ValueError,'重新修復'):t.validate_project(p)
        q=self.build(p)['project'];t.validate_project(q)
        self.assertNotEqual(p['seam_repair']['id'],q['seam_repair']['id'])

    def test_missing_or_corrupt_cache_never_silently_plays_original(self):
        p=self.build()['project'];key=p['clips'][0]['key'];entry=p['seam_repair']['frames'][key][str(p['clips'][0]['end'])]
        path=r.frame_path(p,entry);path.write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError,'修復影格'):t.render_frame(p['clips'][0],self.sources[key],p['clips'][0]['end'],project=p)

    def test_short_clip_rejected_without_partial_project_save(self):
        p=copy.deepcopy(self.p);p['clips'][0]['end']=2;p['clips'][0]['head_frames']=1;p['clips'][0]['tail_frames']=1
        with self.assertRaisesRegex(ValueError,'至少'):self.build(p)

    def test_closed_three_clip_route_has_unique_matching_seams(self):
        c=self.fx.source(2,[translate(self.fx.base,-2)]*12)
        p=copy.deepcopy(self.p);p['clips'].append(c);p['sequence'].append(c['key']);p['route_mode']='loop'
        p,s=t.validate_project(p);q=r.build(p,s,backend='classic-test')['project']
        by={c['key']:c for c in q['clips']}
        for a,b in t.ts.pairs(q):
            self.assertEqual(t.render_frame(by[a],s[a],by[a]['end'],project=q)[0].tobytes(),t.render_frame(by[b],s[b],by[b]['start'],project=q)[0].tobytes())

    def test_whole_clip_scale_cache_and_preview_export_binding(self):
        from test_scale_stability import robot,zoom
        frames=[Image.fromarray(zoom(robot(),1+.02*i/23)) for i in range(24)]
        clip=self.fx.source(3,frames)
        p=copy.deepcopy(self.p);p['clips']=[clip];p['sequence']=[clip['key']];p['route_mode']='loop'
        p,s=t.validate_project(p);q=r.build(p,s,backend='classic-test')['project']
        self.assertEqual(q['seam_repair']['stability']['clips'][clip['key']]['status'],'applied')
        self.assertEqual(len(q['seam_repair']['frames'][clip['key']]),24)
        c=q['clips'][0];mid,_=t.render_frame(c,s[c['key']],12,project=q)
        self.assertNotEqual(mid.tobytes(),frames[11].tobytes())
        self.assertFalse(t._identity(c,12,q));t.validate_project(q)
        self.assertEqual(t.render_frame(c,s[c['key']],1,project=q)[0].tobytes(),t.render_frame(c,s[c['key']],24,project=q)[0].tobytes())

    def test_changed_source_invalidates_saved_repair(self):
        p=self.build()['project'];path=self.sources[p['clips'][0]['key']].files[0]
        Image.new('RGBA',(160,240),(100,100,100,255)).save(path)
        with self.assertRaisesRegex(ValueError,'重新修復'):t.validate_project(p)

    def test_model_failure_leaves_saved_project_untouched(self):
        before=(t.projectdir(self.p['id'])/'project.json').read_bytes()
        with patch.object(r,'runtime',side_effect=ValueError('missing model')):
            with self.assertRaisesRegex(ValueError,'missing model'):r.build(self.p,self.sources)
        self.assertEqual(before,(t.projectdir(self.p['id'])/'project.json').read_bytes())

    def test_edge_recovery_preserves_observed_coverage_not_background(self):
        from seam_repair_core import protect_edges
        a=np.zeros((48,32,4),dtype=np.uint8);a[12:36,:12]=[200,180,160,255]
        damaged=a.copy();damaged[:,:3]=0
        out=protect_edges(damaged,a,a,.5)
        self.assertTrue(np.all(out[12:36,0,3]==255))
        self.assertTrue(np.all(out[:10,:,3]==0))
        self.assertEqual(out.shape,a.shape)

if __name__=='__main__':unittest.main()
