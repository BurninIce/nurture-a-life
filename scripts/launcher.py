# -*- coding: utf-8 -*-
"""NAL 一键启动编排器。

由 start.bat 调用，作为「总控台」按序拉起整套环境：
    Ollama -> NapCat(QQ) -> bot 桥接 -> 进程守护 -> 管理面板 / NapCat UI

设计要点：
  - 本脚本只做「编排 + 状态汇报」，不阻塞具体业务（各环节用独立窗口常驻）。
  - 所有子进程用 `start`（独立窗口）拉起，脱离本脚本的进程树，避免被回收。
  - 每个环节先探测「是否已在运行」，已在运行则跳过，天然幂等。
  - 端口/路径/QQ 号集中在此处，方便改。

用法：
    python scripts/launcher.py [--no-admin-ui] [--no-napcat-ui]
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import lifecycle  # noqa: E402  （启动阶段/事件，面板可读）
import paths  # noqa: E402  （路径/端口的唯一来源，见 scripts/paths.py）
from win_open import open_url  # noqa: E402  （默认浏览器打开，不额外弹资源管理器）

ROOT = paths.ROOT
PYEXE = paths.VENV_PY

# ---- 各环节 ----
OLLAMA_EXE = paths.OLLAMA_EXE
NAPCAT_DIR = paths.NAPCAT_HOME
NAPCAT_EXE = paths.NAPCAT_EXE
BRIDGE_SCRIPT = os.path.join(HERE, 'qq_bridge.py')
SUPERVISOR_SCRIPT = os.path.join(HERE, 'supervisor.py')
SUPERVISOR_STATE = os.path.join(paths.STATE_DIR, 'supervisor.json')
# 干净的启动 bat（避免 launcher 里拼 cmd /k 嵌套引号触发 start 误解析）
RUN_NAPCAT_BAT = paths.RUN_NAPCAT_BAT
RUN_BOT_BAT = paths.RUN_BOT_BAT

# ---- 端口 ----
PORT_OLLAMA = paths.PORT_OLLAMA
PORT_NAPCAT_UI = paths.PORT_NAPCAT_UI
PORT_BRIDGE_WS = paths.PORT_BRIDGE_WS
PORT_ADMIN = paths.PORT_ADMIN

# ---- 身份（与 rp_config.py 保持一致；留空则 bot 走配置默认）----
QQ_SELF_ID = paths.QQ_SELF_ID
GROUP_ID = paths.GROUP_ID
ADMIN_ID = paths.ADMIN_ID

def log(msg):
    print(msg, flush=True)


def _run_step(key, fn, optional=False):
    """跑一个启动环节并上报阶段状态（launcher 同时会写 state/launch_snapshot.json）。"""
    with lifecycle.stage(key) as st:
        ok = fn()
        if ok:
            st.ok('就绪')
        elif optional:
            st.skip('未安装/未启用，已跳过')
        else:
            st.fail('未就绪', '查看本窗口日志或管理面板「启动阶段」')
        return ok


def port_open(port, host='127.0.0.1'):
    """探测端口是否已有进程监听（含 IPv6 回环）。"""
    import socket
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(0.4)
            if s.connect_ex((host, port)) == 0:
                s.close()
                return True
            s.close()
        except OSError:
            pass
    return False


def wait_port(port, timeout=30.0, host='127.0.0.1'):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port, host):
            return True
        time.sleep(0.3)
    return False


def start_detached(command_line, cwd=None, title=None, minimized=False):
    """用 `start` 独立窗口拉起一个进程，脱离本脚本进程树。

    command_line: 完整命令行字符串（不含 `start`），最稳定的是
        `"C:\\path\\to\\file.bat"` 或 `"C:\\path\\to\\prog.exe" arg1 arg2`
        ——即「可执行文件用双引号包住 + 平铺参数」，不要内嵌 `cmd /k "..."` 嵌套引号。

    关键教训（本机实测）：`start "title" cmd /k "chcp ... && xxx.exe"` 这种
    内嵌引号的写法，会让 start 把后续内容误解析成「要打开的路径/文档」，
    结果弹出一个资源管理器窗口（如打开 C:\\NapCat 目录）而什么都不运行。
    所以这里一律只 start 一个「带引号的 exe/bat 路径 + 平铺参数」。

    返回 True/False 表示是否成功发出启动指令（不代表进程长期存活）。
    """
    title_arg = ('"%s" ' % title) if title else '"" '
    if minimized:
        title_arg = '/min ' + title_arg
    full = 'start %s%s' % (title_arg, command_line)
    try:
        # 关键：必须 shell=True。用 shell=False + ['cmd','/c',full] 时，
        # Python 会把 full 当单个 argv 再包一层引号，与 full 内部引号冲突，
        # 导致 start 解析失败、fallback 打开目录（如 C:\NapCat）。
        subprocess.Popen(
            full,
            cwd=cwd or ROOT,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        log('[!!] 拉起失败：%s' % e)
        return False


def ensure_ollama():
    log('\n[1/5] Ollama ...')
    if port_open(PORT_OLLAMA):
        log('      [OK] 已在运行 (127.0.0.1:%d)' % PORT_OLLAMA)
        return True
    log('      [..] 未运行，正在启动 ...')
    if not os.path.exists(OLLAMA_EXE):
        log('      [!!] 找不到 %s' % OLLAMA_EXE)
        log('           要装到本机就运行：python scripts\\install_ollama.py')
        return False
    start_detached('"%s" serve' % OLLAMA_EXE, title='Ollama', minimized=True)
    if wait_port(PORT_OLLAMA, timeout=30):
        log('      [OK] Ollama 已就绪')
        return True
    log('      [!!] Ollama 30 秒内未就绪')
    return False


def ensure_napcat():
    log('\n[2/5] NapCat (QQ %s) ...' % QQ_SELF_ID)
    # 探测：WebUI 端口起来了就认为 NapCat 在跑
    if port_open(PORT_NAPCAT_UI):
        log('      [OK] 已在运行 (WebUI :%d)' % PORT_NAPCAT_UI)
        return True
    if not os.path.exists(NAPCAT_EXE):
        log('      [!!] 找不到 %s' % NAPCAT_EXE)
        return False
    log('      [..] 未运行，正在启动（快速登录 %s，失效再扫码）...' % QQ_SELF_ID)
    # 直接 start 干净的 bat（内部 chcp 65001 + powershell UTF-8 + -q 快速登录）。
    # 不用 cmd /k 嵌套引号——那会让 start 误解析成打开目录。
    start_detached('"%s"' % RUN_NAPCAT_BAT, cwd=NAPCAT_DIR, title='NapCat')
    log('      [..] 等待 NapCat WebUI 就绪 ...')
    if wait_port(PORT_NAPCAT_UI, timeout=40):
        log('      [OK] NapCat 已就绪 (WebUI :%d)' % PORT_NAPCAT_UI)
        return True
    log('      [!!] NapCat 40 秒内 WebUI 未就绪，可能仍需扫码')
    return False


def ensure_bridge():
    log('\n[3/5] QQ 桥接 (bot) ...')
    if port_open(PORT_BRIDGE_WS):
        log('      [OK] 已在运行 (WS :%d)' % PORT_BRIDGE_WS)
        return True
    if not os.path.exists(PYEXE):
        log('      [!!] 找不到 Python：%s' % PYEXE)
        return False
    log('      [..] 未运行，正在启动 ...')
    # 直接 start 干净的 bat（内部已含 chcp 65001 + 完整 python 命令）
    start_detached('"%s"' % RUN_BOT_BAT, cwd=ROOT, title='CharacterBot')
    log('      [..] 等待桥接 WS 就绪 ...')
    if wait_port(PORT_BRIDGE_WS, timeout=30):
        log('      [OK] 桥接已就绪 (WS :%d)' % PORT_BRIDGE_WS)
        return True
    log('      [!!] 桥接 30 秒内未就绪（查看 CharacterBot 窗口日志）')
    return False


def supervisor_alive(max_age=35.0):
    """守护进程是否还活着：状态文件在持续更新即视为存活。"""
    try:
        with open(SUPERVISOR_STATE, encoding='utf-8') as f:
            ts = float(json.load(f).get('ts') or 0)
        return time.time() - ts <= max_age
    except (OSError, ValueError, TypeError):
        return False


def ensure_supervisor():
    """独立守护窗口：桥接进程退出后自动拉起；端口在但心跳停只告警。"""
    log('\n[4/5] 进程守护 ...')
    if supervisor_alive():
        log('      [OK] 已在运行')
        return True
    if not os.path.exists(SUPERVISOR_SCRIPT):
        log('      [!!] 找不到 %s' % SUPERVISOR_SCRIPT)
        return False
    log('      [..] 未运行，正在启动 ...')
    start_detached('"%s" "%s"' % (PYEXE, SUPERVISOR_SCRIPT), cwd=ROOT,
                   title='NAL Watchdog', minimized=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        if supervisor_alive(15):
            log('      [OK] 守护已就绪（桥接退出后自动重启）')
            return True
        time.sleep(0.3)
    log('      [!!] 守护 15 秒内未写出状态（查看 NAL Watchdog 窗口）')
    return False


def _admin_token():
    """管理 token：与 admin_server.get_token 同优先级（环境变量 > state/admin_token）。

    不直接 import admin_server：它会在导入时探测显存，而这里只需要读一个文件。
    """
    tok = (os.environ.get('NAL_ADMIN_TOKEN') or '').strip()
    if tok:
        return tok
    try:
        with open(os.path.join(ROOT, 'state', 'admin_token'),
                  encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def admin_url(port=None):
    """面板 URL。带 token，否则页面能开但所有 API 都 403（表现为面板空白）。"""
    from urllib.parse import quote
    url = 'http://127.0.0.1:%d' % (port or PORT_ADMIN)
    tok = _admin_token()
    if tok:
        url += '/?token=' + quote(tok, safe='')
    return url


def ensure_admin_ui(open_flag=True):
    log('\n[5/5] 管理面板 ...')
    if not port_open(PORT_ADMIN):
        log('      [..] 等待管理面板后端就绪 ...')
        if not wait_port(PORT_ADMIN, timeout=20):
            log('      [!!] 管理面板后端 20 秒内未就绪')
            return False
    log('      [OK] 管理面板后端就绪 (http://127.0.0.1:%d)' % PORT_ADMIN)
    if open_flag:
        # bot 已自带 auto_open_admin 逻辑，这里做兜底：若已打开则无需重复
        # （打开同一 URL 会聚焦已有标签，不会多开窗口，可安全重复调用）
        open_url(admin_url())
        log('      [OK] 已请求打开管理面板')
    return True


def ensure_napcat_ui(open_flag=True):
    if not open_flag:
        return False
    if not port_open(PORT_NAPCAT_UI):
        log('      [..] NapCat UI 尚未就绪，跳过自动打开')
        return False
    token = read_napcat_token()
    url = 'http://127.0.0.1:%d/webui?token=%s' % (PORT_NAPCAT_UI, token)
    open_url(url)
    log('      [OK] 已请求打开 NapCat UI（扫码登录）')
    return True


def read_napcat_token():
    """从 NapCat 的 webui.json 读 token；版本目录不写死，取最新一份。"""
    import glob
    pattern = os.path.join(NAPCAT_DIR, 'versions', '*', 'resources', 'app',
                           'napcat', 'config', 'webui.json')
    candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    for p in candidates:
        try:
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f).get('token', 'a3267e145d37')
        except (OSError, ValueError):
            continue
    return 'a3267e145d37'


def main(argv=None):
    ap = argparse.ArgumentParser(description='NAL 一键启动编排器')
    ap.add_argument('--no-admin-ui', action='store_true', help='不自动打开管理面板')
    ap.add_argument('--no-napcat-ui', action='store_true', help='不自动打开 NapCat UI')
    ap.add_argument('--no-supervisor', action='store_true',
                    help='不启动进程守护（桥接退出后不会自动重启）')
    args = ap.parse_args(argv)

    log('=' * 60)
    log('  NAL 一键启动 · 总控台')
    log('=' * 60)

    lifecycle.configure('launcher', lifecycle.LAUNCHER_STAGES)

    # 解析结果写回环境变量：.bat 只用 %NAL_*%，不再各存一份路径
    paths.export_env()
    with lifecycle.stage('preflight') as st:
        missing = [name for name, path, exists in paths.describe()
                   if not exists and name in ('Python', 'Ollama', 'NapCat')]
        for name, path, exists in paths.describe():
            if not exists and name in ('Python', 'Ollama', 'NapCat'):
                log('  [!!] %s 不存在：%s' % (name, path))
        if missing:
            st.warn('缺少：%s' % '、'.join(missing),
                    '按日志里的路径检查安装位置，或用 NAL_* 环境变量覆盖')
        else:
            st.ok('关键路径就绪')

    _run_step('ollama', ensure_ollama)
    _run_step('napcat', ensure_napcat)
    _run_step('bridge', ensure_bridge)
    if args.no_supervisor:
        lifecycle.mark('supervisor', 'skipped', '--no-supervisor')
    else:
        _run_step('supervisor', ensure_supervisor)
    _run_step('admin', lambda: ensure_admin_ui(open_flag=not args.no_admin_ui))
    if args.no_napcat_ui:
        lifecycle.mark('napcat_ui', 'skipped', '已关闭自动打开')
    else:
        with lifecycle.stage('napcat_ui') as st:
            if ensure_napcat_ui(open_flag=True):
                st.ok('已请求打开')
            else:
                st.skip('UI 未就绪，未自动打开')

    log('\n' + '=' * 60)
    log('  启动流程完成。')
    log('  - bot 桥接窗口（CharacterBot）与 NapCat 窗口需保持打开')
    log('  - 守护窗口（NAL Watchdog）会监视桥接；关掉它即停止自动重启')
    log('  - 若 NapCat 弹二维码，请用手机 QQ 扫码授权登录')
    log('  - 管理面板：http://127.0.0.1:%d' % PORT_ADMIN)
    log('=' * 60)


if __name__ == '__main__':
    sys.exit(main())
