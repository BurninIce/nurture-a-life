# -*- coding: utf-8 -*-
"""Live2D 展示台：一个本地小服务，把当前情绪推给浏览器里的模型。

仓库不带模型与运行时。页面从 CDN 取 pixi 与 pixi-live2d-display，模型由
`?model=` 指定，默认用 pixi-live2d-display 仓库里的公开示例模型（Haru）。
Cubism Core 由 Live2D 官方 CDN 提供，使用前请确认其许可。

启动：
    python scripts/live2d_stage.py --port 6798
    python scripts/live2d_stage.py --port 6798 --model <模型 json 的 URL>

给情绪（对话端或手动都行）：
    curl -X POST http://127.0.0.1:6798/mood -H "Content-Type: application/json" \
         -d "{\"mood\": \"戏谑而略带羞涩\"}"

页面上看到的是归并后的 13 档之一，归并规则在 scripts/mood.py。
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mood  # noqa: E402

ROOT = os.path.dirname(HERE)
PAGE = os.path.join(ROOT, 'admin_ui', 'live2d', 'index.html')
DEFAULT_MODEL = ('https://cdn.jsdelivr.net/gh/guansss/pixi-live2d-display@master/'
                 'test/assets/haru/haru_greeter_t03.model3.json')

_STATE = {'mood': '安静', 'raw': '', 'updated': 0.0}
_LOCK = threading.Lock()


def state():
    with _LOCK:
        return dict(_STATE)


def set_mood(text):
    raw = str(text or '').strip()
    key = mood.classify(raw) or '安静'
    with _LOCK:
        _STATE.update({'mood': key, 'raw': raw, 'updated': time.time()})
    return key


class Handler(BaseHTTPRequestHandler):
    server_version = 'Live2DStage/1.0'

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            try:
                with open(PAGE, 'rb') as f:
                    self._send(200, f.read(), 'text/html; charset=utf-8')
            except OSError:
                self._send(404, 'page not found', 'text/plain; charset=utf-8')
            return
        if path == '/state':
            q = parse_qs(urlparse(self.path).query)
            model = (q.get('model') or [DEFAULT_MODEL])[0]
            self._send(200, json.dumps({'state': state(), 'model': model,
                                        'keys': list(mood.MOOD_KEYS)},
                                       ensure_ascii=False))
            return
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != '/mood':
            self._send(404, {'error': 'not found'})
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
        except (ValueError, OSError):
            self._send(400, {'error': 'bad json'})
            return
        key = set_mood((body or {}).get('mood') or (body or {}).get('emotion'))
        self._send(200, {'ok': True, 'mood': key, 'state': state()})


def main(argv=None):
    ap = argparse.ArgumentParser(description='Live2D 展示台')
    ap.add_argument('--port', type=int, default=6798)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--mood', default='', help='启动时先给一个情绪')
    args = ap.parse_args(argv)
    if args.mood:
        set_mood(args.mood)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print('Live2D 展示台：http://%s:%d/' % (args.host, args.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
