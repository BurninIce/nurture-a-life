# -*- coding: utf-8 -*-
"""外部调用的日预算。

为什么需要：工具一旦开启，群里每句「今天天气怎么样」都会变成一次联网调用，
费用和延迟都没有上限。这里按「自然日 + 全部 egress 工具」共用一个额度，
超额直接返回 budget_exhausted，让模型按 TOOL_FAIL_NOTE 的口吻自己接话。

计数落盘在 state/tool_budget.json，重启不清零；跨天自动归零。
额度取 runtime_config 的 tool_daily_max，管理员面板可热改。
"""
import json
import logging
import os
import threading

import rp_config as cfg

LOG = logging.getLogger('tools')

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'state', 'tool_budget.json')
_LOCK = threading.Lock()


def _today():
    import time
    return time.strftime('%Y-%m-%d')


def _limit():
    try:
        import runtime_config as rc
        val = int(rc.get('tool_daily_max', cfg.TOOL_DAILY_MAX))
    except Exception:
        val = int(getattr(cfg, 'TOOL_DAILY_MAX', 0))
    return max(0, val)


def _read():
    """返回 (date, count)。文件坏了就当今天没用过。"""
    try:
        with open(_PATH, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('date') == _today():
            return data.get('date'), int(data.get('count', 0))
    except (OSError, ValueError, TypeError):
        pass
    return _today(), 0


def _write(date, count):
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'date': date, 'count': count}, f)
        os.replace(tmp, _PATH)
    except OSError:
        LOG.warning('tool budget 落盘失败，计数仅在内存内有效')


def used():
    with _LOCK:
        return _read()[1]


def remaining():
    return max(0, _limit() - used())


def try_consume():
    """预扣一次额度。返回 (allowed, used, limit)。缓存命中不调用这里。"""
    with _LOCK:
        date, count = _read()
        limit = _limit()
        if limit and count >= limit:
            return False, count, limit
        count += 1
        _write(date, count)
        if count == limit:
            LOG.warning('外部工具当日额度用尽：%s/%s', count, limit)
        return True, count, limit
