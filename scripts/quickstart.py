# -*- coding: utf-8 -*-
"""首次启动向导：收集机器人 QQ / 管理员 QQ / 群号 / 安装目录 / 对话与判官模型。

start.bat 在全新环境（state/setup.json 与 state/runtime_config.json 都不存在）
时先拉起本脚本；浏览器打开 http://127.0.0.1:6795/，保存后本进程退出、
启动链继续跑 Ollama / NapCat / bot。

模型 Key 由本页写入，但不进 setup.json：
    file  → 项目根 qwen.key（明文，已 gitignore）
    env   → 当前用户的持久环境变量（HKCU\\Environment，rp_config 启动时读取）

用法：
    python scripts/quickstart.py              # 首次绑定
    python scripts/quickstart.py --force      # 已有 setup.json 也重新打开
    python scripts/quickstart.py --check      # 只打印绑定与检查项，不起服务
    python scripts/quickstart.py --no-browser # 不自动开浏览器

安全：只监听 127.0.0.1，校验 Host/Origin；除 state/setup.json 外只写
用户选择的 Key 目标；不读取、不回显任何密钥。
"""
import argparse
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import paths  # noqa: E402
import setup_config  # noqa: E402
from win_open import open_url  # noqa: E402

PAGE = os.path.join(paths.ADMIN_UI_DIR, 'quickstart.html')
CSS = os.path.join(paths.ADMIN_UI_DIR, 'ui.css')
MAX_BODY = 64 * 1024


def _state_writable():
    try:
        os.makedirs(paths.STATE_DIR, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=paths.STATE_DIR, prefix='.qs-',
                                         delete=True):
            pass
        return True, paths.STATE_DIR
    except OSError as e:
        return False, '%s（%s）' % (paths.STATE_DIR, e)


def key_path():
    """file 模式的 Key 路径（与 rp_config.QWEN_API_KEY_FILE 同一个文件）。"""
    return os.path.join(paths.ROOT, setup_config.MODEL_KEY_FILE)


def write_key_file(key):
    """明文写 qwen.key：临时文件 + os.replace，权限尽量收紧到 600。"""
    p = key_path()
    tmp = p + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(key.strip() + '\n')
    os.replace(tmp, p)


def save_env_var(name, value):
    """写入当前用户的持久环境变量；rp_config 启动时会从注册表补进进程。"""
    if os.name != 'nt':
        raise OSError('当前系统不支持写入用户环境变量')
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
    os.environ[name] = value


def _key_present(setup=None):
    """是否已有可用于当前配置的模型 Key（只看存在性，不读值）。"""
    spec = setup_config.model_spec(setup if setup is not None
                                   else setup_config.load())
    names = ['Lab-Local-Key', 'QWEN_API_KEY', 'Qwen_API_KEY__TOKEN']
    if spec:
        names.append(spec['key_env'])
    if any(os.environ.get(name) or setup_config.user_env(name) for name in names):
        return True
    return any(os.path.exists(os.path.join(paths.ROOT, name))
               for name in (setup_config.MODEL_KEY_FILE,))


def build_status(setup=None):
    """给页面和 --check 用的快照：当前绑定、模型、环境检查、端口、默认路径。"""
    setup = setup if setup is not None else setup_config.load()
    napcat_home = setup.get('napcat_home') or paths.NAPCAT_HOME
    ollama_home = setup.get('ollama_home') or paths.OLLAMA_HOME
    spec = setup_config.model_spec(setup)
    checks = []

    def add(key, label, ok, detail, required=True):
        checks.append({'key': key, 'label': label, 'ok': bool(ok),
                       'detail': detail, 'required': required})

    add('python', 'Python 运行环境', os.path.exists(paths.VENV_PY), paths.VENV_PY)
    napcat_exe = os.path.join(napcat_home, 'NapCatWinBootMain.exe')
    add('napcat', 'NapCat', os.path.exists(napcat_exe),
        napcat_exe if napcat_home else '未填写 NapCat Shell 目录')
    ollama_exe = os.path.join(ollama_home, 'ollama.exe')
    add('ollama', 'Ollama（可选）', os.path.exists(ollama_exe),
        ollama_exe if ollama_home else '未填写（只用云端 API 时可留空）',
        required=False)
    ok, detail = _state_writable()
    add('state', 'state 目录可写', ok, detail)
    key_ok = _key_present(setup)
    if not spec:
        model_detail = '未配置：填好下面一节，或先启动后到管理面板配置'
    elif not key_ok:
        model_detail = '%s · %s / %s · 还没填 Key' % (
            spec['label'], spec['gen_model'], spec['judge_model'])
    else:
        model_detail = '%s · %s / %s' % (
            spec['label'], spec['gen_model'], spec['judge_model'])
    add('model', '对话与判官模型', bool(spec) and key_ok,
        model_detail, required=False)
    return {
        'configured': setup_config.exists(),
        'setup': setup,
        'model': spec,
        'model_key_present': _key_present(setup),
        'model_presets': setup_config.presets_payload(),
        'suggested': {
            'napcat_home': paths.NAPCAT_HOME,
            'ollama_home': paths.OLLAMA_HOME,
        },
        'checks': checks,
        'ports': {
            'bridge_ws': paths.PORT_BRIDGE_WS,
            'admin': paths.PORT_ADMIN,
            'napcat_ui': paths.PORT_NAPCAT_UI,
        },
        'setup_file': setup_config.setup_path(),
    }


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler):
        super().__init__(addr, handler)
        self.saved = None
        self.setup = setup_config.load()


class _Handler(BaseHTTPRequestHandler):
    server_version = 'TokisakiQuickstart/1'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    # ---- 基础响应 ----
    def _send(self, code, body, ctype):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   'application/json; charset=utf-8')

    def _origin(self):
        return 'http://%s:%d' % ('127.0.0.1', self.server.server_port)

    def _host_ok(self):
        host = (self.headers.get('Host') or '').strip().lower()
        return host in ('127.0.0.1:%d' % self.server.server_port,
                        'localhost:%d' % self.server.server_port)

    def _origin_ok(self):
        origin = (self.headers.get('Origin') or '').strip().lower()
        return not origin or origin in (self._origin(), 'http://localhost:%d'
                                        % self.server.server_port)

    def _file(self, path, ctype):
        try:
            with open(path, 'rb') as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._json(500, {'ok': False, 'error': '页面文件缺失：%s' % path})

    # ---- 路由 ----
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {'ok': False, 'error': 'forbidden host'})
        route = urlparse(self.path).path
        if route in ('/', '/index.html'):
            return self._file(PAGE, 'text/html; charset=utf-8')
        if route == '/ui.css':
            return self._file(CSS, 'text/css; charset=utf-8')
        if route == '/api/status':
            return self._json(200, build_status(self.server.setup))
        self._json(404, {'ok': False, 'error': 'not found'})

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._json(403, {'ok': False, 'error': 'forbidden origin'})
        if urlparse(self.path).path != '/api/save':
            return self._json(404, {'ok': False, 'error': 'not found'})
        ctype = (self.headers.get('Content-Type') or '').lower()
        if not ctype.startswith('application/json'):
            return self._json(415, {'ok': False, 'error': '需要 application/json'})
        try:
            size = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            size = -1
        if size <= 0 or size > MAX_BODY:
            return self._json(400, {'ok': False, 'error': '请求体为空或过大'})
        try:
            data = json.loads(self.rfile.read(size).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {'ok': False, 'error': 'JSON 解析失败'})
        if not isinstance(data, dict):
            return self._json(400, {'ok': False, 'error': '请求体需要是对象'})
        # 密钥只用于本次落盘，不写进 setup.json
        key = str(data.pop('model_key', '') or '').strip()
        errors = setup_config.validate(data)
        key_err = setup_config.key_error(key)
        if key_err:
            errors.append(('model_key', key_err))
        if errors:
            return self._json(400, {
                'ok': False,
                'errors': [{'field': f, 'message': m} for f, m in errors],
            })
        spec = setup_config.model_spec(data)
        if key and not spec:
            return self._json(400, {
                'ok': False,
                'errors': [{'field': 'model_key',
                            'message': '先选择服务商并填好端点，再填 Key'}],
            })
        try:
            if key and spec:
                if spec['key_mode'] == setup_config.MODEL_KEY_MODE_ENV:
                    save_env_var(spec['key_env'], key)
                else:
                    write_key_file(key)
            setup_config.save(data)
        except (OSError, ImportError, ValueError) as e:
            return self._json(500, {'ok': False, 'error': '写入失败：%s' % e})
        saved = setup_config.load()
        self.server.saved = saved
        self.server.setup = saved
        self._json(200, {'ok': True, 'setup': saved})
        # 先把响应发完，再退出 serve_forever，让 start.bat 继续启动
        threading.Timer(0.8, self.server.shutdown).start()


def main(argv=None):
    ap = argparse.ArgumentParser(description='Tokisaki 首次启动向导')
    ap.add_argument('--port', type=int, default=paths.PORT_QUICKSTART,
                    help='本地端口（默认 %d）' % paths.PORT_QUICKSTART)
    ap.add_argument('--force', action='store_true',
                    help='已有 setup.json 也重新打开（改绑定用）')
    ap.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    ap.add_argument('--timeout', type=int, default=900,
                    help='无人操作时等待秒数（0=一直等；默认 900）')
    ap.add_argument('--check', action='store_true',
                    help='只打印当前绑定与环境检查，不起服务')
    args = ap.parse_args(argv)

    if args.check:
        print(json.dumps(build_status(), ensure_ascii=False, indent=2))
        return 0

    if not args.force and setup_config.exists():
        print('已存在绑定：%s' % setup_config.setup_path())
        print('如需重新填写：python scripts/quickstart.py --force')
        return 0

    url = 'http://127.0.0.1:%d/' % args.port
    try:
        srv = _Server(('127.0.0.1', args.port), _Handler)
    except OSError as e:
        print('[!!] 端口 %d 无法监听：%s' % (args.port, e))
        print('     换个端口：python scripts/quickstart.py --port 6796')
        return 2

    print('=' * 60)
    print('  Tokisaki 首次启动向导')
    print('=' * 60)
    print('  在浏览器里填写绑定信息：%s' % url)
    print('  保存后本窗口会自动继续启动整套环境。')
    if args.timeout:
        print('  （%d 秒内无操作会自动退出）' % args.timeout)
    print('=' * 60)
    if not args.no_browser:
        open_url(url)

    timer = None
    if args.timeout:
        timer = threading.Timer(args.timeout, srv.shutdown)
        timer.daemon = True
        timer.start()
    try:
        srv.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print('\n[--] 已取消。')
    finally:
        if timer is not None:
            timer.cancel()
        srv.server_close()

    if srv.saved:
        print('[OK] 绑定已保存：%s' % setup_config.setup_path())
        print('     接下来启动 Ollama / NapCat / bot；'
              'NapCat 出现二维码时用机器人 QQ 扫描。')
        return 0
    print('[--] 未保存绑定，启动流程中止。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
