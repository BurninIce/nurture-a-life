# -*- coding: utf-8 -*-
"""NAL 进程守护：桥接挂了就拉起来，并把过程留在可读的状态文件里。

设计边界（为什么不是「万能看门狗」）：
  - 只守 qq_bridge。Ollama / NapCat 的启动代价与交互不同（NapCat 可能要
    扫码），自动重启它们弊大于利；它们的状态继续由面板探活展示。
  - 判定依据只有两个：反向 WS 端口是否监听、心跳文件是否新鲜。心跳由桥接
    独立线程写，生成再慢也不会把它拖陈。
  - 端口在、心跳没了 → 只标 unresponsive，**不自动杀进程**。可能是桥接卡死，
    也可能只是心跳线程写盘失败；误杀会丢正在进行的一轮对话。手动处理方式：
    关掉 CharacterBot 窗口，端口随之关闭，守护会自动重启。
  - 连续快速失败会退避与熔断，避免崩溃循环刷屏、反复拉起占满显存。

状态文件：state/supervisor.json（面板读取）
日志：logs/supervisor.log（10MB × 3 轮转）

用法：
    python scripts/supervisor.py                 # 常驻守护
    python scripts/supervisor.py --once          # 检查一次并按需拉起
    python scripts/supervisor.py --once --dry-run
    python scripts/supervisor.py --no-restart    # 只观测，不拉起
"""
import argparse
import datetime
import logging
import os
import socket
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import heartbeat  # noqa: E402
import paths  # noqa: E402

LOG = logging.getLogger('supervisor')

STATE_PATH = heartbeat.SUPERVISOR_PATH
LOG_PATH = os.path.join(ROOT, 'logs', 'supervisor.log')
BRIDGE_PORT = paths.PORT_BRIDGE_WS
INTERVAL = 10.0
STARTUP_GRACE = 90.0      # 拉起后等这么久再判断「端口没起来」
STALE_SEC = 45.0          # 心跳超过这么久算陈旧（桥接每 10s 写一次）
BACKOFF = (5, 15, 30, 60, 120, 300)
MAX_CONSECUTIVE = 6       # 连续快速失败达到这个次数就熔断
PAUSE_SEC = 900.0         # 熔断后暂停自动拉起
STABLE_SEC = 600.0        # 稳定运行这么久，清空连续失败计数
ALIVE_SEC = 35.0          # 状态文件多久没更新就认为守护自己也没了

_SINGLETON = None


def _iso(ts=None):
    try:
        return datetime.datetime.fromtimestamp(ts or time.time()).strftime(
            '%Y-%m-%d %H:%M:%S')
    except (OSError, OverflowError, ValueError):
        return ''


def _port_open(port=None, host='127.0.0.1', timeout=0.4):
    port = port or BRIDGE_PORT
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(timeout)
            rc = s.connect_ex((host, port))
            s.close()
            if rc == 0:
                return True
        except OSError:
            pass
    return False


def _normalize(state):
    """补齐字段，尽量容忍旧版本/手改过的状态文件。"""
    out = dict(state or {})
    for key in ('restarts', 'consecutive'):
        try:
            out[key] = max(0, int(out.get(key) or 0))
        except (TypeError, ValueError):
            out[key] = 0
    for key in ('last_restart_at', 'last_launch_at', 'paused_until',
                'next_restart_at', 'last_ok_at'):
        try:
            out[key] = float(out.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0.0
    return out


def evaluate(now, port_up, hb, state, startup_grace=STARTUP_GRACE,
             stale=STALE_SEC):
    """纯函数：返回 (action, detail)。

    action ∈ ok / unresponsive / starting / paused / waiting / restart
    """
    state = state or {}
    if port_up:
        age = heartbeat.age(hb, now)
        if age is None or age > stale:
            return 'unresponsive', '端口在监听，但心跳已 %s' % (
                '缺失' if age is None else '%.0f 秒未更新' % age)
        return 'ok', ''

    last_launch = float(state.get('last_launch_at') or 0)
    if last_launch and now - last_launch < startup_grace:
        return 'starting', '等待桥接启动（%d 秒）' % max(
            0, int(startup_grace - (now - last_launch)))
    if float(state.get('paused_until') or 0) > now:
        return 'paused', '连续快速失败，暂停到 %s' % _iso(
            state.get('paused_until'))
    if float(state.get('next_restart_at') or 0) > now:
        return 'waiting', '退避中，%d 秒后重试' % max(
            0, int(state['next_restart_at'] - now))
    return 'restart', '反向 WS 端口 %d 未监听' % BRIDGE_PORT


def tick(now, port_up, hb, state, dry_run=False, startup_grace=STARTUP_GRACE,
         stale=STALE_SEC, restart=True):
    """跑一次判定并更新状态；不负责真正拉起进程。

    返回 (action, new_state)。action=restart 时由调用方执行重启。
    """
    state = _normalize(state)
    if state.get('paused_until') and now >= state['paused_until']:
        state['paused_until'] = 0.0
        state['consecutive'] = 0
        state['next_restart_at'] = 0.0

    action, detail = evaluate(now, port_up, hb, state,
                              startup_grace=startup_grace, stale=stale)
    state.update({
        'ts': now,
        'pid': os.getpid(),
        'state': action,
        'detail': detail,
        'port_up': bool(port_up),
        'heartbeat_age': heartbeat.age(hb, now),
        'bridge_pid': (hb or {}).get('pid'),
        'bridge_uptime': (hb or {}).get('uptime_sec'),
        'napcat_connected': bool((hb or {}).get('napcat_connected')),
    })

    if action == 'ok':
        state['last_ok_at'] = now
        if state['consecutive'] and (hb or {}).get('uptime_sec', 0) >= STABLE_SEC:
            state['consecutive'] = 0
            state['next_restart_at'] = 0.0
    elif action == 'restart':
        if dry_run or not restart:
            state['detail'] = detail + ('（dry-run，不拉起）' if dry_run
                                        else '（--no-restart，不拉起）')
            return action, state
        state['restarts'] += 1
        state['consecutive'] += 1
        state['last_restart_at'] = now
        state['last_launch_at'] = now
        delay = BACKOFF[min(state['consecutive'] - 1, len(BACKOFF) - 1)]
        # 宽限期本身就是一次等待；退避从宽限期之后开始计，否则短退避会被宽限吞掉
        state['next_restart_at'] = now + max(0.0, startup_grace) + delay
        if state['consecutive'] >= MAX_CONSECUTIVE:
            state['paused_until'] = now + PAUSE_SEC
            state['next_restart_at'] = state['paused_until']
            state['state'] = 'paused'
            state['detail'] = '连续 %d 次快速失败，暂停 %d 分钟' % (
                state['consecutive'], int(PAUSE_SEC // 60))
    return action, state


def launch_bridge():
    """用独立控制台拉起桥接；失败抛异常，由调用方记录并继续守护。"""
    bat = paths.RUN_BOT_BAT
    if os.name == 'nt':
        flags = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
        if os.path.exists(bat):
            subprocess.Popen(['cmd', '/c', bat], cwd=ROOT,
                             creationflags=flags)
        else:
            subprocess.Popen([paths.VENV_PY, os.path.join(HERE, 'qq_bridge.py')],
                             cwd=ROOT, creationflags=flags)
    else:
        subprocess.Popen([paths.VENV_PY, os.path.join(HERE, 'qq_bridge.py')],
                         cwd=ROOT)


def write_state(state):
    return heartbeat.write(STATE_PATH, state)


def _acquire_singleton(name='NALSupervisor'):
    """命名互斥体单实例：强杀不残留，端口不同的实例可并存。"""
    global _SINGLETON
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (wintypes.LPCVOID, wintypes.BOOL,
                                     wintypes.LPCWSTR)
        handle = k32.CreateMutexW(None, False, 'Local\\' + name)
        if not handle:
            return True
        if ctypes.get_last_error() == 183:
            return False
        _SINGLETON = handle
        return True
    except Exception:
        return True


def _setup_logging(verbose=False):
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s',
                            '%H:%M:%S')
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        fh = RotatingFileHandler(LOG_PATH, maxBytes=10 * 1024 * 1024,
                                 backupCount=3, encoding='utf-8')
        fh.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S'))
        root.addHandler(fh)
    except OSError:
        pass


def run(args):
    state = _normalize(heartbeat.read(STATE_PATH))
    last_logged = None
    while True:
        now = time.time()
        hb = heartbeat.read(heartbeat.BRIDGE_PATH)
        port_up = _port_open()
        action, state = tick(now, port_up, hb, state,
                             dry_run=args.dry_run,
                             restart=not args.no_restart,
                             startup_grace=args.startup_grace,
                             stale=args.stale)
        if action == 'restart' and not args.dry_run and not args.no_restart:
            try:
                launch_bridge()
                LOG.warning('桥接未监听，已拉起（第 %d 次）', state['restarts'])
            except Exception as e:
                state['detail'] = '拉起失败：%s' % e
                state['state'] = 'error'
                LOG.exception('拉起桥接失败')
        if not write_state(state):
            LOG.warning('守护状态写盘失败：%s', STATE_PATH)

        key = (state.get('state'), state.get('detail'))
        if key != last_logged:
            level = logging.WARNING if state.get('state') in (
                'unresponsive', 'paused', 'error') else logging.INFO
            LOG.log(level, '守护状态：%s（%s）', state.get('state'),
                    state.get('detail'))
            last_logged = key

        if args.once:
            return 0
        time.sleep(args.interval)


def build_args(argv=None):
    ap = argparse.ArgumentParser(description='NAL 桥接进程守护')
    ap.add_argument('--interval', type=float, default=INTERVAL,
                    help='检查间隔秒数（默认 10）')
    ap.add_argument('--stale', type=float, default=STALE_SEC,
                    help='心跳陈旧阈值秒数（默认 45）')
    ap.add_argument('--startup-grace', type=float, default=STARTUP_GRACE,
                    help='拉起后等待端口就绪的秒数（默认 90）')
    ap.add_argument('--once', action='store_true', help='只检查一次后退出')
    ap.add_argument('--dry-run', action='store_true',
                    help='只判定并写状态，不拉起桥接')
    ap.add_argument('--no-restart', action='store_true',
                    help='只观测，不自动拉起（等同 dry-run）')
    ap.add_argument('--verbose', action='store_true')
    return ap.parse_args(argv)


def main(argv=None):
    args = build_args(argv)
    _setup_logging(args.verbose)
    if not args.once:
        if not _acquire_singleton('NALSupervisor-%s' % paths.PORT_ADMIN):
            LOG.error('已有一个守护进程在运行，本进程退出')
            return 2
    LOG.info('守护启动：端口 %d · 检查 %ss · 心跳阈值 %ss',
             BRIDGE_PORT, args.interval, args.stale)
    try:
        return run(args)
    except KeyboardInterrupt:
        state = _normalize(heartbeat.read(STATE_PATH))
        state.update({'state': 'stopped', 'detail': '守护进程已退出',
                      'ts': time.time(), 'pid': os.getpid()})
        write_state(state)
        LOG.info('守护退出')
        return 0


if __name__ == '__main__':
    sys.exit(main())
