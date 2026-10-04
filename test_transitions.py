import base64
import copy
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

import transitions as t


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        self.patches = [patch.object(t.e, 'ROOT', self.root), patch.object(t.e, 'JOBS', self.jobs),
                        patch.dict(t.e.PATHS, {}, clear=True), patch.dict(t.e.ACTIVE, {}, clear=True),
                        patch.dict(t.le.RUNS, {}, clear=True), patch.dict(t.RUNS, {}, clear=True)]
        for p in self.patches:
            p.start()
        self.addCleanup(self._cleanup)
        self.aid, self.bid = 'a' * 32, 'b' * 32
        self.ak, self.bk = 'c' * 32, 'd' * 32
        self._job(self.aid, '笑容.mp4', [18, 19, 20, 21, 22, 23, 24, 25], '12')
        self._job(self.bid, '驚訝.mp4', [28, 28, 27, 27, 26, 26, 25, 25], '24')
        self.project = t.handle('create', dict(name='切換測試'))
        self.project['clips'] = [dict(key=self.ak, label='笑容', job_id=self.aid, version='original', start=1, end=8, head_frames=3, tail_frames=3),
                                 dict(key=self.bk, label='驚訝', job_id=self.bid, version='original', start=1, end=8, head_frames=3, tail_frames=3)]
        self.project['sequence'] = [self.ak, self.bk, self.ak]
        self.project['sequence_version'] = 1  # Preserve an older custom route.
        self.project = t.handle('save', dict(project=self.project))

    def _cleanup(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _image(self, x, dimensions=(64, 80)):
        w, h = dimensions
        a = np.zeros((h, w, 4), dtype=np.uint8)
        a[18:62, x:x+13] = [232, 232, 232, 255]
        a[18:62, x-1] = [232, 232, 232, 96]
        a[30:42, x+3:x+9] = [35, 35, 35, 255]
        return Image.fromarray(a)

    def _job(self, jid, name, positions, fps, dimensions=(64, 80), is_test=False):
        root = self.jobs / jid
        root.mkdir()
        png = root / 'transparent_png'
        png.mkdir()
        frames = []
        for index, x in enumerate(positions, 1):
            filename = f'frame_{index:08d}.png'
            self._image(x, dimensions).save(png / filename)
            frames.append(dict(file=filename, status='done'))
        value = dict(id=jid, name=name, state='complete', is_test=is_test, frames=frames, dimensions=list(dimensions), config=dict(fps=fps), paths=dict(png='transparent_png'), created=time.time())
        t.e.atomic_json(root / 'job.json', value)
        return root

    def _rgba(self, data_url):
        self.assertTrue(data_url.startswith('data:image/png;base64,'))
        return Image.open(io.BytesIO(base64.b64decode(data_url.split(',', 1)[1]))).convert('RGBA')

    def _center(self, image):
        alpha = np.asarray(image)[:, :, 3].astype(float)
        y, x = np.indices(alpha.shape)
        return float((x * alpha).sum() / alpha.sum()), float((y * alpha).sum() / alpha.sum())

    def test_edge_lock_head_tail_and_saved_project(self):
        root = self.jobs / self.aid / 'transparent_png'
        for path in root.glob('*.png'):
            with Image.open(path) as image:
                pixels = np.array(image.convert('RGBA'))
            pixels[35:45, :, :] = [160, 170, 180, 192]
            Image.fromarray(pixels).save(path)
        project = copy.deepcopy(self.project)
        for side in ('head', 'tail'):
            project['clips'][0][side].update(sx=96, region=dict(
                mode='edge', edges='both', edge_left=12, edge_right=18))
        saved = t.handle('save', dict(project=project))
        loaded = t.handle('load', dict(project_id=saved['id']))['project']
        canonical, sources = t.validate_project(loaded)
        clip, source = canonical['clips'][0], sources[self.ak]
        self.assertEqual(clip['tail']['region']['edge_right'], 18)
        for index in range(1, 9):
            original = np.asarray(t._read_frame(source, index))
            adjusted, clipped = t.render_frame(clip, source, index)
            self.assertEqual(adjusted.size, source.dimensions)
            self.assertFalse(clipped)
            np.testing.assert_array_equal(np.asarray(adjusted)[:, [0, -1]], original[:, [0, -1]])
        changed, _ = t.render_frame(clip, source, 8)
        self.assertFalse(np.array_equal(np.asarray(changed), original))

    def test_head_tail_direction_and_fixed_canvas(self):
        project = copy.deepcopy(self.project)
        project['clips'][0]['head']['dx'] = 2
        project['clips'][0]['tail']['dx'] = -3
        canonical, sources = t.validate_project(project)
        clip, source = canonical['clips'][0], sources[self.ak]
        for index, expected in [(1, 2), (2, 1), (3, 0), (4, 0), (6, 0), (7, -1.5), (8, -3)]:
            original = t._read_frame(source, index)
            adjusted, clipped = t.render_frame(clip, source, index)
            self.assertFalse(clipped)
            self.assertEqual(adjusted.size, source.dimensions)
            self.assertAlmostEqual(self._center(adjusted)[0] - self._center(original)[0], expected, delta=.03)
        clip['head_frames'] = clip['tail_frames'] = 0
        self.assertTrue(np.array_equal(t.render_frame(clip, source, 1)[0], t._read_frame(source, 1)))
        clip['head_frames'] = clip['tail_frames'] = 1
        for index, expected in [(1, 2), (2, 0), (7, 0), (8, -3)]:
            adjusted, _ = t.render_frame(clip, source, index)
            self.assertAlmostEqual(self._center(adjusted)[0] - self._center(t._read_frame(source, index))[0], expected, delta=.03)

    def test_premultiplied_edge_tone_and_middle_unchanged(self):
        project = copy.deepcopy(self.project)
        project['clips'][0]['head']['dx'] = .5
        canonical, sources = t.validate_project(project)
        clip, source = canonical['clips'][0], sources[self.ak]
        adjusted, _ = t.render_frame(clip, source, 1)
        pixels = np.asarray(adjusted)
        edge = (pixels[:, :, 3] > 0) & (pixels[:, :, 3] < 255)
        self.assertTrue(edge.any())
        self.assertTrue(np.all(pixels[edge, :3] == 232))
        self.assertTrue(np.array_equal(t.render_frame(clip, source, 4)[0], t._read_frame(source, 4)))
        clip['tone'] = 30
        before = np.asarray(t._read_frame(source, 4))
        after = np.asarray(t.render_frame(clip, source, 4)[0])
        self.assertTrue(np.array_equal(before[:, :, 3], after[:, :, 3]))
        self.assertFalse(np.array_equal(before[:, :, :3], after[:, :, :3]))

    def test_independent_head_tail_regions_and_review_signatures(self):
        project=copy.deepcopy(self.project)
        self.assertNotIn('region',project['clips'][0]['head'])
        head=project['clips'][0]['head'];tail=project['clips'][0]['tail']
        head.update(dx=2,region=dict(mode='inside',shape='rect',x=20,y=20,w=50,h=55,feather=10))
        tail.update(dx=-2,region=dict(mode='outside',shape='ellipse',x=20,y=20,w=50,h=55,feather=0))
        canonical,sources=t.validate_project(project);clip=canonical['clips'][0];source=sources[self.ak]
        yy,xx=np.mgrid[:80,:64].astype(np.float32)
        for index,side in [(1,'head'),(8,'tail')]:
            original=np.asarray(t._read_frame(source,index));result,clipped=t.render_frame(clip,source,index)
            influence=t.le.region_influence(clip[side]['region'],xx,yy,64,80)
            self.assertFalse(clipped)
            self.assertTrue(np.array_equal(np.asarray(result)[influence==0],original[influence==0]))
            self.assertFalse(np.array_equal(result,original))
        self.assertTrue(np.array_equal(t.render_frame(clip,source,4)[0],t._read_frame(source,4)))
        compared=t.handle('compare',dict(project=canonical,a=self.ak,b=self.bk))
        self.assertTrue(np.array_equal(self._rgba(compared['left']),t.render_frame(clip,source,8)[0]))
        canonical['reviews'][self.ak+'>'+self.bk]=dict(signature=compared['signature'],verdict='pass',note='region check')
        saved=t.handle('save',dict(project=canonical))
        self.assertIn(self.ak+'>'+self.bk,saved['reviews'])
        saved['clips'][0]['tail']['region']['feather']=20
        revised=t.handle('save',dict(project=saved))
        self.assertNotIn(self.ak+'>'+self.bk,revised['reviews'])
        self.assertNotEqual(t.clip_signature(saved['clips'][0],source),t.clip_signature(canonical['clips'][0],source))
        revised['clips'][0]['head']['region']['w']=100
        with self.assertRaises(ValueError):t.handle('save',dict(project=revised))

    def test_directed_matrix_compare_and_mixed_fps_preview(self):
        ab = t.handle('compare', dict(project=self.project, a=self.ak, b=self.bk))
        ba = t.handle('compare', dict(project=self.project, a=self.bk, b=self.ak))
        self.assertNotEqual(ab['signature'], ba['signature'])
        self.assertNotEqual(ab['metrics'], ba['metrics'])
        self.assertEqual(self._rgba(ab['left']).size, (64, 80))
        result = t.handle('analyze', dict(project=self.project))
        self.assertEqual({(p['a'], p['b']) for p in result['pairs']}, {(self.ak, self.ak), (self.ak, self.bk), (self.bk, self.ak), (self.bk, self.bk)})
        preview = t.handle('render_preview', dict(project=self.project, a=self.ak, b=self.bk, span=3))
        self.assertEqual([f['frame'] for f in preview['frames']], [6, 7, 8, 1, 2, 3])
        self.assertAlmostEqual(preview['frames'][0]['duration_ms'], 1000 / 12)
        self.assertAlmostEqual(preview['frames'][-1]['duration_ms'], 1000 / 24)
        self.assertLessEqual(max(preview['dimensions']), 720)
        self.assertTrue(preview['preview'])

    def test_review_signatures_drop_after_edit_or_source_change(self):
        comparison = t.handle('compare', dict(project=self.project, a=self.ak, b=self.bk))
        key = self.ak + '>' + self.bk
        self.project['reviews'][key] = dict(signature=comparison['signature'], verdict='pass', note='人工確認')
        self.project = t.handle('save', dict(project=self.project))
        self.assertIn(key, self.project['reviews'])
        changed = copy.deepcopy(self.project)
        changed['clips'][0]['tail']['dx'] = 1
        self.assertNotIn(key, t.handle('save', dict(project=changed))['reviews'])
        self.project = t.handle('save', dict(project=self.project))
        source = self.jobs / self.aid / 'transparent_png/frame_00000008.png'
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
        loaded = t.handle('load', dict(project_id=self.project['id']))
        self.assertNotIn(key, loaded['project']['reviews'])

    def test_catalog_versions_and_path_escape_validation(self):
        testid = 'e' * 32
        self._job(testid, 'fixture', [20, 20], '12', is_test=True)
        version = self.jobs / self.aid / 'loop_edits/修整_001'
        png = version / 'transparent_png'
        png.mkdir(parents=True)
        for i in range(1, 4):
            self._image(30).save(png / f'frame_{i:08d}.png')
        t.e.atomic_json(version / 'status.json', dict(state='complete'))
        t.e.atomic_json(version / 'verification.json', dict(frames=3, dimensions=[64, 80], fps='8'))
        available = t.handle('catalog', {})['sources']
        self.assertEqual(len(available), 3)
        self.assertFalse(any(v['job_id'] == testid for v in available))
        resolved = t.resolve_source(self.aid, '修整_001')
        self.assertEqual(len(resolved.files), 3)
        self.assertEqual(resolved.fps, '8')
        for bad in ['../escape', '修整_001/../../escape', 'C:\\escape', '..\\x']:
            with self.assertRaises(ValueError):
                t.resolve_source(self.aid, bad)
        with self.assertRaises(ValueError):
            t.projectdir('../escape')
        self.assertTrue(t.projectdir(self.project['id']).is_relative_to(self.root / 'transition_projects'))
        job = t.e.read(self.aid)
        job['paths']['png'] = '../outside'
        t.e.atomic_json(self.jobs / self.aid / 'job.json', job)
        with self.assertRaises(ValueError):
            t.resolve_source(self.aid)

    def test_ranges_dimensions_numbers_and_clip_keys(self):
        for field, value in [('head_frames', 7), ('tone', float('nan')), ('start', 1.5), ('end', 1)]:
            project = copy.deepcopy(self.project)
            project['clips'][0][field] = value
            with self.assertRaises(ValueError):
                t.handle('save', dict(project=project))
        project = copy.deepcopy(self.project)
        project['clips'][0]['tail']['sx'] = 0
        with self.assertRaises(ValueError):
            t.handle('save', dict(project=project))
        project = copy.deepcopy(self.project)
        project['clips'][1]['key'] = self.ak
        with self.assertRaises(ValueError):
            t.handle('save', dict(project=project))
        job = t.e.read(self.bid)
        job['dimensions'] = [64, 82]
        t.e.atomic_json(self.jobs / self.bid / 'job.json', job)
        with self.assertRaisesRegex(ValueError, '相同畫布'):
            t.handle('save', dict(project=self.project))

    def test_clipping_detected_and_original_bytes_unchanged(self):
        project = copy.deepcopy(self.project)
        project['clips'][0]['tail']['dx'] = 90
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.jobs.glob('*/transparent_png/*.png')}
        compared = t.handle('compare', dict(project=project, a=self.ak, b=self.bk))
        self.assertTrue(compared['clipped'])
        self.assertTrue(compared['clipped_frames'])
        for path, digest in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_real_ffmpeg_export_snapshot_alpha_and_manifest(self):
        project = copy.deepcopy(self.project)
        for clip in project['clips']:
            clip.update(start=1, end=4, head_frames=2, tail_frames=2)
        project['clips'][0]['head']['dx'] = .5
        project['clips'][0]['tail']['dx'] = -.5
        project['clips'][0]['head']['region']=dict(mode='inside',shape='rect',x=20,y=20,w=50,h=55,feather=10)
        project['clips'][0]['tail']['region']=dict(mode='outside',shape='ellipse',x=20,y=20,w=50,h=55,feather=10)
        before = {p: p.read_bytes() for p in self.jobs.glob('*/transparent_png/*.png')}
        original_thread = t.threading.Thread
        threads = []
        def track(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            threads.append(thread)
            return thread
        with patch.object(t.threading, 'Thread', side_effect=track):
            state = t.handle('export', dict(project=project))
        self.assertEqual(state['state'], 'running')
        project['clips'][0]['tone'] = 35
        t.handle('save', dict(project=project))
        self.assertEqual(len(threads), 1)
        threads[0].join(90)
        self.assertFalse(threads[0].is_alive(), 'FFmpeg did not finish')
        state = t.handle('status', dict(project_id=project['id']))
        self.assertEqual(state['state'], 'complete', state.get('phase'))
        version = state['version']
        root = t.projectdir(project['id'])
        snapshot = t._read_json(root / 'versions' / version / 'project_snapshot.json')
        self.assertEqual(snapshot['clips'][0]['tone'], 0)
        canonical,sources=t.validate_project(snapshot)
        manifest = t._read_json(root / 'versions' / version / 'manifest.json')
        self.assertEqual(len(manifest['clips']), 2)
        self.assertEqual(manifest['sequence'], [self.ak, self.bk, self.ak])
        for item in manifest['clips']:
            self.assertEqual(item['dimensions'], [64, 80])
            self.assertEqual(item['frames'], 4)
            self.assertLessEqual(item['alpha_max_error'], 2)
            self.assertTrue((root / item['video_path']).is_file())
            self.assertTrue((root / (item['png_pattern'] % 1)).is_file())
            clip=next(c for c in canonical['clips'] if c['key']==item['key'])
            for frame in range(1,item['frames']+1):
                with Image.open(root/(item['png_pattern']%frame)) as written:
                    expected,_=t.render_frame(clip,sources[clip['key']],frame)
                    self.assertTrue(np.array_equal(written,expected))
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        for index in range(1, 5):
            original = self.jobs / self.bid / 'transparent_png' / f'frame_{index:08d}.png'
            item = next(v for v in manifest['clips'] if v['key'] == self.bk)
            copied = root / (item['png_pattern'] % index)
            self.assertEqual(original.read_bytes(), copied.read_bytes())
        loaded = t.handle('load', dict(project_id=project['id']))
        self.assertEqual(loaded['versions'][0]['clips'], manifest['clips'])
        self.assertEqual(loaded['versions'][0]['signature'], manifest['signature'])


if __name__ == '__main__':
    unittest.main()
