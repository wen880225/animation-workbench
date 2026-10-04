"""Persistent, single-worker dispatch for existing cutout jobs.

The queue never creates, deletes, or resets source frames. Dispatch shares the
same start lock as manual jobs and animation exports. A restarted process always
requires an explicit start; importing this module does not start a worker.
"""

import copy
import json
from pathlib import Path
import re
import threading
import time


STATES = {'pending', 'running', 'paused', 'review', 'complete', 'error'}
MODES = {'full', 'preview'}
MAX_ITEMS = 500


def job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('無效的隊列任務 ID')
    return value


class QueueManager:
    def __init__(self, path, engine, shared_lock, export_busy, *, autothread=True):
        self.path = Path(path)
        self.engine = engine
        self.shared_lock = shared_lock
        self.export_busy = export_busy
        self.autothread = autothread
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.thread = None
        self.items = []
        self.paused = True
        self.active_job_id = None
        self.resume_after_pause = False
        self.waiting_for = None
        self.phase = '加入影片任務後，按開始批次處理'
        self.error = ''
        self.recovered = False
        self.updated = time.time()
        self.load_error = ''
        self._last_written = None
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding='utf-8-sig'))
            if not isinstance(value, dict) or value.get('schema') != 1:
                raise ValueError('不支援的隊列記錄格式')
            records = value.get('items')
            if not isinstance(records, list) or len(records) > MAX_ITEMS:
                raise ValueError('隊列清單無效')
            seen, restored = set(), []
            for raw in records:
                if not isinstance(raw, dict):
                    raise ValueError('隊列項目無效')
                jid = job_id(raw.get('job_id'))
                if jid in seen or raw.get('mode') not in MODES or raw.get('state') not in STATES:
                    raise ValueError('隊列包含重複或無效項目')
                seen.add(jid)
                item = {key: raw.get(key) for key in ('job_id', 'name', 'mode', 'state', 'done', 'total', 'error', 'phase')}
                item.update(name=str(item.get('name') or jid), error=str(item.get('error') or ''),
                            phase=str(item.get('phase') or ''), done=max(0, int(item.get('done') or 0)), total=max(0, int(item.get('total') or 0)))
                if item['state'] == 'running':
                    item.update(state='paused', phase='上次處理中斷；按開始繼續已完成的進度')
                restored.append(item)
            self.items = restored
            self.recovered = bool(restored)
            self.phase = '已恢復隊列；按開始繼續，不會自動執行' if restored else '隊列目前沒有任務'
            self._persist()
        except (OSError, ValueError, TypeError) as exc:
            # Preserve an unreadable file instead of replacing it with an empty queue.
            self.load_error = self.error = '隊列記錄無法讀取，原檔已保留：' + str(exc)
            self.phase = self.error

    def _persist(self):
        if self.load_error:
            raise ValueError(self.load_error)
        value = dict(schema=1, paused=self.paused, active_job_id=self.active_job_id, items=self.items)
        if value == self._last_written:
            return
        self.updated = time.time()
        self.engine.atomic_json(self.path, dict(value, updated=self.updated))
        self._last_written = copy.deepcopy(value)

    def _find(self, jid):
        jid = job_id(jid)
        item = next((item for item in self.items if item['job_id'] == jid), None)
        if item is None:
            raise ValueError('這個任務不在批次隊列中')
        return item

    def _stats(self, item, job):
        frames = job.get('frames', [])
        if item['mode'] == 'preview':
            indices = job.get('preview_indices') or list(range(min(len(frames), int(job.get('config', {}).get('preview_frames', 48)))))
            selected = [frames[index] for index in indices if 0 <= index < len(frames)]
        else:
            selected = frames
        item.update(name=job.get('name', item['name']), done=sum(frame.get('status') == 'done' for frame in selected), total=len(selected))

    def _snapshot(self):
        result = dict(paused=self.paused, active_job_id=self.active_job_id, items=copy.deepcopy(self.items),
            phase=self.phase, waiting_for=self.waiting_for, recovered=self.recovered, error=self.error,
            updated=self.updated, counts={state: sum(item['state'] == state for item in self.items) for state in sorted(STATES)})
        return result

    def _refresh_records(self):
        for item in self.items:
            try:
                jid = item['job_id']
                job = self.engine.read(jid)
                self._stats(item, job)
                if jid == self.active_job_id:
                    item['phase'] = job.get('phase') or item['phase']
                elif jid in self.engine.ACTIVE:
                    item.update(state='running', external=True, error='', phase=job.get('phase') or '手動處理中')
                    if job.get('last_mode') in MODES:
                        item['mode'] = job['last_mode']
                elif item.get('external'):
                    state = job.get('state')
                    state = state if state in {'complete', 'review', 'paused'} else 'error'
                    item.update(state=state, external=False, error=job.get('error', '') if state == 'error' else '', phase=job.get('phase') or '手動任務已停止')
                    if state == 'paused':
                        self.paused = True
                        self.phase = '手動任務已暫停；批次亦停止派送，按開始可繼續'
                elif item['state'] in {'review', 'error', 'paused'} and job.get('state') in {'complete', 'review'}:
                    item.update(state=job['state'], error='', phase=job.get('phase') or '已完成')
            except (OSError, ValueError, KeyError):
                pass

    def snapshot(self):
        with self.shared_lock, self.engine.LOCK, self.lock:
            self._refresh_records()
            return self._snapshot()

    def add(self, job_ids, mode):
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError('批次模式必須是 full 或 preview')
        if not isinstance(job_ids, list) or not job_ids or len(job_ids) > MAX_ITEMS:
            raise ValueError('請提供 1–500 個任務 ID')
        ids = list(dict.fromkeys(job_id(value) for value in job_ids))
        with self.shared_lock, self.engine.LOCK, self.lock:
            if self.load_error:
                raise ValueError(self.load_error)
            existing = {item['job_id'] for item in self.items}
            new_ids = [jid for jid in ids if jid not in existing]
            if len(self.items) + len(new_ids) > MAX_ITEMS:
                raise ValueError('隊列最多保留 500 個任務，請先移除已結束項目')
            jobs = [self.engine.read(jid) for jid in new_ids]
            if any(job.get('is_test') for job in jobs):
                raise ValueError('測試素材不能加入批次隊列')
            for jid, job in zip(new_ids, jobs):
                item = dict(job_id=jid, name=job.get('name', jid), mode=mode, state='pending', done=0, total=0, error='', phase='等待批次處理')
                self._stats(item, job)
                self.items.append(item)
            self._persist()
            self.wake.set()
            return dict(self._snapshot(), added=new_ids, skipped=[jid for jid in ids if jid in existing])

    def start(self):
        with self.shared_lock, self.engine.LOCK, self.lock:
            if self.load_error:
                raise ValueError(self.load_error)
            if self.closed.is_set():
                raise ValueError('隊列服務已關閉')
            self.paused = False
            stop = self.engine.STOP.get(self.active_job_id)
            self.resume_after_pause = bool(stop is not None and stop.is_set())
            self.recovered = False
            self.error = ''
            self.phase = '準備依序處理；已有手動工作時會等待'
            self._persist()
            if self.autothread and (self.thread is None or not self.thread.is_alive()):
                self.thread = threading.Thread(target=self._run, name='cutout-batch-queue', daemon=True)
                self.thread.start()
            self.wake.set()
            return self._snapshot()

    def pause(self):
        with self.shared_lock, self.engine.LOCK, self.lock:
            self.paused = True
            self.resume_after_pause = False
            self.waiting_for = None
            if self.active_job_id and self.active_job_id in self.engine.ACTIVE:
                stop = self.engine.STOP.get(self.active_job_id)
                if stop is not None:
                    stop.set()
                self.phase = '正在暫停目前項目；不再派送新任務'
            else:
                self.phase = '批次已暫停；按開始可繼續'
            self._persist()
            self.wake.set()
            return self._snapshot()

    def retry(self, jid, mode=None):
        with self.shared_lock, self.engine.LOCK, self.lock:
            item = self._find(jid)
            if jid in self.engine.ACTIVE or item['state'] == 'running':
                raise ValueError('請先暫停並等待此任務停止')
            if mode is not None and (not isinstance(mode, str) or mode not in MODES):
                raise ValueError('批次模式必須是 full 或 preview')
            self.engine.read(jid)
            item.update(state='pending', mode=mode or item['mode'], error='', phase='等待續跑；保留已完成影格')
            self._persist()
            self.wake.set()
            return self._snapshot()

    def remove(self, jid):
        with self.shared_lock, self.engine.LOCK, self.lock:
            item = self._find(jid)
            if jid in self.engine.ACTIVE or item['state'] == 'running':
                raise ValueError('處理中的項目不能移除，請先暫停並等待停止')
            self.items.remove(item)
            self._persist()
            return self._snapshot()

    def move(self, jid, direction):
        if direction not in {'up', 'down'}:
            raise ValueError('移動方向必須是 up 或 down')
        with self.shared_lock, self.engine.LOCK, self.lock:
            item = self._find(jid)
            if jid in self.engine.ACTIVE or item['state'] == 'running':
                raise ValueError('處理中的項目不能移動')
            index = self.items.index(item)
            target = index + (-1 if direction == 'up' else 1)
            if 0 <= target < len(self.items):
                other = self.items[target]
                if other['job_id'] in self.engine.ACTIVE or other['state'] == 'running':
                    raise ValueError('請保持正在處理的項目位置，改排後續任務')
                self.items[index], self.items[target] = self.items[target], item
                self._persist()
            return self._snapshot()

    def tick(self):
        """Perform one dispatch step; public for deterministic mock-worker tests."""
        with self.shared_lock, self.engine.LOCK, self.lock:
            if self.load_error or self.closed.is_set():
                return
            self._refresh_records()
            if self.active_job_id:
                item = self._find(self.active_job_id)
                job = self.engine.read(self.active_job_id)
                self._stats(item, job)
                item['phase'] = job.get('phase') or item['phase']
                if self.active_job_id in self.engine.ACTIVE:
                    self._persist()
                    return
                state = job.get('state')
                if state in {'complete', 'review', 'paused'}:
                    item.update(state=state, error='')
                    if state == 'paused':
                        if not self.resume_after_pause or self.paused:
                            self.paused = True  # Also honor the ordinary job pause button.
                            self.phase = '批次已暫停；按開始可繼續'
                else:
                    item.update(state='error', error=job.get('error') or '此任務處理中斷，請檢查後重試')
                    item['phase'] = item['error']
                self.active_job_id = None
                self.resume_after_pause = False
                self._persist()
            if self.paused:
                self._persist()
                return
            if self.engine.ACTIVE:
                self.waiting_for = 'manual'
                self.phase = '等待目前手動任務完成；不會打斷正在處理的工作'
                return
            if self.export_busy():
                self.waiting_for = 'export'
                self.phase = '等待修整／多動畫輸出完成'
                return
            self.waiting_for = None
            item = next((item for item in self.items if item['state'] in {'pending', 'paused'}), None)
            if item is None:
                self.paused = True
                self.phase = '本批已處理完；請查看待確認或失敗項目' if any(item['state'] in {'review', 'error'} for item in self.items) else '本批已全部完成'
                self._persist()
                return
            try:
                jid = item['job_id']
                job = self.engine.read(jid)
                self._stats(item, job)
                if job.get('state') == 'complete':
                    item.update(state='complete', error='', phase='此任務已完成，保留既有結果')
                    self._persist()
                    return
                if item['mode'] == 'preview' and job.get('state') == 'review' and job.get('last_mode') == 'preview':
                    item.update(state='review', error='', phase='試跑已完成，等待人工確認')
                    self._persist()
                    return
                if item['mode'] == 'full':
                    self.engine.update(jid, approved=True, skipped_preview=True)
                item.update(state='running', error='', phase='準備開始批次項目')
                self.active_job_id = jid
                self.phase = '正在處理：' + item['name']
                self._persist()  # Persist ownership before the worker can begin.
                self.engine.start(jid, item['mode'])
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                self.active_job_id = None
                item.update(state='error', error=str(exc), phase='無法開始：' + str(exc))
                self._persist()

    def _run(self):
        while not self.closed.is_set():
            try:
                self.tick()
            except Exception as exc:
                with self.lock:
                    self.paused = True
                    self.error = '批次隊列已停止派送：' + str(exc)
                    self.phase = self.error
            self.wake.wait(.5)
            self.wake.clear()

    def close(self):
        try:
            self.pause()
        finally:
            self.closed.set()
            self.wake.set()
            if self.thread and self.thread is not threading.current_thread():
                self.thread.join(timeout=2)


_manager = None
_initialization_lock = threading.Lock()


def initialize():
    global _manager
    with _initialization_lock:
        if _manager is None:
            import engine as e
            import loop_editor as le
            import transitions as tr

            def export_busy():
                with tr.LOCK:
                    return any(run.get('state') == 'running' for run in le.RUNS.values()) or any(run.get('state') == 'running' for run in tr.RUNS.values())

            _manager = QueueManager(e.ROOT / 'batch_queue.json', e, le.LOCK, export_busy)
    return _manager


def status():
    return initialize().snapshot()


def handle(action, data):
    if not isinstance(data, dict):
        raise ValueError('隊列請求內容必須是物件')
    manager = initialize()
    if action == 'add':
        return manager.add(data.get('job_ids'), data.get('mode', 'full'))
    if action == 'start':
        return manager.start()
    if action == 'pause':
        return manager.pause()
    if action == 'retry':
        return manager.retry(data.get('job_id'), data.get('mode'))
    if action == 'remove':
        return manager.remove(data.get('job_id'))
    if action == 'move':
        return manager.move(data.get('job_id'), data.get('direction'))
    raise ValueError('未知隊列操作')


def shutdown():
    if _manager is not None:
        _manager.close()
