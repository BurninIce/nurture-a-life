# -*- coding: utf-8 -*-
"""TTS 常开开关：按会话（每个群 / 每个私聊对象）独立记录，落盘重启后仍生效。

存储：state/personas/<id>/tts/g<gid>.json / p<uid>.json
    {"key": "g123456", "enabled": true, "style": "daily", "updated": "…"}

与 runtime_config 的 tts_enabled（全局总开关）是两层：
总开关优先 —— 关着时这里全灭，避免全局静音后个别群还在发语音。
"""
import datetime
import json
import logging
import os
import re
import threading

import persona

LOG = logging.getLogger('qq_bridge')

_ROOT = None  # None = 跟随当前角色的运行态目录

_LOCK = threading.RLock()
_KEY_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')


def root():
    """当前 TTS 状态目录：显式 set_root 优先，否则按角色隔离。"""
    return _ROOT or persona.tts_root()


def set_root(path=None):
    """切换根目录（离线模拟/测试隔离用）。"""
    global _ROOT
    _ROOT = path or None
    return root()


def conv_key(is_group, gid, uid):
    """会话唯一键：群按群号，私聊按对方 QQ 号。"""
    return ('g%s' % gid) if (is_group and gid) else ('p%s' % uid)


def _path(key):
    return os.path.join(root(), '%s.json' % key)


def load(key):
    if not key or not _KEY_RE.match(str(key)):
        return {}
    with _LOCK:
        try:
            with open(_path(key), encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except Exception:
            LOG.warning('tts 状态读取失败（%s）', key, exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}


def is_enabled(key):
    return bool(load(key).get('enabled'))


def get_style(key):
    return load(key).get('style') or ''


def set_state(key, enabled, style=None):
    """写开关（+可选语气）。返回是否实际改动。"""
    if not key or not _KEY_RE.match(str(key)):
        return False
    with _LOCK:
        cur = load(key)
        nxt = dict(cur)
        nxt['key'] = str(key)
        nxt['enabled'] = bool(enabled)
        if style:
            nxt['style'] = style
        nxt.setdefault('style', '')
        nxt['updated'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        if cur.get('enabled') == nxt['enabled'] and cur.get('style') == nxt['style']:
            return False
        os.makedirs(root(), exist_ok=True)
        tmp = _path(key) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(nxt, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _path(key))
        return True


def enabled_count():
    """当前常开的会话数（/diag 用）。目录没建过时为 0。"""
    with _LOCK:
        try:
            names = os.listdir(root())
        except FileNotFoundError:
            return 0
        n = 0
        for name in names:
            if name.endswith('.json') and is_enabled(name[:-5]):
                n += 1
        return n


# ---- 一次性：光发 /tts = 「我下一条听到的回复带语音」----
# 只活在进程里：重启后丢掉是可接受的（人当时就在会话里，会再发一次）。
# 键带 uid：群里 A 排了队，不能被 B 接话吃掉。
_PENDING = set()


def pending_key(conv_key, uid):
    return '%s:%s' % (conv_key, uid)


def arm_pending(key):
    if not key or ':' not in str(key):
        return False
    with _LOCK:
        _PENDING.add(str(key))
    return True


def take_pending(key):
    """取走一次性标记。返回是否命中。"""
    with _LOCK:
        if str(key) not in _PENDING:
            return False
        _PENDING.discard(str(key))
        return True


def has_pending(key):
    with _LOCK:
        return str(key) in _PENDING


def clear_pending(key):
    with _LOCK:
        _PENDING.discard(str(key))


def clear_pending_in_conv(conv_key):
    """关掉/打开某会话常开时，把该会话里每个人的一次性排队一起清掉。"""
    with _LOCK:
        for k in [k for k in _PENDING if str(k).startswith(conv_key + ':')]:
            _PENDING.discard(k)


def clear_pending_all():
    """切换角色时清空一次性语音排队：新角色不该接过旧角色的「下一条带语音」。"""
    with _LOCK:
        _PENDING.clear()
