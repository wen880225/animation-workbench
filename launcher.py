"""Launch the loop-capable workbench, or reuse its existing instance."""
import argparse
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
import launcher_restart as restart

ROOT = Path(__file__).resolve().parent
PORT = 8767
EXPECTED_VERSION = '3.12.0'
URL = f'http://127.0.0.1:{PORT}'

def ready():
    try:
        with urllib.request.urlopen(URL, timeout=2) as response:
            html = response.read().decode('utf-8')
        token = re.search(r"window.APP_TOKEN='([^']+)'", html)
        if not token or 'loop.js' not in html:
            return False
        with urllib.request.urlopen(URL+'/api/version', timeout=2) as response:
            info=json.load(response)
            if info.get('version') != EXPECTED_VERSION or info.get('workspace') != 3 or any(info.get('features',{}).get(name) != expected for name,expected in (('transitions',1),('transition_tone',1),('transition_sequence',2),('transition_seams',2),('boundary_routes',1),('finish_plan',3),('candidate_playback',1),('seam_repair',2),('queue',1),('region',3),('alignment',1),('closure',1))):
                return False
        for asset in ('workspace.js','workspace.css','transitions.js','transitions.css','queue.js','region.js','region.css','beginner.js','beginner.css','boundary_workspace.js','seam_repair.js'):
            with urllib.request.urlopen(URL+'/'+asset, timeout=2) as response:
                if not response.read():
                    return False
        with urllib.request.urlopen(URL+'/loop.js', timeout=2) as response:
            if 'loopRefresh' not in response.read().decode('utf-8'):
                return False
        with urllib.request.urlopen(URL+'/api/jobs', timeout=2) as response:
            jobs = json.load(response)
        with urllib.request.urlopen(URL+'/api/queue', timeout=2) as response:
            queue = json.load(response)
            if not isinstance(queue.get('items'), list) or not isinstance(queue.get('paused'), bool):
                return False
        if jobs:
            req = urllib.request.Request(URL+'/api/loop/status',
                data=json.dumps({'id':jobs[0]['id']}).encode(),
                headers={'Content-Type':'application/json','X-Token':token.group(1)})
            with urllib.request.urlopen(req, timeout=2) as response:
                if not isinstance(json.load(response), dict):
                    return False
        return True
    except Exception:
        return False

def main(argv=None):
    parser = argparse.ArgumentParser(description='啟動或明確重新啟動動畫工作台')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--diagnose', action='store_true', help='只檢查版本、程序身分與工作狀態，不開啟或停止服務')
    parser.add_argument('--restart', action='store_true', help='確認本資料夾服務閒置後重新啟動')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    if args.diagnose:
        result = restart.diagnose(ROOT, PORT, URL, sys.executable)
        print('目前服務版本：' + str(result['version'] or '無法確認') + '；本資料夾版本：' + EXPECTED_VERSION)
        print('已確認本資料夾 PID：' + str(result['matched_pid'] or '無法確認'))
        for reason in result['reasons']:
            print('不可重新啟動：' + reason)
        if not result['reasons']:
            print('已確認所有已登記任務閒置；本次只檢查，未停止或開啟服務。')
        return 1 if result['reasons'] else 0
    if args.check:
        current = ready()
        print(('READY ' if current else 'NOT READY ') + URL)
        return 0 if current else 1
    current = ready()
    occupied = restart.port_open(PORT)
    if args.restart:
        print('重新啟動模式：請先儲存草稿；不要同時上傳、對位或校準。')
        if occupied:
            try:
                restart.restart_existing(ROOT, PORT, URL, sys.executable)
            except restart.RestartRefused as error:
                print('未重新啟動：' + str(error))
                return 1
        current = False
    elif not current and occupied:
        service = restart.inspect_service(URL)
        if service['recognized']:
            print('目前背景工作台版本：' + str(service['version']) + '；本資料夾版本：' + EXPECTED_VERSION + '。')
            print('關閉網頁不會關閉背景服務。請先儲存並暫停隊列，再執行「重新啟動工作台.cmd」。')
            if service['version'] == EXPECTED_VERSION:
                print('版本相同，但服務未完整就緒；重新啟動工具會先檢查是否可安全重啟。')
        else:
            print(str(PORT) + ' 被未知服務使用，未停止任何程序。請使用 --diagnose 查看原因。')
        return 1
    if not current:
        if restart.port_open(PORT):
            print('連接埠已被其他服務占用，未啟動第二個服務。請重新檢查。')
            return 1
        try:
            with (ROOT/'launcher_server.log').open('ab') as log:
                process = subprocess.Popen([sys.executable,str(ROOT/'server.py'),'--no-browser','--port',str(PORT)],
                    cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except OSError as error:
            print('工作台啟動失敗，請查看 launcher_server.log：' + str(error))
            return 1
        for _ in range(40):
            if ready():
                break
            if process.poll() is not None:
                print('工作台啟動失敗，請查看 launcher_server.log。')
                return 1
            time.sleep(.25)
        else:
            print('工作台尚未就緒，請查看 launcher_server.log，稍後再次啟動。')
            return 1
    print('動畫去背工作台 v' + EXPECTED_VERSION + ' 已就緒：' + URL)
    if not args.no_browser:
        webbrowser.open(URL)
    return 0

if __name__=='__main__':
    sys.exit(main())
