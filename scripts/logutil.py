# -*- coding: utf-8 -*-
"""日志小工具：按 key 限流的告警。

故障（Ollama 掉线、磁盘写满）时同一条告警可能每轮都触发；直接 warning 会刷屏，
把真正有用的日志挤掉。throttled() 让同一 key 在 interval 秒内只记一次，
调用方拿到返回值可以决定是否做更重的动作（默认不需要）。
"""
import threading
import time

_lock = threading.Lock()
_last = {}


def throttled(logger, key, level, msg, *args, interval=120, exc_info=False):
    """同一 key 每 interval 秒最多记一次日志；返回本次是否真的写了。"""
    now = time.monotonic()
    with _lock:
        prev = _last.get(key)
        if prev is not None and now - prev < interval:
            return False
        _last[key] = now
    logger.log(level, msg, *args, exc_info=exc_info)
    return True


def reset():
    """清空限流状态（测试或长跑后重置用）。"""
    with _lock:
        _last.clear()