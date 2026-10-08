"""Export freshness regressions; synthetic temporary PNGs, no model invocation."""
import copy
import io
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
import engine as e
import loop_editor as le
import transitions as tr
import server


class ArtifactStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        self.jid = 'a' * 32
        self.folder = self.jobs / 'synthetic'
        self.folder.mkdir()
        for name in ('rgba', 'frames', 'candidates', 'exports'):
            (self.folder / name).mkdir()
        self.image = Image.new('RGBA', (32, 32))
        ImageDraw.Draw(self.image).rectangle((8, 4, 23, 27), fill=(210, 210, 210, 255))
        frames = []
        for n in range(2):
            name = f'frame_{n + 1:08d}.png'
            self.image.save(self.folder / 'rgba' / name)
            frames.append(dict(file=name, index=n, status='done', prompt_id=None))
        self.job = dict(id=self.jid, name='synthetic.mp4', dimensions=[32, 32],
                        frames=frames, config=dict(fps='12', comfy='http://127.0.0.1:8188'),
                        state='complete', approved=True, extracted=True, created=1,
                        video_files=dict(full='old.webm'), exports=['old.webm'],
                        alpha_report=dict(file='old.webm', alpha_max_error=0), media_revision=0)
        (self.folder / 'exports' / 'old.webm').write_bytes(b'old immutable video')
        e.atomic_json(self.folder / 'job.json', self.job)
        self.patches = [patch.object(e, 'ROOT', self.root), patch.object(e, 'JOBS', self.jobs),
                        patch.dict(e.PATHS, {}, clear=True), patch.dict(e.ACTIVE, {}, clear=True),
                        patch.dict(e.STOP, {self.jid: threading.Event()}, clear=True),
                        patch.dict(le.RUNS, {}, clear=True), patch.dict(tr.RUNS, {}, clear=True)]
        for p in self.patches:
            p.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_retry_immediately_marks_existing_output_stale_without_deleting_it(self):
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        port = http.server_address[1]
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        with patch.object(server, 'PORT', port), patch.object(e, 'request', return_value={}), patch.object(e, 'start'):
            thread.start()
            try:
                req = urllib.request.Request(f'http://127.0.0.1:{port}/api/retry',
                    data=json.dumps(dict(id=self.jid, index=0)).encode(),
                    headers={'X-Token': server.TOKEN, 'Content-Type': 'application/json'})
                urllib.request.urlopen(req).close()
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/jobs') as response:
                    actual = json.load(response)[0]
                self.assertEqual(actual.get('export_freshness', {}).get('full', {}).get('state'), 'stale')
                self.assertIsNone(actual.get('alpha_report'))
                self.assertEqual(actual['video_files']['full'], 'old.webm')
                self.assertEqual((self.folder / 'exports/old.webm').read_bytes(), b'old immutable video')
            finally:
                http.shutdown()
                thread.join(3)
                http.server_close()

    def test_successful_regeneration_advances_revision_and_invalidates_output(self):
        e.change_frame(self.jid, 0, status='pending', prompt_id='synthetic-prompt')
        replacement = self.image.copy()
        ImageDraw.Draw(replacement).rectangle((10, 6, 21, 25), fill=(100, 100, 100, 255))
        raw = io.BytesIO()
        replacement.save(raw, format='PNG')
        history = {'synthetic-prompt': {'outputs': {'461': {'images': [{'filename': 'fake.png'}]}}}}
        with patch.object(e, 'request', side_effect=[history, raw.getvalue()]):
            e.process_frame(self.jid, 0)
        actual = e.read(self.jid)
        self.assertEqual(actual.get('media_revision'), 1)
        self.assertEqual(actual.get('export_freshness', {}).get('full', {}).get('state'), 'stale')
        self.assertIsNone(actual.get('alpha_report'))
        self.assertEqual(actual['frames'][0]['status'], 'done')
        # A later accepted frame must work after the first cleared alpha_report.
        e.invalidate_exports(self.jid, 'another replacement', media_changed=True)
        self.assertEqual(e.read(self.jid)['media_revision'], 2)

    def test_legacy_output_is_unknown_not_silently_current(self):
        actual = e.public_job(self.job)
        self.assertEqual(actual['export_freshness']['full']['state'], 'unknown')
        self.assertIsNone(actual['alpha_report'])
        self.assertEqual(e.read(self.jid)['alpha_report']['file'], 'old.webm')

    def test_current_partial_preview_does_not_require_unprocessed_full_frames(self):
        job = copy.deepcopy(self.job)
        job['frames'][1]['status'] = 'pending'
        job['video_files'] = dict(preview='preview.webm')
        job['export_freshness'] = dict(preview=dict(state='current', source_revision=0,
            report=dict(verified_frames=1)))
        job['alpha_report'] = dict(file='preview.webm', verified_frames=1, source_revision=0)
        actual = e.public_job(job)
        self.assertEqual(actual['export_freshness']['preview']['state'], 'current')
        self.assertIsNotNone(actual['alpha_report'])

    def test_reexport_marks_only_new_artifact_current(self):
        e.update(self.jid, media_revision=3,
                 video_files=dict(full='old.webm', preview='preview.webm'),
                 export_freshness={key: dict(state='stale', source_revision=0) for key in ('full', 'preview')})
        def encode(_jid, args, *unused):
            target = Path(args[-1])
            target.write_bytes(self.image.getchannel('A').tobytes() * 2 if target.suffix == '.raw' else b'new video')
        with patch.object(e, 'ffmpeg', side_effect=encode), patch.object(e, 'probe', return_value={'duration': 2 / 12}):
            e.export(self.jid)
        actual = e.public_job(e.read(self.jid))
        self.assertEqual(actual['export_freshness']['full']['state'], 'current')
        self.assertEqual(actual['export_freshness']['full']['source_revision'], 3)
        self.assertEqual(actual['export_freshness']['preview']['state'], 'stale')
        self.assertEqual(actual['alpha_report']['source_revision'], 3)

    def test_loop_version_keeps_identity_but_reports_changed_source(self):
        version = self.folder / 'loop_edits/修整'
        version.mkdir(parents=True)
        e.atomic_json(version / 'status.json', dict(state='complete', version='修整', source_revision=0))
        e.atomic_json(version / 'verification.json', dict(frames=2, dimensions=[32, 32], fps='12', source_revision=0))
        before = le.handle('load', e.read(self.jid), {})['versions'][0]
        e.update(self.jid, media_revision=1)
        after = le.handle('load', e.read(self.jid), {})['versions'][0]
        self.assertEqual(before.get('source_freshness', {}).get('state'), 'current')
        self.assertEqual(after.get('source_freshness', {}).get('state'), 'stale')
        self.assertEqual(before['revision'], after['revision'])
        self.assertEqual(after['state'], 'complete')

    def test_legacy_loop_version_predating_known_regeneration_is_stale(self):
        version = self.folder / 'loop_edits/修整'
        version.mkdir(parents=True)
        status = version / 'status.json'
        e.atomic_json(status, dict(state='complete', version='修整'))
        before = le.handle('load', e.read(self.jid), {})['versions'][0]
        e.update(self.jid, media_revision=1, media_changed_at=status.stat().st_mtime + 1)
        after = le.handle('load', e.read(self.jid), {})['versions'][0]
        self.assertEqual(before['source_freshness']['state'], 'unknown')
        self.assertEqual(after['source_freshness']['state'], 'stale')
        self.assertEqual(before['revision'], after['revision'])

    def test_multi_version_returns_saved_snapshot_and_source_staleness(self):
        project = tr.handle('create', dict(name='synthetic snapshot'))
        key = 'b' * 32
        project['clips'] = [dict(key=key, job_id=self.jid, version='original')]
        project['sequence'] = [key]
        project, sources = tr.validate_project(project)
        root = tr.projectdir(project['id']) / 'versions/v001'
        root.mkdir(parents=True)
        e.atomic_json(root / 'project_snapshot.json', project)
        e.atomic_json(root / 'status.json', dict(state='complete', version='v001'))
        e.atomic_json(root / 'manifest.json', dict(signature=tr.project_signature(project, sources), clips=[]))
        before = tr._versions(project['id'])[0]
        replacement = self.image.copy()
        ImageDraw.Draw(replacement).rectangle((3, 3, 10, 10), fill='red')
        replacement.save(self.folder / 'rgba/frame_00000001.png')
        after = tr._versions(project['id'])[0]
        self.assertEqual(before.get('project_snapshot'), project)
        self.assertEqual(before.get('source_freshness', {}).get('state'), 'current')
        self.assertEqual(after.get('source_freshness', {}).get('state'), 'stale')
        self.assertEqual(after['project_snapshot'], before['project_snapshot'])


if __name__ == '__main__':
    unittest.main()
