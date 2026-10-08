"""Read-only candidate playback contract; neutral generated diagrams only."""
import copy
import time
import unittest
from unittest.mock import patch
import finish_plan as f
import transitions as t
import test_finish_registration_review as fixture


class CandidatePlaybackTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.FinishRegistrationIndependentTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.pair(self.fx.base, self.fx.base)
        self.project, self.sources = t.validate_project(self.fx.project)
        self.before = copy.deepcopy(self.project)
        self.after = copy.deepcopy(self.project)
        self.after['clips'][0]['tail']['dx'] = 2
        self.a, self.b = [c['key'] for c in self.project['clips']]
        self.signature = t.project_signature(self.project, self.sources)
        self.entry = dict(created=time.monotonic(), public=dict(signature=self.signature),
                          candidates={self.a+'>'+self.b:dict(before=self.before,after=self.after,candidate=dict(kind='similarity',apply_scope='none'))})
        self.cache = patch.dict(f._PLANS, {'playback-fixture':self.entry}, clear=True)
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def play(self, **changes):
        args=dict(project=self.project,plan_id='playback-fixture',signature=self.signature,
                  a=self.a,b=self.b,view='after',span=4)
        args.update(changes)
        return t.handle('finish_candidate_playback', args)

    def test_before_after_frames_are_real_ephemeral_renders_and_do_not_apply(self):
        saved=copy.deepcopy(self.project);hashes=self.fx.hashes();entry=copy.deepcopy(self.entry)
        before=self.play(view='before');after=self.play()
        for view, result in [('before',before),('after',after)]:
            expected=t.render_preview(getattr(self,view),self.sources,self.a,self.b,4)
            self.assertEqual(result['frames'],expected['frames'])
            self.assertEqual(result['candidate_view'],view)
            self.assertFalse(result['applied'])
            self.assertEqual(len(result['frames']),8)
        self.assertNotEqual(before['frames'][3]['image'],after['frames'][3]['image'])
        self.assertEqual(saved,self.project);self.assertEqual(entry,self.entry)
        self.assertEqual(hashes,self.fx.hashes())

    def test_unknown_view_pair_and_expired_plan_reject_without_fallback(self):
        for values in [dict(view='draft'),dict(a='unknown'),dict(plan_id='missing')]:
            with self.assertRaises(ValueError):self.play(**values)
        self.entry['created']-=f.TTL+1
        with self.assertRaisesRegex(ValueError,'過期'):self.play()

    def test_changed_draft_rejects(self):
        altered=copy.deepcopy(self.project);altered['clips'][0]['tone']=1
        with self.assertRaisesRegex(ValueError,'變更'):self.play(project=altered)

    def test_source_change_during_render_rejects(self):
        original=t.render_preview
        def mutate(*args,**kwargs):
            result=original(*args,**kwargs)
            path=self.sources[self.a].files[0]
            path.write_bytes(path.read_bytes()+b'changed')
            return result
        with patch.object(t,'render_preview',side_effect=mutate):
            with self.assertRaisesRegex(ValueError,'來源已變更'):self.play()

if __name__=='__main__':unittest.main()
