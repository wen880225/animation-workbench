"""Conservative, explicitly requested restart of this Windows workbench only.

The legacy service has no atomic shutdown lock. Users must save drafts and stop
upload/alignment/calibration operations before invoking the restart launcher.
No process-name killing, child-tree killing, or implicit restart is performed.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request


class RestartRefused(RuntimeError):
    pass


def request_json(url, token=None, data=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Token'] = token
    request = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode('utf-8'), headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def inspect_service(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            html = response.read().decode('utf-8')
        match = re.search(r"window\.APP_TOKEN\s*=\s*'([A-Za-z0-9]+)'", html)
        info = request_json(url + '/api/version')
        if not match or 'loop.js' not in html or not isinstance(info, dict) or not isinstance(info.get('version'), str) or info.get('workspace') != 3:
            raise ValueError('無法確認為動畫工作台')
        return {'recognized': True, 'version': info['version'], 'token': match.group(1), 'info': info}
    except Exception as error:
        return {'recognized': False, 'version': None, 'token': None, 'reason': '無法確認連接埠上的工作台服務：' + str(error)}


def port_open(port):
    with socket.socket() as sock:
        sock.settimeout(.5)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def _powershell_json(script):
    if os.name != 'nt':
        raise RestartRefused('自動重新啟動僅支援 Windows。')
    prefix = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
    try:
        completed = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', prefix + script], capture_output=True, encoding='utf-8-sig', timeout=12, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return json.loads(completed.stdout)
    except Exception as error:
        # Do not print another application's command line or shell output.
        raise RestartRefused('無法可靠查詢 Windows 服務身分，已保留現有程序。') from error


def listener_pid(port):
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise RestartRefused('連接埠設定無效。')
    rows = _powershell_json("$rows=@(Get-NetTCPConnection -State Listen | Where-Object {$_.LocalPort -eq " + str(port) + "} | Select-Object OwningProcess); ConvertTo-Json -InputObject @($rows) -Compress")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) or type(row.get('OwningProcess')) is not int or row['OwningProcess'] <= 0 for row in rows):
        raise RestartRefused('無法確認唯一的監聽程序，已保留現有程序。')
    pids = {row['OwningProcess'] for row in rows}
    if len(pids) != 1:
        raise RestartRefused('同一連接埠有多個程序，無法安全重新啟動。')
    return pids.pop()


def process_record(pid):
    if type(pid) is not int or pid <= 0:
        raise RestartRefused('程序編號無效。')
    result = _powershell_json("$p=Get-CimInstance Win32_Process -Filter 'ProcessId = " + str(pid) + "'; if($null -eq $p){throw 'missing process'}; [pscustomobject]@{pid=[int]$p.ProcessId; executable=$p.ExecutablePath; command_line=$p.CommandLine; created=$p.CreationDate.ToUniversalTime().ToString('o')} | ConvertTo-Json -Compress")
    if not isinstance(result, dict) or result.get('pid') != pid or any(not isinstance(result.get(key), str) or not result[key] for key in ('executable', 'command_line', 'created')):
        raise RestartRefused('程序資訊不完整，已保留現有程序。')
    return result


def windows_argv(command_line):
    if os.name != 'nt':
        raise RestartRefused('程序命令列解析僅支援 Windows。')
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    values = shell.CommandLineToArgvW(command_line, ctypes.byref(count))
    if not values:
        raise RestartRefused('無法解析程序命令列。')
    try:
        return [values[i] for i in range(count.value)]
    finally:
        kernel.LocalFree(ctypes.cast(values, wintypes.HLOCAL))


def canonical(path):
    value = Path(path)
    if not value.is_absolute():
        raise RestartRefused('相對路徑無法確認服務身分，請手動關閉舊服務。')
    return os.path.normcase(str(value.resolve()))


def allowed_python_paths(root, python_executable):
    paths = [python_executable, getattr(sys, '_base_executable', sys.executable), Path(root) / '.venv' / 'Scripts' / 'python.exe', Path(root) / 'venv' / 'Scripts' / 'python.exe']
    return {canonical(path) for path in paths if path and Path(path).is_file()}


@dataclass(frozen=True)
class Target:
    pid: int
    executable: str
    command_line: str
    created: str


def validate_record(record, root, port, allowed, parser=None):
    if not isinstance(record, dict) or type(record.get('pid')) is not int or record['pid'] <= 0 or any(not isinstance(record.get(key), str) or not record[key] for key in ('executable', 'command_line', 'created')):
        raise RestartRefused('程序資訊不完整，已保留現有程序。')
    executable = canonical(record['executable'])
    argv = (parser or windows_argv)(record['command_line'])
    # Windows venv launchers can report the base Python image while preserving
    # the venv interpreter in argv[0]. Both must be explicitly expected paths.
    if executable not in allowed or len(argv) < 4 or canonical(argv[0]) not in allowed:
        raise RestartRefused('此連接埠不是由本工作台預期的 Python 啟動，已保留現有程序。')
    if canonical(argv[1]) != canonical(Path(root) / 'server.py'):
        raise RestartRefused('此連接埠屬於其他資料夾或未知服務，已保留現有程序。')
    found_port = None
    no_browser = False
    index = 2
    while index < len(argv):
        argument = argv[index]
        if argument == '--no-browser' and not no_browser:
            no_browser = True
        elif argument == '--port' and found_port is None and index + 1 < len(argv):
            index += 1
            found_port = argv[index]
        elif argument.startswith('--port=') and found_port is None:
            found_port = argument.partition('=')[2]
        else:
            raise RestartRefused('服務啟動參數不符合本工作台，已保留現有程序。')
        index += 1
    if found_port != str(port):
        raise RestartRefused('服務啟動連接埠與監聽連接埠不一致。')
    return Target(record['pid'], executable, record['command_line'], record['created'])


def inspect_target(root, port, python_executable):
    pid = listener_pid(port)
    return validate_record(process_record(pid), root, port, allowed_python_paths(root, python_executable))


def _ids_from_disk(root, directory, metadata_name):
    result = set()
    location = Path(root) / directory
    if not location.exists():
        return result
    if not location.is_dir():
        raise RestartRefused(directory + ' 資料夾狀態異常。')
    for folder in location.iterdir():
        if not folder.is_dir():
            continue
        path = folder / metadata_name
        try:
            record = json.loads(path.read_text(encoding='utf-8-sig'))
            identifier = record.get('id') if isinstance(record, dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier) or identifier in result:
                raise ValueError('invalid or duplicate id')
            result.add(identifier)
        except Exception as error:
            raise RestartRefused(directory + ' 中有無法確認的任務記錄，請先檢查資料夾或完成匯入。') from error
    return result


IDLE_STATES = {'idle', 'ready', 'review', 'complete', 'attention', 'paused', 'error'}


def _state_reason(value, label, allow_empty=False):
    if not isinstance(value, dict) or (not value and not allow_empty):
        return label + ' 的狀態資料不完整。'
    if not value and allow_empty:
        return None  # Legacy loop/status uses {} when there has been no export.
    state = value.get('state')
    if state == 'running':
        return label + ' 仍在執行，請等待完成或先暫停。'
    if state not in IDLE_STATES:
        return label + ' 回傳未知狀態，已取消重新啟動。'
    return None


def idle_reasons(root, url, token):
    """Check all known jobs, including test/hidden disk entries and every project."""
    reasons = []
    try:
        queue = request_json(url + '/api/queue')
        if not isinstance(queue, dict) or not isinstance(queue.get('paused'), bool) or not isinstance(queue.get('items'), list) or 'active_job_id' not in queue or not isinstance(queue.get('counts'), dict) or not isinstance(queue.get('error'), str):
            raise ValueError('queue schema')
        if queue['error']:
            reasons.append('隊列回報錯誤，請先檢查隊列狀態後再重新啟動。')
        if not queue['paused']:
            reasons.append('任務隊列尚未暫停，請先按暫停隊列。')
        if queue['active_job_id'] is not None:
            reasons.append('任務隊列仍有作用中的項目。')
        if type(queue['counts'].get('running')) is not int or queue['counts']['running'] < 0:
            raise ValueError('queue counts')
        if queue['counts']['running']:
            reasons.append('任務隊列仍有執行中的項目。')
        for item in queue['items']:
            if not isinstance(item, dict) or item.get('state') not in IDLE_STATES | {'pending', 'running'} or not isinstance(item.get('job_id'), str):
                raise ValueError('queue item')
            if item['state'] == 'running':
                reasons.append('任務隊列仍有執行中的項目。')
        jobs = request_json(url + '/api/jobs')
        if not isinstance(jobs, list):
            raise ValueError('jobs schema')
        ids = _ids_from_disk(root, 'jobs', 'job.json')
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get('id'), str) or not re.fullmatch(r'[0-9a-f]{32}', job['id']):
                raise ValueError('job schema')
            ids.add(job['id'])
            reason = _state_reason(job, '影片任務 ' + job['id'][:8])
            if reason:
                reasons.append(reason)
        # Read disk job states as well because /api/jobs omits is_test jobs.
        for path in (Path(root) / 'jobs').glob('*/job.json'):
            record = json.loads(path.read_text(encoding='utf-8-sig'))
            reason = _state_reason(record, '影片任務 ' + record['id'][:8])
            if reason:
                reasons.append(reason)
        for identifier in sorted(ids):
            status = request_json(url + '/api/loop/status', token, {'id': identifier})
            reason = _state_reason(status, '單段修整 ' + identifier[:8], allow_empty=True)
            if reason:
                reasons.append(reason)
        projects = request_json(url + '/api/transitions/list', token, {})
        if not isinstance(projects, dict) or not isinstance(projects.get('projects'), list):
            raise ValueError('projects schema')
        ids = _ids_from_disk(root, 'transition_projects', 'project.json')
        for project in projects['projects']:
            if not isinstance(project, dict) or not isinstance(project.get('id'), str) or not re.fullmatch(r'[0-9a-f]{32}', project['id']):
                raise ValueError('project schema')
            ids.add(project['id'])
        for identifier in sorted(ids):
            status = request_json(url + '/api/transitions/status', token, {'project_id': identifier})
            reason = _state_reason(status, '多動畫輸出 ' + identifier[:8])
            if reason:
                reasons.append(reason)
    except Exception as error:
        reasons.append(str(error) if isinstance(error, RestartRefused) else '無法完整讀取隊列／任務／修整／表情組狀態，已取消重新啟動。')
    return list(dict.fromkeys(reasons))


class NativeProcess:
    """An exact Windows process handle; PID reuse never retargets this handle."""
    def __init__(self, pid):
        if os.name != 'nt':
            raise RestartRefused('自動重新啟動僅支援 Windows。')
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x1000 | 0x100000 | 0x0001, False, pid)
        if not self.handle:
            raise RestartRefused('無法取得此服務的程序控制權，已保留現有程序。')
        try:
            self.created = self.creation_time()
        except Exception:
            self.close()
            raise

    def creation_time(self):
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.kernel.GetProcessTimes(self.handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            raise RestartRefused('無法驗證原程序建立時間。')
        return (created.dwHighDateTime << 32) | created.dwLowDateTime

    def alive(self):
        return self.kernel.WaitForSingleObject(self.handle, 0) == 258

    def terminate(self):
        if not self.alive() or self.creation_time() != self.created:
            raise RestartRefused('原程序已變更，已取消重新啟動。')
        if not self.kernel.TerminateProcess(self.handle, 0):
            raise RestartRefused('無法停止已確認的工作台程序。')
        if self.kernel.WaitForSingleObject(self.handle, 5000) != 0:
            raise RestartRefused('原工作台尚未停止，請稍後再試。')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def diagnose(root, port, url, python_executable):
    service = inspect_service(url)
    result = {'version': service.get('version'), 'matched_pid': None, 'reasons': []}
    if not service['recognized']:
        result['reasons'].append(service['reason'])
        return result
    try:
        target = inspect_target(root, port, python_executable)
        result['matched_pid'] = target.pid
        result['reasons'] = idle_reasons(root, url, service['token'])
    except RestartRefused as error:
        result['reasons'].append(str(error))
    return result


def restart_existing(root, port, url, python_executable, emit=print):
    service = inspect_service(url)
    if not service['recognized']:
        raise RestartRefused(service['reason'])
    target = inspect_target(root, port, python_executable)
    with NativeProcess(target.pid) as held:
        if inspect_target(root, port, python_executable) != target or not held.alive():
            raise RestartRefused('檢查期間服務身分已變更，已取消重新啟動。')
        reasons = idle_reasons(root, url, service['token'])
        if reasons:
            raise RestartRefused('\n'.join(reasons))
        # The legacy API cannot lock out new work: check the entire workload a
        # second time immediately before rechecking identity and stopping it.
        reasons = idle_reasons(root, url, service['token'])
        if reasons:
            raise RestartRefused('\n'.join(reasons))
        if inspect_target(root, port, python_executable) != target or not held.alive() or held.creation_time() != held.created:
            raise RestartRefused('檢查期間連接埠或程序已變更，已取消重新啟動。')
        emit('已確認本資料夾的閒置工作台 v' + str(service['version']) + '（PID ' + str(target.pid) + '），正在重新啟動。')
        held.terminate()
    for _ in range(20):
        if not port_open(port):
            return target.pid
        time.sleep(.1)
    raise RestartRefused('連接埠仍被使用，未啟動第二個服務；請稍後再試。')
