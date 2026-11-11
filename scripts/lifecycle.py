# -*- coding: utf-8 -*-
"""启动生命周期与运行事件。

解决三个问题：
  1. 启动到哪一步了？——固定阶段 + 状态机（pending/running/ok/warn/fail/skipped）。
  2. 出问题怎么查？——每个阶段带 detail/fix，事件与日志有增量序号。
  3. 面板怎么实时看到？——进程内环形缓冲 + 长轮询，不依赖 WebSocket。

两个进程共用本模块：
  - launcher（启动器）configure('launcher')，每个阶段结束写 state/launch_snapshot.json；
  - qq_bridge（桥接）configure('bridge')，把阶段/事件留在自己进程，管理面板通过
    HTTP 读取，再由 snapshot() 合并启动器快照。

设计约束：
  - import 本模块**不产生任何文件副作用**；只有 configure/emit 之后才创建 logs/state。
  - 事件与日志都有限长，避免长时间运行吃内存。
  - 写盘失败不影响主流程（可观测性不能反过来拖垮 bot）。
"""
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE_DIR = os.path.join(ROOT, 'state')
LOG_DIR = os.path.join(ROOT, 'logs')
EVENTS_PATH = os.path.join(LOG_DIR, 'events.jsonl')
LAUNCH_SNAPSHOT = os.path.join(STATE_DIR, 'launch_snapshot.json')

EVENT_RING = 2000
LOG_RING = 1000
EVENT_FILE_MAX = 5 * 1024 * 1024
EVENT_FILE_BACKUPS = 3

# 阶段中文名（key 稳定，label 只用于展示）
STAGE_LABELS = {
    # launcher
    'preflight': '环境检查',
    'ollama': 'Ollama',
    'napcat': 'NapCat',
    'bridge': 'QQ 桥接',
    'supervisor': '进程守护',
    'admin': '管理面板',
    'napcat_ui': 'NapCat UI',
    # bridge
    'config': '配置加载',
    'single_instance': '单实例',
    'ws_listen': '反向 WS',
    'napcat_online': 'NapCat 连接',
    'selfcheck': '启动自检',
    'warmup': '预热',
    'ready': '就绪',
}

LAUNCHER_STAGES = ['preflight', 'ollama', 'napcat', 'bridge', 'supervisor',
                   'admin', 'napcat_ui']
BRIDGE_STAGES = ['config', 'single_instance', 'ws_listen', 'napcat_online',
                 'selfcheck', 'warmup', 'ready']

TERMINAL = ('ok', 'warn', 'fail', 'skipped')
LEVEL_BY_STATE = {'ok': 'info', 'warn': 'warn', 'fail': 'error',
                  'running': 'debug', 'skipped': 'info', 'pending': 'debug'}
_WORST_ORDER = {'ok': 0, 'skipped': 0, 'pending': 1, 'running': 2, 'warn': 3, 'fail': 4}

# 脱敏：key/token 不进入日志环形缓冲与事件文件
_SECRET_PATTERNS = (
    (re.compile(r'sk-[A-Za-z0-9_\-]{6,}'), 'sk-***'),
    (re.compile(r'(?i)((?:api[_-]?key|token|secret|authorization)\s*[=:]\s*)'
                r'([^\s,;"\']{4,})'), r'\1***'),
)


def _iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def redact(text):
    out = str(text or '')
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _rotate(path, max_bytes, backups):
    try:
        if os.path.getsize(path) < max_bytes:
            return
    except OSError:
        return
    for i in range(backups - 1, 0, -1):
        src = '%s.%d' % (path, i)
        if os.path.exists(src):
            try:
                os.replace(src, '%s.%d' % (path, i + 1))
            except OSError:
                return
    try:
        os.replace(path, path + '.1')
    except OSError:
        pass


class Lifecycle(object):
    def __init__(self):
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self.source = None
        self.plan = []
        self.stages = {}
        self._events = deque(maxlen=EVENT_RING)
        self._logs = deque(maxlen=LOG_RING)
        self._event_seq = 0
        self._log_seq = 0
        self._log_handler = None

    # ---- 初始化 ----
    def configure(self, source, stages=None):
        """设置进程角色与阶段清单。重复调用不会清空已有状态。"""
        with self._lock:
            if self.source:
                return
            self.source = source
            self.plan = list(stages or (LAUNCHER_STAGES if source == 'launcher'
                                        else BRIDGE_STAGES))
            self.stages = {key: {
                'key': key,
                'label': STAGE_LABELS.get(key, key),
                'state': 'pending',
                'started_at': None,
                'ended_at': None,
                'detail': '',
                'fix': '',
            } for key in self.plan}

    def reset(self):
        with self._cond:
            self.stages = {}
            self._events.clear()
            self._logs.clear()
            self._event_seq = 0
            self._log_seq = 0
            self.source = None
            self.plan = []

    # ---- 阶段 ----
    def start(self, key, detail=''):
        return self.mark(key, 'running', detail=detail)

    def finish(self, key, state, detail='', fix='', message=''):
        return self.mark(key, state, detail=detail, fix=fix, message=message)

    def mark(self, key, state, detail='', fix='', message=''):
        """更新阶段并产生一条事件。状态/详情完全相同时不重复发。"""
        if state not in ('pending', 'running') + TERMINAL:
            raise ValueError('bad lifecycle state: %s' % state)
        with self._cond:
            st = self.stages.get(key)
            if st is None:
                st = {'key': key, 'label': STAGE_LABELS.get(key, key),
                      'state': 'pending', 'started_at': None,
                      'ended_at': None, 'detail': '', 'fix': ''}
                self.stages[key] = st
                if key not in self.plan:
                    self.plan.append(key)
            if (st['state'] == state and st['detail'] == detail
                    and st['fix'] == fix):
                return None
            now = _iso()
            if st['started_at'] is None:
                st['started_at'] = now
            if state in TERMINAL:
                st['ended_at'] = now
            st['state'] = state
            st['detail'] = detail or ''
            st['fix'] = fix or ''

            self._event_seq += 1
            event = {
                'seq': self._event_seq,
                'ts': now,
                'source': self.source or 'unknown',
                'stage': key,
                'label': st['label'],
                'state': state,
                'level': LEVEL_BY_STATE.get(state, 'info'),
                'message': redact(message or detail or ''),
                'detail': redact(detail or ''),
                'fix': redact(fix or ''),
            }
            self._events.append(event)
            self._cond.notify_all()
            self._sink_event(event)
            if self.source == 'launcher':
                self._write_launch_snapshot_locked()
            return event

    def stage(self, key):
        return _StageHandle(self, key)

    # ---- 事件读取 ----
    def events_since(self, since=0, timeout=0.0):
        since = int(since or 0)
        timeout = max(0.0, min(float(timeout or 0), 20.0))
        deadline = time.time() + timeout
        with self._cond:
            while time.time() < deadline and not any(
                    e['seq'] > since for e in self._events):
                self._cond.wait(deadline - time.time())
            events = [e for e in self._events if e['seq'] > since]
            oldest = self._events[0]['seq'] if self._events else 0
            return {
                'events': events,
                'next': self._event_seq,
                'oldest': oldest,
                'lost': bool(since and oldest and since < oldest - 1),
                'ts': _iso(),
            }

    def snapshot(self, include_launcher=True):
        with self._lock:
            source = self.source or 'unknown'
            stages = self._stage_list_locked()
            worst = _worst(stages)
            current = {'source': source, 'worst': worst, 'stages': stages}
            launch = None
            if include_launcher and source != 'launcher':
                launch = read_launch_snapshot()
        sources = []
        if launch:
            sources.append({'source': 'launcher',
                            'worst': _worst(launch.get('stages') or []),
                            'stages': launch.get('stages') or [],
                            'ts': launch.get('ts')})
        sources.append(current)
        all_stages = [s for src in sources for s in (src.get('stages') or [])]
        return {
            'ts': _iso(),
            'worst': _worst(all_stages),
            'sources': sources,
        }

    def _stage_list_locked(self):
        ordered = [self.stages[k] for k in self.plan if k in self.stages]
        extra = [v for k, v in self.stages.items() if k not in self.plan]
        return [dict(s) for s in ordered + extra]

    # ---- 日志 ----
    def attach_logs(self, level=logging.INFO):
        """把根 logger 的日志抄一份进环形缓冲（控制台/文件日志保持不变）。"""
        with self._lock:
            if self._log_handler is not None:
                return self._log_handler
            handler = _RingHandler(self)
            handler.setLevel(level)
            logging.getLogger().addHandler(handler)
            self._log_handler = handler
            return handler

    def _append_log(self, level, name, message):
        with self._cond:
            self._log_seq += 1
            self._logs.append({
                'seq': self._log_seq,
                'ts': _iso(),
                'level': level,
                'logger': name,
                'message': redact(message),
            })
            self._cond.notify_all()

    def logs_since(self, since=0, level=None, limit=200):
        since = int(since or 0)
        minimum = _level_no(level)
        with self._lock:
            rows = [r for r in self._logs
                    if r['seq'] > since and _level_no(r['level']) >= minimum]
            return {
                'logs': rows[-int(limit or 200):],
                'next': self._log_seq,
                'oldest': self._logs[0]['seq'] if self._logs else 0,
                'ts': _iso(),
            }

    # ---- 内部 ----
    def _sink_event(self, event):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            _rotate(EVENTS_PATH, EVENT_FILE_MAX, EVENT_FILE_BACKUPS)
            with open(EVENTS_PATH, 'a', encoding='utf-8') as f:
                f.write(json.dumps(event, ensure_ascii=False) + '\n')
        except OSError:
            pass

    def _write_launch_snapshot_locked(self):
        payload = {'ts': _iso(), 'source': 'launcher',
                   'stages': self._stage_list_locked()}
        _atomic_json(LAUNCH_SNAPSHOT, payload)


class _StageHandle(object):
    """with lifecycle.stage('ollama') as st: ... ；未显式结束则按 ok 处理。"""

    def __init__(self, bus, key):
        self.bus = bus
        self.key = key
        self._done = False

    def __enter__(self):
        self.bus.start(self.key)
        return self

    def ok(self, detail=''):
        return self.finish('ok', detail)

    def warn(self, detail='', fix=''):
        return self.finish('warn', detail, fix)

    def fail(self, detail='', fix=''):
        return self.finish('fail', detail, fix)

    def skip(self, detail=''):
        return self.finish('skipped', detail)

    def finish(self, state, detail='', fix=''):
        self._done = True
        return self.bus.finish(self.key, state, detail=detail, fix=fix)

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.fail(redact(exc))
            return False
        if not self._done:
            self.ok()
        return False


class _RingHandler(logging.Handler):
    def __init__(self, bus):
        logging.Handler.__init__(self)
        self.bus = bus

    def emit(self, record):
        try:
            self.bus._append_log(record.levelname, record.name,
                                 record.getMessage())
        except Exception:
            pass


def _level_no(name):
    if not name:
        return logging.INFO
    return getattr(logging, str(name).upper(), logging.INFO)


def _worst(stages):
    rank = 0
    for stage in stages or []:
        rank = max(rank, _WORST_ORDER.get(stage.get('state'), 0))
    for name, value in _WORST_ORDER.items():
        if value == rank:
            return name
    return 'ok'


def _atomic_json(path, payload):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def read_launch_snapshot():
    try:
        with open(LAUNCH_SNAPSHOT, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_launch_snapshot():
    with _LC._lock:
        _LC._write_launch_snapshot_locked()


# ---- 模块级单例与便捷函数 ----
_LC = Lifecycle()


def configure(source, stages=None):
    return _LC.configure(source, stages)


def reset():
    return _LC.reset()


def start(key, detail=''):
    return _LC.start(key, detail)


def mark(key, state, detail='', fix='', message=''):
    return _LC.mark(key, state, detail=detail, fix=fix, message=message)


def finish(key, state, detail='', fix='', message=''):
    return _LC.finish(key, state, detail=detail, fix=fix, message=message)


def stage(key):
    return _LC.stage(key)


def snapshot(include_launcher=True):
    return _LC.snapshot(include_launcher=include_launcher)


def events_since(since=0, timeout=0.0):
    return _LC.events_since(since, timeout)


def logs_since(since=0, level=None, limit=200):
    return _LC.logs_since(since, level=level, limit=limit)


def attach_logs(level=logging.INFO):
    return _LC.attach_logs(level)


def worst():
    return _LC.snapshot()['worst']
