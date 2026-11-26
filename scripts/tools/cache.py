# -*- coding: utf-8 -*-
"""工具结果的 TTL 缓存。

群聊里同一张图/同一个链接会被反复转发，搜索也可能被连续追问。
缓存放在 tools 层而不是各工具内部，保证「缓存键 = 工具名 + 参数」这一条语义统一，
调试时只看一个地方。
"""
import hashlib
import json
import threading
import time

_MAX = 256
_items = {}
_lock = threading.Lock()


def key_for(name, args):
    try:
        blob = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        blob = str(args)
    digest = hashlib.sha1(blob.encode('utf-8')).hexdigest()
    return '%s:%s' % (name, digest)


def get(name, args):
    key = key_for(name, args)
    now = time.time()
    with _lock:
        item = _items.get(key)
        if not item:
            return None
        ts, ttl, result = item
        if ttl <= 0 or now - ts > ttl:
            _items.pop(key, None)
            return None
        return result


def put(name, args, result, ttl):
    if ttl <= 0:
        return
    key = key_for(name, args)
    now = time.time()
    with _lock:
        if len(_items) >= _MAX:
            for k in [k for k, (ts, t, _) in _items.items() if now - ts > t]:
                _items.pop(k, None)
            if len(_items) >= _MAX:
                for k in list(_items.keys())[:_MAX // 2]:
                    _items.pop(k, None)
        _items[key] = (now, ttl, result)


def clear():
    with _lock:
        _items.clear()
