"""Public workflow acceptance for seam alignment; synthetic temporary assets only."""
import base64
import copy
import importlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

import engine as e
import loop_editor as le
import transitions as tr


class AlignmentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.sa = importlib.import_module('seam_alignment')
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        self.patches = [patch.object(e, 'ROOT', self.root), patch.object(e, 'JOBS', self.jobs),
                        patch.dict(e.PATHS, {}, clear=True), patch.dict(e.ACTIVE, {}, clear=True),
                        patch.dict(le.RUNS, {}, clear=True), patch.dict(tr.RUNS, {}, clear=True),
                        patch.dict(tr._FEATURES, {}, clear=True)]
        for item in self.patches:
            item.start()
        self.addCleanup(self._cleanup)
        self.sa.clear_cache()
        self.aid, self.bid = 'a' * 32, 'b' * 32
        self.ak, self.bk = 'c' * 32, 'd' * 32
        self.a = self._job(self.aid, [0, 2, 4, 6])
        self.b = self._job(self.bid, [0, -1, -2, -3])
        self.project = tr.handle('create', dict(name='Synthetic alignment acceptance'))
        self.project['clips'] = [dict(key=self.ak, job_id=self.aid, version='original', label='Pattern A',
                                     start=1, end=4, head_frames=2, tail_frames=2),
                                 dict(key=self.bk, job_id=self.bid, version='original', label='Pattern B',
                                     start=1, end=4, head_frames=2, tail_frames=2)]
        self.project['sequence'] = [self.ak, self.bk]
        self.project = tr.handle('save', dict(project=self.project))

    def _cleanup(self):
        self.sa.clear_cache()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _pattern(self, bend):
        # Independently drawn geometric tiles; no user footage or algorithm mocks.
        width, height = 256, 768
        image = Image.new('RGBA', (width, height), (255, 0, 255, 0))
        draw = ImageDraw.Draw(image)
        def y(value):
            return round(value + bend * np.cos(value / height * 2 * np.pi))
        draw.rounded_rectangle((24, y(24), 232, y(744)), radius=14, fill=(185, 185, 185, 96))
        draw.rounded_rectangle((27, y(27), 229, y(741)), radius=12, fill=(205, 205, 205, 255))
        for row, center_y in enumerate(range(67, 725, 60)):
            for col, center_x in enumerate((55, 104, 153, 202)):
                cy = y(center_y)
                shade = 35 + ((row * 37 + col * 53) % 110)
                draw.rectangle((center_x-13, cy-16, center_x+13, cy+16), outline=(shade, shade, shade, 255), width=3)
                draw.line((center_x-8, cy-8, center_x+7, cy+9), fill=(25, 25, 25, 255), width=2)
                draw.rectangle((center_x-7, cy+5, center_x-3, cy+10), fill=(90+col*20,)*3+(255,))
        return image

    def _job(self, jid, bends):
        folder = self.jobs / jid
        png = folder / 'transparent_png'
        png.mkdir(parents=True)
        frames = []
        for i, bend in enumerate(bends, 1):
            filename = f'frame_{i:08d}.png'
            self._pattern(bend).save(png / filename)
            frames.append(dict(file=filename, status='done'))
        job = dict(id=jid, name='Synthetic pattern', state='complete', frames=frames,
                   dimensions=[256, 768], config=dict(fps='24'), paths=dict(png='transparent_png'), created=1)
        e.atomic_json(folder / 'job.json', job)
        return job

    def _path(self, jid, frame):
        return self.jobs / jid / 'transparent_png' / f'frame_{frame:08d}.png'

    def _read(self, path):
        with Image.open(path) as image:
            return image.convert('RGBA')

    def _decode(self, value):
        self.assertTrue(value.startswith('data:image/png;base64,'))
        with Image.open(io.BytesIO(base64.b64decode(value.split(',', 1)[1]))) as image:
            return image.convert('RGBA')

    def _distance(self, a, b):
        def associated(image):
            pixels = np.asarray(image).astype(float) / 255
            return np.concatenate((pixels[:, :, :3] * pixels[:, :, 3:4], pixels[:, :, 3:4]), axis=2)
        return float(np.mean(np.abs(associated(a) - associated(b))))

    def _loop_alignment(self):
        recipe = le.recipe(self.a, dict(start=1, end=4, ramp=2))
        result = le.handle('align', self.a, dict(recipe=recipe, reference_frame=1, source_frame=4))
        self.assertIn('report', result)
        recipe['alignment'] = result['alignment']
        return recipe

    def _loop_preview(self, recipe, frame=4):
        return self._decode(le.handle('preview', self.a, dict(recipe=recipe, frame=frame))['image'])

    def _transition_alignment(self, side):
        project = copy.deepcopy(self.project)
        result = tr.handle('align', dict(project=project, a=self.ak, b=self.bk, side=side))
        index = 0 if side == 'tail' else 1
        project['clips'][index][side]['alignment'] = result['alignment']
        return tr.handle('save', dict(project=project))

    def _compare(self, project):
        return tr.handle('compare', dict(project=project, a=self.ak, b=self.bk))

    def _mutate_source(self, jid, frame):
        path = self._path(jid, frame)
        stamp = path.stat()
        image = self._read(path)
        ImageDraw.Draw(image).rectangle((100, 300, 108, 308), fill=(0, 0, 0, 255))
        image.save(path)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 10000000))

    def test_loop_saved_model_rebuild_priority_and_tone(self):
        recipe = self._loop_alignment()
        endpoint = self._loop_preview(recipe)
        source = self._read(self._path(self.aid, 4))
        target = self._read(self._path(self.aid, 1))
        self.assertLess(self._distance(endpoint, target), self._distance(source, target) * .75)
        np.testing.assert_array_equal(self._loop_preview(recipe, 1), target)
        le.handle('save', self.a, dict(recipe=json.loads(json.dumps(recipe))))
        self.sa.clear_cache()
        loaded = le.handle('load', self.a, {})['recipe']
        self.assertEqual(loaded['alignment'], recipe['alignment'])
        np.testing.assert_array_equal(self._loop_preview(loaded), endpoint)
        overridden = dict(loaded, dx=40, sy=112, angle=5, protect=True, radius=60,
                          region=dict(mode='split', x=20, y=20, w=60, h=60, outside=dict(dx=-40)))
        np.testing.assert_array_equal(self._loop_preview(overridden), endpoint)
        toned = self._loop_preview(dict(overridden, tone=35, contrast=10))
        np.testing.assert_array_equal(np.asarray(toned)[:, :, 3], np.asarray(endpoint)[:, :, 3])
        opaque = np.asarray(endpoint)[:, :, 3] > 240
        self.assertGreater(np.abs(np.asarray(toned).astype(float)[:, :, :3][opaque] -
                                  np.asarray(endpoint).astype(float)[:, :, :3][opaque]).mean(), 2)
        disabled = copy.deepcopy(loaded)
        disabled['alignment']['enabled'] = False
        np.testing.assert_array_equal(self._loop_preview(disabled), source)

    def test_loop_rejects_stale_end_source_and_reference(self):
        recipe = self._loop_alignment()
        with self.assertRaises(ValueError):
            self._loop_preview(dict(recipe, end=3), frame=3)
        with self.assertRaises(ValueError):
            le.handle('align', self.a, dict(recipe=recipe, reference_frame=1, source_frame=3))
        self._mutate_source(self.aid, 4)
        self.sa.clear_cache()
        with self.assertRaises(ValueError):
            self._loop_preview(recipe)
        fresh = self._loop_alignment()
        self._mutate_source(self.aid, 1)
        self.sa.clear_cache()
        with self.assertRaises(ValueError):
            self._loop_preview(fresh)

    def test_transition_head_tail_saved_preview_and_real_export_agree(self):
        originals = {path: path.read_bytes() for path in self.jobs.glob('*/transparent_png/*.png')}
        for side in ('tail', 'head'):
            with self.subTest(side=side):
                project = self._transition_alignment(side)
                before = self._compare(self.project)
                expected_compare = self._compare(project)
                self.sa.clear_cache()
                tr._FEATURES.clear()
                loaded = tr.handle('load', dict(project_id=project['id']))
                self.assertFalse(loaded['source_error'])
                project = loaded['project']
                self.assertEqual(self._compare(project)['left'], expected_compare['left'])
                self.assertEqual(self._compare(project)['right'], expected_compare['right'])
                field, clip_index, frame = ('left', 0, 4) if side == 'tail' else ('right', 1, 1)
                full = self._decode(expected_compare[field])
                self.assertEqual(full.size, (256, 768))
                self.assertNotEqual(expected_compare[field], before[field])
                preview = tr.handle('render_preview', dict(project=project, a=self.ak, b=self.bk, span=2))
                key = project['clips'][clip_index]['key']
                actual = next(item for item in preview['frames'] if item['clip_key'] == key and item['frame'] == frame)
                thumbnail = full.copy()
                thumbnail.thumbnail((720, 720), Image.Resampling.LANCZOS)
                self.assertLess(max(thumbnail.size), max(full.size))
                np.testing.assert_array_equal(self._decode(actual['image']), thumbnail)
                original_thread = tr.threading.Thread
                threads = []
                def track(*args, **kwargs):
                    thread = original_thread(*args, **kwargs)
                    threads.append(thread)
                    return thread
                with patch.object(tr.threading, 'Thread', side_effect=track):
                    state = tr.handle('export', dict(project=project))
                self.assertEqual(state['state'], 'running')
                self.assertEqual(len(threads), 1)
                threads[0].join(45)
                self.assertFalse(threads[0].is_alive(), 'Synthetic export did not finish within 45 s')
                state = tr.handle('status', dict(project_id=project['id']))
                self.assertEqual(state['state'], 'complete', state.get('phase'))
                item = next(item for item in state['clips'] if item['key'] == key)
                exported = self._read(tr.projectdir(project['id']) / (item['png_pattern'] % frame))
                np.testing.assert_array_equal(exported, full)
                jid = self.aid if side == 'tail' else self.bid
                self.assertFalse(np.array_equal(exported, self._read(self._path(jid, frame))),
                                 'Identity copy skipped an enabled alignment during export')
                self.assertLessEqual(item['alpha_max_error'], 2)
        for path, value in originals.items():
            self.assertEqual(path.read_bytes(), value)

    def test_transition_reference_snapshot_persists_but_endpoint_and_pixels_invalidate(self):
        for side in ('tail', 'head'):
            with self.subTest(side=side):
                project = self._transition_alignment(side)
                reference_index = 1 if side == 'tail' else 0
                changed = copy.deepcopy(project)
                reference = changed['clips'][reference_index]
                if side == 'tail':
                    reference.update(start=2, head_frames=1)
                else:
                    reference.update(end=3, tail_frames=1)
                with self.assertRaises(ValueError):
                    self._compare(changed)
                changed = copy.deepcopy(project)
                changed['clips'][reference_index]['tone'] = 30
                field = 'left' if side == 'tail' else 'right'
                # The fitted reference appearance is a persistent snapshot. A later
                # reference tone edit must not rewrite the existing displacement.
                self.assertEqual(self._compare(changed)[field], self._compare(project)[field])
                jid, frame = (self.bid, 1) if side == 'tail' else (self.aid, 4)
                self._mutate_source(jid, frame)
                self.sa.clear_cache()
                with self.assertRaises(ValueError):
                    self._compare(project)


if __name__ == '__main__':
    unittest.main()
