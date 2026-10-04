"""Meaningful closure acceptance: synthetic art only, real PNG/WebM export."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

import engine as e
import loop_closure as lc
import loop_editor as le


class LoopClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        self.jid = 'a' * 32
        self.folder = self.jobs / self.jid
        self.png = self.folder / 'transparent_png'
        self.png.mkdir(parents=True)
        self.patches = [patch.object(e, 'ROOT', self.root), patch.object(e, 'JOBS', self.jobs),
                        patch.dict(e.PATHS, {}, clear=True), patch.dict(e.ACTIVE, {}, clear=True),
                        patch.dict(le.RUNS, {}, clear=True)]
        for item in self.patches:
            item.start()
        self.addCleanup(self._cleanup)
        frames = []
        for index in range(1, 10):
            name = f'frame_{index:08d}.png'
            self.pattern((index - 1) * 1.5).save(self.png / name)
            frames.append(dict(file=name, status='done'))
        self.j = dict(id=self.jid, name='Synthetic closure', state='complete', frames=frames,
                      dimensions=[160, 160], config=dict(fps='24'), paths=dict(png='transparent_png'))
        e.atomic_json(self.folder / 'job.json', self.j)
        self.r = le.recipe(self.j, dict(start=1, end=9, ramp=6,
                         closure=dict(enabled=True, reference=4, head_frames=4, tail_frames=4)))

    def _cleanup(self):
        lc.clear_cache()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def pattern(dx=0):
        image = Image.new('RGBA', (160, 160), (255, 0, 255, 0))
        draw = ImageDraw.Draw(image)
        dx = round(dx)
        draw.rounded_rectangle((30 + dx, 20, 112 + dx, 140), radius=12, fill=(210, 210, 210, 255))
        draw.rectangle((35 + dx, 15, 107 + dx, 19), fill=(240, 240, 240, 96))
        for y in range(35, 135, 20):
            for x in range(42, 103, 20):
                draw.rectangle((x + dx, y, x + dx + 8, y + 10), fill=(35, 35, 35, 255))
                draw.line((x + dx, y, x + dx + 8, y + 10), fill=(140, 140, 140, 255), width=2)
        return image

    def preview(self, recipe, frame, original=False):
        result = le.handle('preview', self.j, dict(recipe=recipe, frame=frame, original=original))
        with Image.open(io.BytesIO(base64.b64decode(result['image'].split(',', 1)[1]))) as im:
            return im.convert('RGBA'), result

    def hashes(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.png.glob('*.png')}

    @staticmethod
    def distance(a, b):
        return float(np.mean(np.abs(lc._associated(a) - lc._associated(b))))

    def test_validation_rejects_overlap_noninteger_and_bad_reference(self):
        invalid = [None, [], dict(enabled='yes'), dict(reference=True), dict(reference=3.5),
                   dict(reference='4'), dict(reference=0), dict(reference=10), dict(head_frames=True),
                   dict(head_frames=2.5), dict(tail_frames=float('nan')), dict(tail_frames=float('inf')),
                   dict(head_frames=1), dict(head_frames=6, tail_frames=4)]
        for change in invalid:
            value = change if not isinstance(change, dict) else dict(self.r['closure'], **change)
            with self.subTest(value=value), self.assertRaises(ValueError):
                le.recipe(self.j, dict(closure=value))
        with self.assertRaises(ValueError):
            le.recipe(self.j, dict(start=2, end=4, ramp=3, closure=dict(enabled=True)))
        short = le.recipe(self.j, dict(start=2, end=5, ramp=3, closure=dict(enabled=True)))
        self.assertEqual(short['closure']['reference'], 2)
        self.assertEqual(short['closure']['head_frames'], 2)

    def test_disabled_is_backwards_compatible_and_windows_leave_middle_unchanged(self):
        before = self.hashes()
        legacy = le.recipe(self.j, dict(start=1, end=9, ramp=5, dx=2.5, tone=15))
        disabled = dict(legacy, closure=dict(self.r['closure'], enabled=False))
        active = dict(legacy, closure=self.r['closure'])
        for index in range(1, 10):
            expected, _ = le.render(le.source(self.j, index), legacy, index)
            actual, _ = self.preview(disabled, index)
            np.testing.assert_array_equal(actual, expected)
            if lc.weight(active, index) == 0:
                np.testing.assert_array_equal(self.preview(active, index)[0], expected)
        self.assertEqual([lc.weight(active, index) for index in (1, 4, 5, 6, 9)], [1, 0, 0, 0, 1])
        self.assertEqual(self.hashes(), before)
        # Inactive saved settings do not block later trimming of the old recipe.
        trimmed = le.recipe(self.j, dict(start=5, end=9, ramp=7,
                            closure=dict(enabled=False, reference=1, head_frames=1, tail_frames=99)))
        np.testing.assert_array_equal(self.preview(trimmed, 5)[0], le.source(self.j, 5))

    def test_endpoint_bypasses_irrelevant_warp_and_uses_same_toned_raw_reference(self):
        recipe = dict(self.r, tone=35, contrast=12, dx=100, sx=120, angle=10,
                      region=dict(mode='edge', edges='both', edge_left=1, edge_right=1))
        expected = le.adjust_tone(le.source(self.j, 4), recipe)
        for index in (1, 9):
            image, meta = self.preview(recipe, index)
            np.testing.assert_array_equal(image, expected)
            self.assertFalse(meta['clipped'])
            self.assertEqual(meta['closure'], dict(enabled=True, reference=4, weight=1))
        original, meta = self.preview(recipe, 9, original=True)
        np.testing.assert_array_equal(original, le.source(self.j, 9))
        self.assertEqual(meta['closure']['weight'], 0)

    def test_registered_translation_beats_unregistered_crossfade(self):
        a, b, midpoint = self.pattern(0), self.pattern(12), self.pattern(6)
        actual = lc.morph(a, b, .5)
        naive = (lc._associated(a) + lc._associated(b)) / 2
        actual_error = self.distance(actual, midpoint)
        naive_error = float(np.mean(np.abs(naive - lc._associated(midpoint))))
        self.assertLess(actual_error, naive_error * .65, (actual_error, naive_error))
        # Semi-transparent edges stay grayscale despite magenta invisible RGB.
        pixels = np.asarray(actual)
        visible = pixels[:, :, 3] > 0
        self.assertTrue(np.all(pixels[:, :, 0][visible] == pixels[:, :, 1][visible]))
        self.assertTrue(np.all(pixels[:, :, 1][visible] == pixels[:, :, 2][visible]))
        np.testing.assert_array_equal(lc.morph(a, b, 0), a)
        np.testing.assert_array_equal(lc.morph(a, b, 1), b)
        self.assertEqual(actual.size, a.size)

    def test_cache_uses_actual_pixels_and_has_bounded_storage(self):
        a, b = self.pattern(0), self.pattern(6)
        first = lc.morph(a, b, .5)
        changed = self.pattern(-6)
        second = lc.morph(a, changed, .5)
        self.assertFalse(np.array_equal(first, second))
        lc.clear_cache()
        np.testing.assert_array_equal(lc.morph(a, b, .5), first)
        with patch.object(lc, '_MAX_CACHE_BYTES', 1):
            lc.morph(a, self.pattern(10), .5)
            self.assertEqual(len(lc._CACHE), 0)

    def test_cropped_edges_do_not_gain_artificial_transparent_gap(self):
        a, b = self.pattern(), self.pattern(6)
        for image, dy in ((a, 0), (b, 4)):
            ImageDraw.Draw(image).rectangle((0, 70 + dy, 55, 110 + dy), fill=(220, 220, 220, 255))
        actual = np.asarray(lc.morph(a, b, .5))
        self.assertTrue(np.all(actual[79:105, 0, 3] == 255))

    def test_public_preview_and_real_export_match_with_tone_and_source_immutability(self):
        before = self.hashes()
        recipe = dict(self.r, tone=28, contrast=8, dx=2)
        le.handle('save', self.j, dict(recipe=recipe))
        loaded = le.handle('load', self.j, {})['recipe']
        self.assertEqual(loaded['closure'], recipe['closure'])
        expected = {i: self.preview(loaded, i)[0] for i in range(1, 10)}
        lc.clear_cache()
        original_thread = le.threading.Thread
        threads = []
        def track(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            threads.append(thread)
            return thread
        with patch.object(le.threading, 'Thread', side_effect=track):
            state = le.handle('export', self.j, dict(recipe=loaded))
        self.assertEqual(state['state'], 'running')
        threads[0].join(45)
        self.assertFalse(threads[0].is_alive(), 'Synthetic export exceeded 45 seconds')
        state = le.handle('status', self.j, {})
        self.assertEqual(state['state'], 'complete', state.get('phase'))
        output = self.folder / 'loop_edits' / state['version']
        for index, image in expected.items():
            with Image.open(output / 'transparent_png' / f'frame_{index:08d}.png') as actual:
                np.testing.assert_array_equal(actual.convert('RGBA'), image)
                self.assertEqual(actual.size, (160, 160))
        report = json.loads((output / 'verification.json').read_text('utf-8'))
        self.assertEqual(report['frames'], 9)
        self.assertEqual(report['dimensions'], [160, 160])
        self.assertLessEqual(report['alpha_max_error'], 2)
        closure = report['loop_closure']
        self.assertTrue(closure['endpoints_equal'])
        self.assertEqual(closure['max_channel_error'], 0)
        self.assertEqual(closure['changed_pixels'], 0)
        self.assertGreater(closure['head_adjacent_delta'], 0)
        self.assertGreater(closure['tail_adjacent_delta'], 0)
        self.assertEqual(closure['reference'], 4)
        self.assertEqual(self.hashes(), before)

    def test_stale_export_binding_rejects_changed_source_before_render(self):
        fingerprint = le.closure_fingerprint(self.j, self.r)
        path = self.png / 'frame_00000004.png'
        stamp = path.stat()
        self.pattern(20).save(path)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 10_000_000))
        out = self.folder / 'stale'
        out.mkdir()
        with patch.object(le, 'render_frame', side_effect=AssertionError('Must not render stale export')):
            le.RUNS[self.jid] = dict(state='running')
            le.export_worker(self.j, self.r, out, fingerprint)
        self.assertEqual(le.RUNS[self.jid]['state'], 'error')
        self.assertIn('來源 PNG', le.RUNS[self.jid]['phase'])
        self.assertFalse((out / 'transparent_png').exists())

    def test_missing_source_does_not_leave_export_permanently_running(self):
        (self.png / 'frame_00000004.png').unlink()
        with self.assertRaises(ValueError):
            le.handle('export', self.j, dict(recipe=self.r))
        self.assertNotEqual(le.RUNS.get(self.jid, {}).get('state'), 'running')
        self.assertFalse(list((self.folder / 'loop_edits').glob('修整*')))

    def test_unsupported_canvas_is_rejected_before_render_even_with_endpoint_only_ranges(self):
        for dimensions in ([40000, 8], [8, 7], [4096, 4096]):
            with self.subTest(dimensions=dimensions), self.assertRaisesRegex(ValueError, '畫布'):
                le.recipe(dict(self.j, dimensions=dimensions),
                          dict(start=1, end=4, ramp=2, closure=dict(enabled=True)))


if __name__ == '__main__':
    unittest.main()
