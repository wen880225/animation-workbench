import json
import copy
import hashlib
import mimetypes
import os
from pathlib import Path
import secrets
import re
import sys
import threading
import urllib.parse
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import engine as e
import loop_editor as le
import transitions as tr
import queue_manager as qm

TOKEN = secrets.token_hex(24)
PORT = 8766
CREATE_LOCK = threading.Lock()
CREATE_META = '_create_request'


def create_job(data):
    """Recover a committed creation after a lost HTTP response, without re-probing."""
    request_id = data.get('request_id')
    if request_id is None:
        return e.create(data['source'], data.get('config', {}))
    if not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id):
        raise ValueError('建立請求 request_id 必須是 32 位小寫十六進位 ID')
    source, config = data.get('source'), data.get('config', {})
    if not isinstance(source, str) or not source or not isinstance(config, dict):
        raise ValueError('請提供影片來源路徑與設定物件')
    if CREATE_META in config:
        raise ValueError('設定包含保留欄位')
    source_key = os.path.normcase(str(Path(source).resolve()))
    canonical = json.dumps(dict(source=source_key, config=config), sort_keys=True,
                           ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    fingerprint = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    # Do not hold e.LOCK over ffprobe or filesystem setup.
    with CREATE_LOCK:
        found = []
        for path in e.JOBS.glob('*/job.json'):
            try:
                job = json.loads(path.read_text(encoding='utf-8-sig'))
                metadata = job.get('config', {}).get(CREATE_META, {})
                if isinstance(metadata, dict) and metadata.get('id') == request_id:
                    found.append(job)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        if found:
            if len(found) != 1 or found[0]['config'][CREATE_META].get('fingerprint') != fingerprint:
                raise ValueError('此建立請求 ID 已用於不同來源或設定，請勿重複使用')
            return found[0]
        settings = copy.deepcopy(config)
        settings[CREATE_META] = dict(id=request_id, fingerprint=fingerprint)
        return e.create(source, settings)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send(self, data, status=200, mime='application/json'):
        if not isinstance(data, bytes): data=json.dumps(data,ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers(); self.wfile.write(data)
    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{PORT}',f'localhost:{PORT}')
    def stream_file(self, f):
        size=f.stat().st_size; start=0; end=size-1
        value=self.headers.get('Range')
        if value:
            import re
            m=re.fullmatch(r'bytes=(\d*)-(\d*)',value)
            if not m or not any(m.groups()): raise ValueError('無效 Range')
            a,b=m.groups()
            if a:
                start=int(a); end=min(int(b),end) if b else end
            else: start=max(0,size-int(b))
            if start>end or start>=size:
                self.send_response(416); self.send_header('Content-Range',f'bytes */{size}');self.end_headers();return
        self.send_response(206 if value else 200)
        self.send_header('Content-Type',mimetypes.guess_type(f.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(end-start+1))
        self.send_header('Accept-Ranges','bytes');self.send_header('Cache-Control','no-store')
        if value:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        with f.open('rb') as source:
            source.seek(start);left=end-start+1
            while left:
                chunk=source.read(min(left,1024*1024))
                if not chunk:break
                self.wfile.write(chunk);left-=len(chunk)
    def do_GET(self):
        try:
            if not self.valid_host(): return self.send({'error':'Host rejected'},403)
            p=urllib.parse.urlsplit(self.path); q=urllib.parse.parse_qs(p.query)
            if p.path=='/':
                data=(e.ROOT/'ui.html').read_text(encoding='utf-8').replace('__TOKEN__',TOKEN)
                data=data.replace('__TRANSITIONS_PANEL__',(e.ROOT/'transitions_panel.html').read_text(encoding='utf-8'))
                return self.send(data.encode(),mime='text/html; charset=utf-8')
            if p.path=='/loop.js': return self.send((e.ROOT/'loop.js').read_bytes(),mime='text/javascript; charset=utf-8')
            if p.path=='/app.js': return self.send((e.ROOT/'app.js').read_bytes(),mime='text/javascript; charset=utf-8')
            if p.path=='/workspace.js': return self.send((e.ROOT/'workspace.js').read_bytes(),mime='text/javascript; charset=utf-8')
            if p.path=='/workspace.css': return self.send((e.ROOT/'workspace.css').read_bytes(),mime='text/css; charset=utf-8')
            if p.path=='/transitions.js': return self.send((e.ROOT/'transitions.js').read_bytes(),mime='text/javascript; charset=utf-8')
            if p.path=='/transitions.css': return self.send((e.ROOT/'transitions.css').read_bytes(),mime='text/css; charset=utf-8')
            if p.path=='/beginner.js': return self.send((e.ROOT/'beginner.js').read_bytes(),mime='text/javascript; charset=utf-8')
            if p.path=='/beginner.css': return self.send((e.ROOT/'beginner.css').read_bytes(),mime='text/css; charset=utf-8')
            if p.path=='/queue.js': return self.send((e.ROOT/'queue.js').read_bytes(),mime='text/javascript; charset=utf-8')
            if p.path=='/region.js': return self.send((e.ROOT/'region.js').read_bytes(),mime='text/javascript; charset=utf-8')
            if p.path=='/region.css': return self.send((e.ROOT/'region.css').read_bytes(),mime='text/css; charset=utf-8')
            if p.path=='/api/version': return self.send({'version':'3.9','workspace':3,'features':{'transitions':1,'transition_tone':1,'transition_sequence':2,'transition_seams':1,'queue':1,'region':3,'alignment':1,'closure':1}})
            if p.path=='/api/queue': return self.send(qm.status())
            if p.path=='/api/jobs':
                jobs=[json.loads(x.read_text(encoding='utf-8')) for x in e.JOBS.glob('*/job.json')]
                jobs=[j for j in jobs if not j.get('is_test')]
                return self.send(sorted(jobs,key=lambda j:j['created'],reverse=True))
            if p.path=='/api/check': return self.send(e.preflight(q.get('url',['http://127.0.0.1:8188'])[0]))
            if p.path.startswith('/transition-file/'):
                parts=p.path.split('/');root=tr.projectdir(parts[2]).resolve()
                f=(root/urllib.parse.unquote('/'.join(parts[3:]))).resolve()
                if not f.is_relative_to(root) or not f.is_file(): raise ValueError('找不到銜接專案檔案')
                return self.stream_file(f)
            if p.path.startswith('/file/'):
                parts=p.path.split('/'); root=e.jobdir(parts[2]).resolve()
                f=(root/urllib.parse.unquote('/'.join(parts[3:]))).resolve()
                if not f.is_relative_to(root) or not f.is_file(): raise ValueError('找不到檔案')
                return self.stream_file(f)
            self.send({'error':'Not found'},404)
        except Exception as ex: self.send({'error':str(ex)},400)
    def do_POST(self):
        try:
            if not self.valid_host() or self.headers.get('X-Token')!=TOKEN:
                return self.send({'error':'請重新整理工具頁面'},403)
            p=urllib.parse.urlsplit(self.path)
            n=int(self.headers.get('Content-Length','0'))
            if p.path=='/api/upload':
                q=urllib.parse.parse_qs(p.query); name=Path(q.get('name',['input.mp4'])[0]).name
                suffix=Path(name).suffix.lower()
                if suffix not in ('.mp4','.mov','.mkv','.webm','.avi','.m4v','.ogv','.gif'): raise ValueError('不支援此副檔名')
                if n<=0 or n>e.shutil.disk_usage(e.ROOT).free-512*1024**2: raise ValueError('檔案太大或磁碟空間不足')
                folder=e.unique_folder(e.ROOT/'uploads',Path(name).stem)
                target=folder/e.safe_name(name)
                try:
                    with target.open('wb') as out:
                        left=n
                        while left:
                            chunk=self.rfile.read(min(left,1024*1024))
                            if not chunk: raise ValueError('上傳中斷')
                            out.write(chunk); left-=len(chunk)
                    return self.send({'path':str(target),'info':e.probe(target),'name':name})
                except Exception:
                    target.unlink(missing_ok=True); raise
            if n>1024*1024: raise ValueError('請求過大')
            data=json.loads(self.rfile.read(n) or b'{}'); jid=data.get('id')
            if p.path=='/api/probe': return self.send(e.probe(data['source']))
            if p.path=='/api/create': return self.send(create_job(data))
            if p.path.startswith('/api/queue/'):
                return self.send(qm.handle(p.path.rsplit('/',1)[-1],data))
            if p.path.startswith('/api/transitions/'):
                return self.send(tr.handle(p.path.rsplit('/',1)[-1],data))
            if p.path.startswith('/api/loop/'):
                if p.path.endswith('/export'):
                    with le.LOCK:
                        if any(v.get('state')=='running' for v in tr.RUNS.values()): raise ValueError('請等待多動畫輸出完成')
                        return self.send(le.handle('export',e.read(jid),data))
                return self.send(le.handle(p.path.rsplit('/',1)[-1],e.read(jid),data))
            if p.path in ('/api/start','/api/direct','/api/preview','/api/approve','/api/retry','/api/atlas'):
                with le.LOCK:
                    if any(v.get('state')=='running' for v in le.RUNS.values()) or any(v.get('state')=='running' for v in tr.RUNS.values()):
                        raise ValueError('請等待循環修整匯出完成，再啟動其他處理')
            with le.LOCK,e.LOCK:
                if p.path in ('/api/start','/api/direct','/api/preview','/api/approve','/api/retry','/api/atlas') and (any(v.get('state')=='running' for v in le.RUNS.values()) or any(v.get('state')=='running' for v in tr.RUNS.values())):
                    raise ValueError('請等待修整或多動畫輸出完成，再啟動其他處理')
                j=e.read(jid)
                if p.path=='/api/start':
                    mode=data.get('mode',j.get('last_mode','preview'))
                    if mode not in ('preview','full','single','export'): raise ValueError('模式錯誤')
                    e.start(jid,mode)
                elif p.path=='/api/direct':
                    if e.ACTIVE: raise ValueError('請先暫停目前任務')
                    e.update(jid,approved=True,skipped_preview=True)
                    e.start(jid,'full')
                elif p.path=='/api/preview':
                    if e.ACTIVE: raise ValueError('請先暫停目前任務')
                    count=int(data.get('frames',48))
                    if not 1<=count<=10000: raise ValueError('試跑幀數必須介於 1–10000')
                    config=dict(j['config'],preview_frames=count)
                    e.update(jid,config=config,preview_indices=list(range(min(count,len(j['frames'])))),approved=False)
                    e.start(jid,'preview')
                elif p.path=='/api/pause':
                    if jid in e.STOP: e.STOP[jid].set()
                elif p.path=='/api/approve':
                    if jid in e.ACTIVE or not j['preview_indices'] or any(j['frames'][i]['status']!='done' for i in j['preview_indices']):
                        raise ValueError('請先完成試跑')
                    e.update(jid,approved=True); e.start(jid,'full')
                elif p.path=='/api/retry':
                    if e.ACTIVE: raise ValueError('請先暫停並等待處理停止')
                    idx=int(data['index'])
                    if not 0<=idx<len(j['frames']): raise ValueError('幀編號錯誤')
                    f=j['frames'][idx]
                    queue=e.request(j['config']['comfy']+'/queue')
                    ids=[v[1] for key in ('queue_running','queue_pending') for v in queue.get(key,[])]
                    if f.get('prompt_id') in ids: raise ValueError('該幀仍在 ComfyUI 執行，請稍後繼續')
                    e.change_frame(jid,idx,status='pending',prompt_id=None,error='')
                    e.update(jid,approved=False,state='paused',exports=[],retry_index=idx,phase='準備重跑指定幀')
                    e.start(jid,'single')
                elif p.path=='/api/atlas': e.spritesheet(jid)
                elif p.path=='/api/open':
                    folder=data.get('folder','job')
                    if folder not in ('job','png'): raise ValueError('未知資料夾')
                    target=e.directory(j,'png') if folder=='png' else e.jobdir(jid)
                    os.startfile(str(target))
                else: raise ValueError('未知操作')
            self.send({'ok':True})
        except Exception as ex: self.send({'error':str(ex)},400)

if __name__=='__main__':
    if '--port' in sys.argv: PORT=int(sys.argv[sys.argv.index('--port')+1])
    e.recover()
    qm.initialize()
    server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    print(f'Animation Cutout: http://127.0.0.1:{PORT}',flush=True)
    if '--no-browser' not in sys.argv:
        threading.Timer(.7,lambda:webbrowser.open(f'http://127.0.0.1:{PORT}')).start()
    try:
        server.serve_forever()
    finally:
        qm.shutdown()
        server.server_close()
