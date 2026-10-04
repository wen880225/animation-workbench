"""Deleted/recreated output and job identity regressions; temporary assets only."""
import copy
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

import engine as e
import loop_editor as le
import server
import transitions as tr


class SourceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        self.patches = [patch.object(e, 'ROOT', self.root), patch.object(e, 'JOBS', self.jobs),
                        patch.dict(e.PATHS, {}, clear=True), patch.dict(le.RUNS, {}, clear=True),
                        patch.dict(tr.RUNS, {}, clear=True), patch.dict(tr._FEATURES, {}, clear=True)]
        for item in self.patches:
            item.start()
        self.addCleanup(self.cleanup)
        self.jid = 'a' * 32
        self.folder = self.jobs / 'A'
        self.folder.mkdir()
        self.job = dict(id=self.jid, name='A.mp4', dimensions=[32, 40], frames=[],
                        config=dict(fps='12'), state='complete', created=1)
        e.atomic_json(self.folder / 'job.json', self.job)
        self.make_version(2)

    def cleanup(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def make_version(self, x, count=4):
        folder = self.folder / 'loop_edits' / '修整'
        if folder.exists():
            self.assertTrue(folder.resolve().is_relative_to(self.root.resolve()))
            shutil.rmtree(folder)
        png = folder / 'transparent_png'
        png.mkdir(parents=True)
        for n in range(1, count + 1):
            im = Image.new('RGBA', (32, 40))
            ImageDraw.Draw(im).rectangle((x, 8, x+8, 32), fill=(220, 220, 220, 255))
            im.save(png / f'frame_{n:08d}.png')
        e.atomic_json(folder / 'status.json', dict(state='complete', version='修整'))
        e.atomic_json(folder / 'verification.json', dict(frames=count, dimensions=[32,40], fps='12'))
        (folder / 'loop_transparent.webm').write_bytes(b'fixture video ' + bytes([x]))
        return folder

    def test_same_named_recreation_reads_new_pixels_and_rejects_old_review(self):
        project = tr.handle('create', dict(name='refresh regression'))
        key = 'b' * 32
        project['clips'] = [dict(key=key, label='A-1', job_id=self.jid, version='修整',
                                 start=1, end=4, head_frames=0, tail_frames=0)]
        project['sequence'] = [key]
        canonical, sources = tr.validate_project(project)
        old = tr.compare(canonical, sources, key, key)
        canonical['reviews'][key+'>'+key] = dict(signature=old['signature'], verdict='pass')
        old_revision = tr.catalog()['sources'][0]['revision']
        old_video_revision = le.handle('load', self.job, {})['versions'][0]['revision']
        self.make_version(18)
        fresh = tr.catalog()['sources'][0]
        self.assertNotEqual(fresh['revision'], old_revision)
        self.assertNotEqual(le.handle('load', self.job, {})['versions'][0]['revision'], old_video_revision)
        updated, sources = tr.validate_project(canonical)
        self.assertFalse(updated['reviews'])
        self.assertNotEqual(tr.compare(updated, sources, key, key)['left'], old['left'])
        self.assertEqual(tr.render_frame(updated['clips'][0], sources[key], 4)[0].size, (32,40))

    def test_cached_deleted_job_does_not_alias_reused_folder(self):
        self.assertEqual(e.jobdir(self.jid), self.folder)
        replacement = 'c' * 32
        e.atomic_json(self.folder / 'job.json', dict(self.job, id=replacement))
        with self.assertRaises(FileNotFoundError):
            e.read(self.jid)
        self.assertEqual(e.read(replacement)['id'], replacement)
        self.assertNotIn(self.jid, e.PATHS)

    def test_renamed_job_folder_is_found_again(self):
        self.assertEqual(e.jobdir(self.jid), self.folder)
        moved = self.jobs / 'A_renamed'
        self.folder.rename(moved)
        self.assertEqual(e.jobdir(self.jid), moved)
        self.assertEqual(e.read(self.jid)['id'], self.jid)

    def test_http_missing_then_recreated_file_recovers(self):
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        port = http.server_address[1]
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        with patch.object(server, 'PORT', port):
            thread.start()
            try:
                url = f'http://127.0.0.1:{port}/file/{self.jid}/loop_edits/' + urllib.parse.quote('修整') + '/transparent_png/frame_00000001.png'
                with urllib.request.urlopen(url) as result:
                    first = result.read()
                    self.assertEqual(result.headers['Cache-Control'], 'no-store')
                frame = self.folder / 'loop_edits/修整/transparent_png/frame_00000001.png'
                frame.unlink()
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(url)
                self.make_version(18)
                with urllib.request.urlopen(url) as result:
                    self.assertNotEqual(result.read(), first)
                body = json.dumps({}).encode()
                request = urllib.request.Request(f'http://127.0.0.1:{port}/api/transitions/catalog', data=body,
                            headers={'X-Token': server.TOKEN, 'Content-Type': 'application/json'})
                with urllib.request.urlopen(request) as result:
                    self.assertEqual(json.load(result)['sources'][0]['frames'], 4)
            finally:
                http.shutdown()
                thread.join(3)
                http.server_close()


if __name__ == '__main__':
    unittest.main()
