"""Restart safety regressions use synthetic metadata and mocked processes only."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import launcher
import launcher_restart as lr


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='workbench launcher test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'server.py').write_text('# synthetic server', encoding='utf-8')
        self.jid = 'a' * 32
        self.pid = 'b' * 32
        self.pid2 = 'c' * 32
        self.queue = {'paused': True, 'items': [], 'active_job_id': None, 'counts': {'running': 0}, 'error': ''}
        self.jobs = []
        self.projects = []
        self.loops = {}
        self.runs = {}
        self.reads = []

    def api(self, url, token=None, data=None):
        self.reads.append((url, data))
        if url.endswith('/api/queue'):
            return self.queue
        if url.endswith('/api/jobs'):
            return self.jobs
        if url.endswith('/api/loop/status'):
            return self.loops.get(data['id'], {})
        if url.endswith('/api/transitions/list'):
            return {'projects': self.projects}
        if url.endswith('/api/transitions/status'):
            return self.runs.get(data['project_id'], {'state': 'idle'})
        raise AssertionError(url)

    def reasons(self):
        with patch.object(lr, 'request_json', side_effect=self.api):
            return lr.idle_reasons(self.root, 'http://127.0.0.1:8772', 'synthetic')

    def test_current_service_reused_without_stop_or_spawn(self):
        with patch.object(launcher, 'ready', return_value=True), patch.object(lr, 'port_open', return_value=True), patch.object(lr, 'restart_existing') as stop, patch.object(launcher.subprocess, 'Popen') as spawn, patch.object(launcher.webbrowser, 'open') as browser, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(['--no-browser']), 0)
        stop.assert_not_called()
        spawn.assert_not_called()
        browser.assert_not_called()

    def test_old_or_unknown_service_normal_start_never_stops(self):
        for service in ({'recognized': True, 'version': '3.7'}, {'recognized': False, 'version': None}):
            output = io.StringIO()
            with self.subTest(service=service), patch.object(launcher, 'ready', return_value=False), patch.object(lr, 'port_open', return_value=True), patch.object(lr, 'inspect_service', return_value=service), patch.object(lr, 'restart_existing') as stop, patch.object(launcher.subprocess, 'Popen') as spawn, contextlib.redirect_stdout(output):
                self.assertEqual(launcher.main(['--no-browser']), 1)
            stop.assert_not_called()
            spawn.assert_not_called()
            self.assertIn('重新啟動工作台.cmd' if service['recognized'] else '未知服務', output.getvalue())

    def test_full_scan_finds_hidden_job_loop_and_last_multi_project(self):
        folder = self.root / 'jobs' / '教學素材_003'
        folder.mkdir(parents=True)
        (folder / 'job.json').write_text(json.dumps({'id': self.jid, 'is_test': True, 'state': 'complete'}), encoding='utf-8')
        self.loops[self.jid] = {'state': 'running'}
        self.projects = [{'id': self.pid}, {'id': self.pid2}]
        self.runs[self.pid2] = {'state': 'running'}
        reasons = self.reasons()
        self.assertTrue(any('單段修整' in r for r in reasons))
        self.assertTrue(any(self.pid2[:8] in r and '多動畫輸出' in r for r in reasons))
        self.assertTrue(any(data == {'id': self.jid} for _, data in self.reads))

    def test_unpaused_unknown_malformed_api_and_metadata_fail_closed(self):
        self.queue['paused'] = False
        self.assertTrue(any('尚未暫停' in r for r in self.reasons()))
        self.queue['paused'] = True
        self.queue['error'] = 'synthetic queue error'
        self.assertTrue(any('隊列回報錯誤' in r for r in self.reasons()))
        self.queue['error'] = ''
        self.jobs = [{'id': self.jid, 'state': 'unrecognized'}]
        self.assertTrue(any('未知狀態' in r for r in self.reasons()))
        self.jobs = []
        folder = self.root / 'transition_projects' / self.pid
        folder.mkdir(parents=True)
        (folder / 'project.json').write_text('{broken', encoding='utf-8')
        self.assertTrue(any('無法確認的任務記錄' in r for r in self.reasons()))
        with patch.object(lr, 'request_json', side_effect=OSError('synthetic failure')):
            self.assertTrue(lr.idle_reasons(self.root, 'http://127.0.0.1:8772', 'synthetic'))

    def test_finished_error_states_are_idle(self):
        self.jobs = [{'id': self.jid, 'state': 'attention'}]
        self.loops[self.jid] = {'state': 'error'}
        self.projects = [{'id': self.pid}]
        self.runs[self.pid] = {'state': 'error'}
        self.assertEqual(self.reasons(), [])

    def test_exact_identity_rejects_relative_wrong_root_port_and_unknown_exe(self):
        allowed = {lr.canonical(sys.executable)}
        args = [sys.executable, str(self.root / 'server.py'), '--no-browser', '--port', '8772']
        record = {'pid': 54321, 'executable': sys.executable, 'command_line': subprocess.list2cmdline(args), 'created': '2026-10-04T00:00:00Z'}
        self.assertEqual(lr.validate_record(record, self.root, 8772, allowed).pid, 54321)
        for changed in ([sys.executable, 'server.py', '--port', '8772'], [sys.executable, str(self.root / 'other' / 'server.py'), '--port', '8772'], [sys.executable, str(self.root / 'server.py'), '--port', '8767']):
            with self.subTest(args=changed), self.assertRaises(lr.RestartRefused):
                lr.validate_record({**record, 'command_line': subprocess.list2cmdline(changed)}, self.root, 8772, allowed)
        with self.assertRaises(lr.RestartRefused):
            lr.validate_record(record, self.root, 8772, set())
        venv_exe = self.root / '.venv' / 'Scripts' / 'python.exe'
        venv_exe.parent.mkdir(parents=True)
        venv_exe.write_bytes(b'synthetic')
        redirected = {**record, 'command_line': subprocess.list2cmdline([str(venv_exe), *args[1:]])}
        self.assertEqual(lr.validate_record(redirected, self.root, 8772, allowed | {lr.canonical(venv_exe)}).pid, 54321)

    def test_restart_holds_identity_and_refuses_late_work_or_process_race(self):
        target = lr.Target(54321, str(sys.executable), 'synthetic', 'created-first')
        changed = lr.Target(54321, str(sys.executable), 'synthetic', 'created-later')
        for identities, scans, succeeds in (([target, target, changed], [[], []], False), ([target, target], [[], ['late running task']], False), ([target, target, target], [[], []], True)):
            held = Mock(created=100)
            held.alive.return_value = True
            held.creation_time.return_value = 100
            native = Mock()
            native.__enter__ = Mock(return_value=held)
            native.__exit__ = Mock(return_value=None)
            with self.subTest(succeeds=succeeds), patch.object(lr, 'inspect_service', return_value={'recognized': True, 'version': '3.7', 'token': 'synthetic'}), patch.object(lr, 'inspect_target', side_effect=identities), patch.object(lr, 'NativeProcess', return_value=native), patch.object(lr, 'idle_reasons', side_effect=scans), patch.object(lr, 'port_open', return_value=False):
                if succeeds:
                    self.assertEqual(lr.restart_existing(self.root, 8772, 'http://127.0.0.1:8772', sys.executable, emit=lambda _: None), target.pid)
                    held.terminate.assert_called_once()
                else:
                    with self.assertRaises(lr.RestartRefused):
                        lr.restart_existing(self.root, 8772, 'http://127.0.0.1:8772', sys.executable, emit=lambda _: None)
                    held.terminate.assert_not_called()

    def test_diagnose_has_no_mutation_or_browser(self):
        with patch.object(lr, 'diagnose', return_value={'version': '3.7', 'matched_pid': 54321, 'reasons': []}), patch.object(lr, 'restart_existing') as stop, patch.object(launcher.subprocess, 'Popen') as spawn, patch.object(launcher.webbrowser, 'open') as browser, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(['--diagnose']), 0)
        stop.assert_not_called()
        spawn.assert_not_called()
        browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
