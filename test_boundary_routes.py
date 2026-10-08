"""One BoundaryPair pipeline for single loops and open/loop clip routes."""
import copy
import hashlib
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
import test_transition_seams as fixture
import transitions as t


class BoundaryRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.TransitionSeamTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = copy.deepcopy(self.fixture.project)

    def route(self, count, mode='loop'):
        p = copy.deepcopy(self.project)
        p['clips'] = p['clips'][:count]
        p['sequence'] = [c['key'] for c in p['clips']]
        p['route_mode'] = mode
        return p

    def test_open_route_roundtrip_and_only_adjacent_analysis(self):
        p = t.handle('save', dict(project=self.route(3, 'open')))
        self.assertEqual(p.get('route_mode'), 'open')
        self.assertEqual(t.handle('load', dict(project_id=p['id']))['project']['route_mode'], 'open')
        result = t.handle('analyze', dict(project=p, scope='sequence'))
        self.assertEqual([(row['a'], row['b']) for row in result['pairs']], list(zip(p['sequence'], p['sequence'][1:])))

    def test_single_loop_uses_existing_shared_anchor_render(self):
        p = t.handle('build_seams', dict(project=self.route(1)))['project']
        self.assertEqual(len(p['seam_closure']['links']), 1)
        canonical, sources = t.validate_project(p)
        clip = canonical['clips'][0]
        self.assertEqual(t.ts.pairs(canonical), [(clip['key'], clip['key'])])
        head = t.render_frame(clip, sources[clip['key']], clip['start'], project=canonical)[0]
        tail = t.render_frame(clip, sources[clip['key']], clip['end'], project=canonical)[0]
        np.testing.assert_array_equal(head, tail)
        preview = t.handle('render_preview', dict(project=p, a=clip['key'], b=clip['key'], span=3))
        self.assertEqual([row['frame'] for row in preview['frames']], [8, 9, 10, 1, 2, 3])

    def test_open_outer_endpoints_remain_base_and_do_not_need_transition_windows(self):
        p = self.route(3, 'open')
        p['clips'][0]['head_frames'] = 0
        p['clips'][-1]['tail_frames'] = 0
        closed = t.handle('build_seams', dict(project=p))['project']
        canonical, sources = t.validate_project(closed)
        self.assertEqual(len(closed['seam_closure']['links']), 2)
        for clip, index in [(canonical['clips'][0], 1), (canonical['clips'][-1], 10)]:
            original = t._render_base_frame(clip, sources[clip['key']], index, project=canonical)[0]
            actual = t.render_frame(clip, sources[clip['key']], index, project=canonical)[0]
            np.testing.assert_array_equal(actual, original)
        for a, b in t.ts.pairs(canonical):
            clips = {c['key']: c for c in canonical['clips']}
            np.testing.assert_array_equal(t.render_frame(clips[a], sources[a], 10, project=canonical)[0],
                                          t.render_frame(clips[b], sources[b], 1, project=canonical)[0])

    def test_single_open_route_has_no_artificial_join(self):
        p = self.route(1, 'open')
        result = t.handle('build_seams', dict(project=p))
        self.assertEqual(result['report']['links'], [])
        self.assertFalse(result['project']['seam_closure']['enabled'])
        analyzed = t.handle('analyze', dict(project=result['project'], scope='sequence'))
        self.assertEqual(analyzed['pairs'], [])

    def test_legacy_route_default_and_custom_sequences_preserved(self):
        p = self.route(3)
        p.pop('route_mode')
        p['sequence_version'] = 1
        p['sequence'] = p['sequence'][:2] + p['sequence'][:1]
        saved, sources = t.validate_project(p)
        self.assertEqual(saved.get('route_mode'), 'loop')
        self.assertEqual(saved['sequence'], p['sequence'])
        before = t.project_signature(saved, sources)
        saved.pop('route_mode')
        self.assertEqual(t.project_signature(saved, sources), before)
        p['route_mode'] = 'open'
        analyzed = t.handle('analyze', dict(project=p, scope='sequence'))
        self.assertEqual(len(analyzed['pairs']), 2)

    def test_route_change_invalidates_anchors_and_export_identity(self):
        p = t.handle('build_seams', dict(project=self.route(3)))['project']
        canonical, sources = t.validate_project(p)
        original_signature = t.project_signature(canonical, sources)
        p['route_mode'] = 'open'
        with self.assertRaisesRegex(ValueError, '重新建立'):
            t.validate_project(p)
        p['seam_closure']['enabled'] = False
        opened, sources = t.validate_project(p)
        self.assertNotEqual(t.project_signature(opened, sources), original_signature)
        for bad in ('random', False, None):
            opened['route_mode'] = bad
            with self.assertRaises(ValueError):
                t.validate_project(opened)

    def test_single_loop_public_export_and_open_manifest(self):
        class InlineThread:
            def __init__(self, target, args, **kwargs): self.target, self.args = target, args
            def start(self): self.target(*self.args)
        originals = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in self.fixture.jobs.glob('*/transparent_png/*.png')}
        for count, mode in [(1, 'loop'), (3, 'open')]:
            p = t.handle('build_seams', dict(project=self.route(count, mode)))['project']
            with patch.object(t.threading, 'Thread', InlineThread):
                status = t.handle('export', dict(project=p))
            self.assertEqual(status['state'], 'complete', status.get('phase'))
            root = t._version_dir(p['id'], status['version'])
            manifest = t._read_json(root / 'manifest.json')
            self.assertEqual(manifest.get('route_mode'), mode)
            self.assertEqual(len(manifest['clips']), count)
            self.assertEqual(len(manifest['seam_verification']['pairs']), 1 if count == 1 else 2)
            self.assertEqual(t._versions(p['id'])[0].get('route_mode'), mode)
            for item in manifest['clips']:
                self.assertLessEqual(item['alpha_max_error'], 2)
        for path, digest in originals.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_large_split_region_preview_matches_downsampled_full_render(self):
        p = self.route(1)
        clip = p['clips'][0]
        source = t.resolve_source(clip['job_id'])
        for path in source.files:
            with Image.open(path) as image:
                image.resize((768, 960), Image.Resampling.NEAREST).save(path)
        t.e.update(clip['job_id'], dimensions=[768, 960])
        clip['tail'] = dict(dx=3.2, dy=-1.1, sx=98, sy=101, angle=.3,
            region=dict(mode='split', shape='rect', x=20, y=20, w=50, h=55, feather=12,
                        outside=dict(dx=-1.7, dy=.8, sx=99, sy=100, angle=-.2)))
        canonical, sources = t.validate_project(p)
        clip = canonical['clips'][0]
        full, full_clipped = t.render_frame(clip, sources[clip['key']], clip['end'], project=canonical)
        expected = full.copy()
        expected.thumbnail((160, 200), Image.Resampling.LANCZOS)
        preview, clipped = t.render_frame(clip, sources[clip['key']], clip['end'], (160, 200), canonical)
        np.testing.assert_array_equal(preview, expected)
        self.assertEqual(clipped, full_clipped)

    def test_full_draft_route_preview_preserves_every_frame_and_has_a_memory_gate(self):
        p = self.route(3, 'open')
        result = t.handle('render_route_preview', dict(project=p))
        self.assertEqual(result['route_mode'], 'open')
        self.assertEqual(result['sequence'], p['sequence'])
        self.assertEqual([len(c['frames']) for c in result['clips']], [10, 10, 10])
        for clip in result['clips']:
            self.assertEqual([f['frame'] for f in clip['frames']], list(range(1, 11)))
            self.assertLessEqual(max(clip['preview_dimensions']), 360)
        with patch.object(t, 'ROUTE_PREVIEW_BYTES', 1):
            with self.assertRaisesRegex(ValueError, '預覽.*預算'):
                t.handle('render_route_preview', dict(project=p))
        single = t.handle('render_route_preview', dict(project=self.route(1)))
        self.assertEqual(single['route_mode'], 'loop')
        self.assertEqual(len(single['clips'][0]['frames']), 10)

    def test_route_preview_rejects_source_changed_after_it_was_rendered(self):
        p = self.route(1)
        source = t.resolve_source(p['clips'][0]['job_id'])
        original = t.render_frame
        def render(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[2] == 10:
                Image.new('RGBA', (64, 80), (30, 30, 30, 128)).save(source.files[0])
            return result
        with patch.object(t, 'render_frame', side_effect=render):
            with self.assertRaisesRegex(ValueError, '預覽期間來源已變更'):
                t.handle('render_route_preview', dict(project=p))


if __name__ == '__main__':
    unittest.main()
