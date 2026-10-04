"""Queue state-machine tests. Workers are mocked; no GPU or user assets run."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from queue_manager import QueueManager


def jid(number):
    return f'{number:032x}'


class FakeEngine:
    def __init__(self):
        self.LOCK = threading.RLock()
        self.ACTIVE = {}
        self.STOP = {}
        self.jobs = {}
        self.started = []

    def add_job(self, number, count=5):
        key = jid(number)
        self.jobs[key] = dict(id=key, name=f'clip_{number}.mp4', state='ready', phase='ready', approved=False,
            frames=[dict(status='pending') for _ in range(count)], config=dict(preview_frames=2), preview_indices=list(range(min(2, count))), error='')
        return key

    def read(self, key):
        if key not in self.jobs:
            raise FileNotFoundError('missing job')
        return copy.deepcopy(self.jobs[key])

    def update(self, key, **fields):
        self.jobs[key].update(fields)
        return self.read(key)

    @staticmethod
    def atomic_json(path, value):
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        temp.replace(path)

    def start(self, key, mode):
        if self.ACTIVE:
            raise ValueError('worker already active')
        if mode == 'full' and not self.jobs[key]['approved']:
            raise ValueError('full mode requires approval')
        self.started.append((key, mode, sum(f['status'] == 'done' for f in self.jobs[key]['frames'])))
        self.STOP[key] = threading.Event()
        self.ACTIVE[key] = object()
        self.update(key, state='running', last_mode=mode, phase='working')

    def finish(self, key, state='complete', error=''):
        self.ACTIVE.pop(key, None)
        if state == 'complete':
            for frame in self.jobs[key]['frames']:
                frame['status'] = 'done'
        elif state == 'review':
            for index in self.jobs[key]['preview_indices']:
                self.jobs[key]['frames'][index]['status'] = 'done'
        self.update(key, state=state, phase=state, error=error)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'batch_queue.json'
        self.engine = FakeEngine()
        self.keys = [self.engine.add_job(i) for i in range(1, 5)]
        self.shared = threading.RLock()
        self.exporting = False
        self.queue = self.make_queue()

    def tearDown(self):
        self.temp.cleanup()

    def make_queue(self):
        return QueueManager(self.path, self.engine, self.shared, lambda: self.exporting, autothread=False)

    def states(self):
        return [item['state'] for item in self.queue.snapshot()['items']]

    def test_add_deduplicates_without_starting_and_validates_batch(self):
        result = self.queue.add([self.keys[0], self.keys[0], self.keys[1]], 'full')
        self.assertEqual(result['added'], self.keys[:2])
        self.queue.tick()
        self.assertEqual(self.engine.started, [])
        self.assertTrue(result['paused'])
        again = self.queue.add(self.keys[:2], 'preview')
        self.assertEqual(again['skipped'], self.keys[:2])
        self.assertEqual([item['mode'] for item in again['items']], ['full', 'full'])
        with self.assertRaises(FileNotFoundError):
            self.queue.add([self.keys[2], jid(99)], 'full')
        self.assertEqual(len(self.queue.items), 2)
        with self.assertRaises(ValueError):
            self.queue.add(['../escape'], 'full')

    def test_serial_full_jobs_and_failure_do_not_block_later_items(self):
        self.queue.add(self.keys[:3], 'full')
        self.queue.start()
        self.queue.tick()
        self.queue.tick()
        self.assertEqual(len(self.engine.started), 1)
        self.engine.finish(self.keys[0])
        self.queue.tick()
        self.assertEqual(self.engine.started[-1][:2], (self.keys[1], 'full'))
        self.engine.finish(self.keys[1], 'attention', 'mock model failure')
        self.queue.tick()
        self.assertEqual(self.engine.started[-1][0], self.keys[2])
        self.engine.finish(self.keys[2])
        self.queue.tick()
        self.assertEqual(self.states(), ['complete', 'error', 'complete'])
        self.assertEqual(self.queue.items[1]['error'], 'mock model failure')
        self.assertTrue(self.queue.paused)
        self.assertIsNone(self.queue.active_job_id)

    def test_preview_stops_at_review_then_advances_and_can_approve_full(self):
        self.queue.add(self.keys[:2], 'preview')
        self.queue.start()
        self.queue.tick()
        self.engine.finish(self.keys[0], 'review')
        self.queue.tick()
        self.assertEqual(self.states(), ['review', 'running'])
        self.assertEqual(self.queue.items[0]['done'], 2)
        self.assertFalse(self.engine.jobs[self.keys[0]]['approved'])
        self.engine.finish(self.keys[1], 'review')
        self.queue.tick()
        self.queue.retry(self.keys[0], 'full')
        self.queue.tick()
        self.assertEqual(len(self.engine.started), 2, 'retry does not unpause the queue')
        self.queue.start()
        self.queue.tick()
        self.assertEqual(self.engine.started[-1], (self.keys[0], 'full', 2))

    def test_pause_and_resume_preserve_completed_frames(self):
        self.queue.add(self.keys[:2], 'full')
        self.queue.start()
        self.queue.tick()
        for frame in self.engine.jobs[self.keys[0]]['frames'][:3]:
            frame['status'] = 'done'
        self.queue.pause()
        self.assertTrue(self.engine.STOP[self.keys[0]].is_set())
        self.engine.finish(self.keys[0], 'paused')
        self.queue.tick()
        self.assertEqual(self.states(), ['paused', 'pending'])
        self.assertEqual(len(self.engine.started), 1)
        self.queue.start()
        self.queue.tick()
        self.assertEqual(self.engine.started[-1], (self.keys[0], 'full', 3))
        self.assertFalse(self.engine.STOP[self.keys[0]].is_set())

    def test_restart_retains_queue_and_requires_explicit_start(self):
        self.queue.add(self.keys[:2], 'full')
        self.queue.start()
        self.queue.tick()
        self.engine.jobs[self.keys[0]]['frames'][0]['status'] = 'done'
        self.queue.tick()
        self.engine.finish(self.keys[0], 'paused')  # Equivalent to server recovery.
        restarted = self.make_queue()
        self.assertTrue(restarted.paused)
        self.assertTrue(restarted.recovered)
        self.assertIsNone(restarted.active_job_id)
        self.assertEqual(restarted.items[0]['state'], 'paused')
        restarted.tick()
        self.assertEqual(len(self.engine.started), 1)
        restarted.start()
        restarted.tick()
        self.assertEqual(self.engine.started[-1], (self.keys[0], 'full', 1))

    def test_start_while_pause_is_finishing_resumes_without_skipping_item(self):
        self.queue.add(self.keys[:2], 'full')
        self.queue.start()
        self.queue.tick()
        self.engine.jobs[self.keys[0]]['frames'][0]['status'] = 'done'
        self.queue.pause()
        self.queue.start()  # The old worker is still unwinding its stop request.
        self.queue.tick()
        self.assertEqual(len(self.engine.started), 1)
        self.engine.finish(self.keys[0], 'paused')
        self.queue.tick()
        self.assertEqual(self.engine.started[-1], (self.keys[0], 'full', 1))
        self.assertFalse(self.queue.paused)

    def test_waits_for_manual_job_and_export_and_does_not_stop_manual(self):
        manual = self.keys[-1]
        self.engine.update(manual, approved=True)
        self.engine.start(manual, 'full')
        self.queue.add([self.keys[0]], 'full')
        self.queue.start()
        self.queue.tick()
        self.assertEqual(self.queue.waiting_for, 'manual')
        self.queue.pause()
        self.assertFalse(self.engine.STOP[manual].is_set())
        self.engine.finish(manual)
        self.exporting = True
        self.queue.start()
        self.queue.tick()
        self.assertEqual(self.queue.waiting_for, 'export')
        self.assertEqual(len(self.engine.started), 1)
        self.exporting = False
        self.queue.tick()
        self.assertEqual(self.engine.started[-1][0], self.keys[0])

    def test_observes_external_approval_and_manual_pause_of_queued_job(self):
        key = self.keys[0]
        self.queue.add(self.keys[:2], 'preview')
        self.engine.update(key, approved=True)
        self.engine.start(key, 'full')
        self.queue.start()
        self.queue.tick()
        item = self.queue.snapshot()['items'][0]
        self.assertTrue(item['external'])
        self.assertEqual(item['state'], 'running')
        self.assertIsNone(self.queue.active_job_id)
        self.queue.pause()
        self.assertFalse(self.engine.STOP[key].is_set())
        self.engine.finish(key)
        self.queue.tick()
        self.assertEqual(self.states()[0], 'complete')
        self.engine.start(self.keys[1], 'preview')
        self.queue.start()
        self.queue.tick()
        self.engine.finish(self.keys[1], 'paused')
        self.queue.tick()
        self.assertTrue(self.queue.paused, 'manual pause must not be immediately resumed')
        self.assertEqual(self.states(), ['complete', 'paused'])

    def test_remove_and_reorder_only_change_queue_records(self):
        before = copy.deepcopy(self.engine.jobs)
        self.queue.add(self.keys[:3], 'full')
        self.queue.move(self.keys[2], 'up')
        self.assertEqual([item['job_id'] for item in self.queue.items], [self.keys[0], self.keys[2], self.keys[1]])
        self.queue.remove(self.keys[1])
        self.assertEqual(self.engine.jobs, before)
        self.queue.start()
        self.queue.tick()
        with self.assertRaises(ValueError):
            self.queue.remove(self.keys[0])
        with self.assertRaises(ValueError):
            self.queue.move(self.keys[0], 'down')

    def test_already_complete_is_not_regenerated_and_missing_job_does_not_block(self):
        self.queue.add(self.keys[:3], 'full')
        self.engine.finish(self.keys[0])
        del self.engine.jobs[self.keys[1]]
        self.queue.start()
        self.queue.tick()
        self.queue.tick()
        self.queue.tick()
        self.assertEqual(self.states(), ['complete', 'error', 'running'])
        self.assertEqual([value[0] for value in self.engine.started], [self.keys[2]])

    def test_corrupt_persistence_is_kept_and_not_silently_overwritten(self):
        invalid = '{not a queue'
        self.path.write_text(invalid, encoding='utf-8')
        broken = self.make_queue()
        self.assertTrue(broken.snapshot()['error'])
        with self.assertRaises(ValueError):
            broken.start()
        with self.assertRaises(ValueError):
            broken.add([self.keys[0]], 'full')
        self.assertEqual(self.path.read_text(encoding='utf-8'), invalid)

    def test_shared_start_lock_prevents_dispatch_during_export_start(self):
        self.queue.add([self.keys[0]], 'full')
        self.queue.start()
        attempted = threading.Event()
        def dispatch():
            attempted.set()
            self.queue.tick()
        with self.shared:
            thread = threading.Thread(target=dispatch)
            thread.start()
            self.assertTrue(attempted.wait(1))
            self.assertEqual(self.engine.started, [])
            self.exporting = True
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.engine.started, [])
        self.assertEqual(self.queue.waiting_for, 'export')

    def test_http_api_returns_state_and_requires_mutation_token(self):
        import server
        import queue_manager
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        port = http.server_address[1]
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        url = f'http://127.0.0.1:{port}'
        with patch.object(server, 'PORT', port), patch.object(queue_manager, '_manager', self.queue):
            thread.start()
            try:
                with urllib.request.urlopen(url + '/api/queue') as response:
                    self.assertEqual(json.load(response)['items'], [])
                body = json.dumps(dict(job_ids=[self.keys[0]], mode='preview')).encode()
                request = urllib.request.Request(url + '/api/queue/add', data=body, headers={'Content-Type': 'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 403)
                request.add_header('X-Token', server.TOKEN)
                with urllib.request.urlopen(request) as response:
                    data = json.load(response)
                self.assertEqual(data['items'][0]['state'], 'pending')
                self.assertTrue(data['paused'])
                self.assertEqual(self.engine.started, [])
                with urllib.request.urlopen(url + '/api/version') as response:
                    version = json.load(response)
                self.assertEqual(version['version'], '3.9')
                self.assertEqual(version['features']['transition_tone'], 1)
                self.assertEqual(version['features']['transition_sequence'], 2)
                self.assertEqual(version['features']['transition_seams'], 1)
                self.assertEqual(version['features']['queue'], 1)
                self.assertEqual(version['features']['region'], 3)
                self.assertEqual(version['features']['alignment'], 1)
                self.assertEqual(version['features']['closure'], 1)
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=2)


class CreateRequestTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        self.created = []
        self.body = dict(source=str(self.root / 'clip.mp4'), config={'fps': 24, 'original_name': 'clip.mp4'}, request_id=jid(100))
        self.patch_jobs = patch.object(server.e, 'JOBS', self.jobs)
        self.patch_create = patch.object(server.e, 'create', side_effect=self.mock_create)
        self.patch_jobs.start()
        self.patch_create.start()

    def tearDown(self):
        self.patch_create.stop()
        self.patch_jobs.stop()
        self.temp.cleanup()

    def mock_create(self, source, config):
        self.assertFalse(self.server.e.LOCK._is_owned(), 'ffprobe must not run under the engine-wide lock')
        key = jid(len(self.created) + 1)
        name = config.pop('original_name', 'clip.mp4')  # Match the existing engine behavior.
        job = dict(id=key, name=name, source=source, config=copy.deepcopy(config), state='ready')
        folder = self.jobs / key
        folder.mkdir()
        FakeEngine.atomic_json(folder / 'job.json', job)
        self.created.append(job)
        return copy.deepcopy(job)

    def test_lost_response_retry_returns_committed_job_without_creating_again(self):
        original = copy.deepcopy(self.body)
        first = self.server.create_job(self.body)
        second = self.server.create_job(self.body)
        self.assertEqual(second, first)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.body, original, 'the caller settings snapshot stays intact')

    def test_reusing_request_id_with_changed_source_or_settings_is_rejected(self):
        self.server.create_job(self.body)
        for changed in (dict(self.body, source=str(self.root / 'other.mp4')),
                        dict(self.body, config=dict(self.body['config'], fps=12))):
            with self.assertRaisesRegex(ValueError, '不同來源或設定'):
                self.server.create_job(changed)
        self.assertEqual(len(self.created), 1)

    def test_concurrent_same_request_is_created_once(self):
        from concurrent.futures import ThreadPoolExecutor
        entered, release = threading.Event(), threading.Event()
        def delayed_create(source, config):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.mock_create(source, config)
        with patch.object(self.server.e, 'create', side_effect=delayed_create), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.server.create_job, copy.deepcopy(self.body))
            self.assertTrue(entered.wait(2))
            second = pool.submit(self.server.create_job, copy.deepcopy(self.body))
            release.set()
            self.assertEqual(first.result(timeout=3), second.result(timeout=3))
        self.assertEqual(len(self.created), 1)

    def test_legacy_calls_are_unchanged_and_invalid_id_creates_nothing(self):
        invalid = dict(self.body, request_id='../invalid')
        with self.assertRaises(ValueError):
            self.server.create_job(invalid)
        self.assertEqual(self.created, [])
        legacy = {key: value for key, value in self.body.items() if key != 'request_id'}
        first = self.server.create_job(copy.deepcopy(legacy))
        second = self.server.create_job(copy.deepcopy(legacy))
        self.assertNotEqual(first['id'], second['id'])


if __name__ == '__main__':
    unittest.main()
