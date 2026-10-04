"""All fixtures are generated gray shapes; never reads user artwork."""
import base64
import copy
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

import transitions as t


class TransitionToneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        self.patches = [patch.object(t.e, 'ROOT', self.root), patch.object(t.e, 'JOBS', self.jobs),
                        patch.dict(t.e.PATHS, {}, clear=True), patch.dict(t.e.ACTIVE, {}, clear=True),
                        patch.dict(t.le.RUNS, {}, clear=True), patch.dict(t.RUNS, {}, clear=True)]
        for item in self.patches:
            item.start()
        self.addCleanup(self.cleanup)
        self.project = t.handle('create', dict(name='Generated tone test'))
        self.keys = ['c'*32, 'd'*32]
        self.ids = ['a'*32, 'b'*32]
        for number, jid in enumerate(self.ids):
            folder = self.jobs / jid / 'transparent_png'
            folder.mkdir(parents=True)
            frames = []
            for index in range(1, 7):
                pixels = np.zeros((80, 64, 4), dtype=np.uint8)
                pixels[:,:,:3] = 249  # Invisible white must not bias matching.
                stripe = np.linspace(30, 225, 44).round().astype(np.uint8)
                pixels[10:70, 10:54, :3] = stripe[None,:,None]
                pixels[10:70, 10:54, 3] = 255
                pixels[10:70, 9] = [180, 180, 180, 97]
                pixels[12:14,12:14,:3] = 0
                pixels[15:17,12:14,:3] = 255
                image = Image.fromarray(pixels)
                if number:
                    image = t.le.adjust_tone(image, dict(tone=25, contrast=-10))
                filename = f'frame_{index:08d}.png'
                image.save(folder / filename)
                frames.append(dict(file=filename, status='done'))
            t.e.atomic_json(folder.parent / 'job.json', dict(id=jid, name=f'Generated {number}', state='complete', frames=frames,
                dimensions=[64,80], config=dict(fps=12+12*number), paths=dict(png='transparent_png')))
            self.project['clips'].append(dict(key=self.keys[number], job_id=jid, version='original', label=f'Gray shapes {number}',
                start=1, end=6, head_frames=0, tail_frames=0))
        self.project['sequence'] = list(reversed(self.keys))
        self.project = t.handle('save', dict(project=self.project))

    def cleanup(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def matched(self):
        return t.handle('match_tone', dict(project=self.project, reference=self.keys[0]))

    def test_brightness_contrast_match_improves_and_preserves_pixels(self):
        result = self.matched()
        row = result['report']['clips'][1]
        self.assertLess(row['after']['error'], row['before']['error']*.15)
        self.assertLess(abs(row['after']['median']-result['report']['clips'][0]['after']['median']), 1)
        self.assertLess(abs(row['after']['spread']-result['report']['clips'][0]['after']['spread']), 2)
        project, sources = t.validate_project(result['project'])
        c = project['clips'][1]
        before = np.array(t._read_frame(sources[c['key']], 3))
        after = np.array(t.render_frame(c, sources[c['key']], 3, project=project)[0])
        np.testing.assert_array_equal(before[:,:,3], after[:,:,3])
        np.testing.assert_array_equal(before[:,:,:3][before[:,:,:3]==0], after[:,:,:3][before[:,:,:3]==0])
        np.testing.assert_array_equal(before[:,:,:3][before[:,:,:3]==255], after[:,:,:3][before[:,:,:3]==255])
        self.assertEqual(before.shape, after.shape)
        self.assertFalse(np.array_equal(before[:,:,:3],after[:,:,:3]))
        again = t.handle('match_tone', dict(project=project, reference=self.keys[0]))['project']
        self.assertEqual(again['tone_match']['adjustments'], project['tone_match']['adjustments'])

    def test_stale_manual_source_range_disable_and_rematch(self):
        project = self.matched()['project']
        for field, value in [('tone',5), ('contrast',3), ('end',5)]:
            stale = copy.deepcopy(project)
            stale['clips'][1][field] = value
            with self.assertRaisesRegex(ValueError, '重新統一明暗'):
                t.handle('compare', dict(project=stale, a=self.keys[0], b=self.keys[1]))
            rematched = t.handle('match_tone', dict(project=stale, reference=self.keys[0]))['project']
            t.validate_project(rematched)
            stale['tone_match']['enabled'] = False
            t.validate_project(stale)
        file = self.jobs / self.ids[1] / 'transparent_png' / 'frame_00000002.png'
        Image.new('RGBA',(64,80),(130,130,130,255)).save(file)
        with self.assertRaisesRegex(ValueError, '重新統一明暗'):
            t.validate_project(project)

    def test_shared_tone_signature_review_invalidation_and_undo(self):
        project = self.matched()['project']
        a,b = self.keys
        before = t.handle('compare',dict(project=project,a=a,b=b))
        project['reviews'][a+'>'+b] = dict(signature=before['signature'],verdict='pass')
        project['shared_tone'] = dict(tone=-12,contrast=8)
        saved = t.handle('save',dict(project=project))
        self.assertNotIn(a+'>'+b,saved['reviews'])
        self.assertNotEqual(before['signature'],t.handle('compare',dict(project=saved,a=a,b=b))['signature'])
        saved['tone_match']['enabled'] = False
        saved['shared_tone'] = dict(tone=0,contrast=0)
        clean, sources = t.validate_project(saved)
        for clip in clean['clips']:
            np.testing.assert_array_equal(t.render_frame(clip,sources[clip['key']],3,project=clean)[0],t._read_frame(sources[clip['key']],3))

    def test_sequence_permutation_and_legacy_routes(self):
        route = copy.deepcopy(self.project)
        route['sequence'] = self.keys + self.keys[:1]
        with self.assertRaisesRegex(ValueError,'每段動畫一次'):
            t.validate_project(route)
        route['sequence_version'] = 1
        self.assertEqual(t.validate_project(route)[0]['sequence'], self.keys+self.keys[:1])
        route['sequence'] = self.keys[:1]
        self.assertEqual(t.validate_project(route)[0]['sequence'],self.keys[:1])
        route.pop('sequence')
        self.assertEqual(t.validate_project(route)[0]['sequence'],self.keys)
        compared = t.handle('analyze',dict(project=self.project,scope='sequence'))
        self.assertEqual([(r['a'],r['b']) for r in compared['pairs']], [(self.keys[1],self.keys[0]),(self.keys[0],self.keys[1])])

    def test_large_tone_preview_equals_export_then_downsample(self):
        yy,xx = np.indices((800,1024))
        pixels = np.zeros((800,1024,4),dtype=np.uint8)
        pixels[:,:,:3] = np.where((xx+yy)%2,20,230)[:,:,None]
        pixels[:,:,3] = 255
        path = self.root/'large_generated.png'
        Image.fromarray(pixels).save(path)
        source = t.Source(self.ids[0],'original','Large generated',(1024,800),'24',[path],[t._stamp(path)])
        clip = copy.deepcopy(self.project['clips'][0])
        clip.update(tone=15,contrast=10)
        project = dict(shared_tone=dict(tone=30,contrast=15),tone_match=dict(enabled=True,adjustments={clip['key']:dict(tone=20,contrast=-5)}))
        expected,_ = t.render_frame(clip,source,1,project=project)
        expected.thumbnail((720,720),Image.Resampling.LANCZOS)
        preview,_ = t.render_frame(clip,source,1,(720,720),project)
        np.testing.assert_array_equal(preview,expected)

    def test_actual_export_matches_preview_and_preserves_sources_and_order(self):
        project = self.matched()['project']
        project['shared_tone'] = dict(tone=-6,contrast=3)
        project,sources = t.validate_project(project)
        before = {path:hashlib.sha256(path.read_bytes()).hexdigest() for path in self.jobs.glob('*/transparent_png/*.png')}
        pid,version = project['id'],'v001'
        output = t._version_dir(pid,version)
        output.mkdir(parents=True)
        t.RUNS[pid] = dict(state='running',version=version)
        t.export_worker(project,sources,version,t.project_signature(project,sources))
        self.assertEqual(t.RUNS[pid]['state'],'complete',t.RUNS[pid].get('phase'))
        manifest = t._read_json(output/'manifest.json')
        self.assertEqual(manifest['sequence'],list(reversed(self.keys)))
        self.assertEqual(manifest['tone_match']['adjustments'],project['tone_match']['adjustments'])
        comparison = t.compare(project,sources,*self.keys)
        for side, clip in zip(('left','right'),project['clips']):
            item = next(item for item in manifest['clips'] if item['key']==clip['key'])
            self.assertEqual(item['dimensions'],[64,80])
            self.assertLessEqual(item['alpha_max_error'],2)
            for frame in range(1,7):
                with Image.open(t.projectdir(pid)/(item['png_pattern']%frame)) as actual:
                    expected = t.render_frame(clip,sources[clip['key']],frame,project=project)[0]
                    np.testing.assert_array_equal(actual,expected)
            frame = 6 if side=='left' else 1
            with Image.open(t.projectdir(pid)/(item['png_pattern']%frame)) as actual:
                payload = Image.open(io.BytesIO(base64.b64decode(comparison[side].split(',',1)[1])))
                np.testing.assert_array_equal(actual,payload)
        for path,digest in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)

    def test_split_outside_only_export_and_scaled_preview(self):
        project = copy.deepcopy(self.project)
        project['clips'][0]['tail_frames'] = 3
        project['clips'][0]['tail']['region'] = dict(mode='split',shape='rect',x=40,y=40,w=20,h=20,feather=5,outside=dict(dx=2))
        project,sources = t.validate_project(project)
        clip = project['clips'][0]
        source = sources[clip['key']]
        unchanged = copy.deepcopy(clip)
        self.assertFalse(t._identity(clip,6,project))
        full,_ = t.render_frame(clip,source,6,project=project)
        original = t._read_frame(source,6)
        self.assertFalse(np.array_equal(full,original))
        preview,_ = t.render_frame(clip,source,6,(32,40),project)
        original.thumbnail((32,40),Image.Resampling.LANCZOS)
        def center(image):
            alpha=np.array(image)[:,:,3].astype(float)
            return float((alpha*np.arange(alpha.shape[1])[None,:]).sum()/alpha.sum())
        full_shift = center(full)-center(t._read_frame(source,6))
        self.assertAlmostEqual(center(preview)-center(original), full_shift/2, delta=.12)
        self.assertEqual(clip,unchanged)
        pid,version = project['id'],'v001'
        output = t._version_dir(pid,version)
        output.mkdir(parents=True)
        t.RUNS[pid] = dict(state='running',version=version)
        t.export_worker(project,sources,version,t.project_signature(project,sources))
        self.assertEqual(t.RUNS[pid]['state'],'complete',t.RUNS[pid].get('phase'))
        manifest=t._read_json(output/'manifest.json')
        item=next(item for item in manifest['clips'] if item['key']==clip['key'])
        with Image.open(t.projectdir(pid)/(item['png_pattern']%6)) as actual:
            np.testing.assert_array_equal(actual,full)


if __name__ == '__main__':
    unittest.main()
