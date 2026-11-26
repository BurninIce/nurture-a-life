# -*- coding: utf-8 -*-
"""逻辑日界线：一天从 04:00 开始，不是 00:00。

日程、日记、书架初始化都挂在同一条边界上，避免两套跨天逻辑打架。
边界时间由 runtime_config 的 self_day_start 覆盖，默认读 rp_config.SELF_DAY_START。
"""
import datetime
import re

import rp_config as cfg

_DEFAULT = '04:00'
_HHMM = re.compile(r'^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$')


def parse_hhmm(value, default=_DEFAULT):
    m = _HHMM.match(str(value or ''))
    if not m:
        m = _HHMM.match(default)
    return int(m.group(1)) * 60 + int(m.group(2))


def start_minutes():
    raw = getattr(cfg, 'SELF_DAY_START', _DEFAULT)
    try:
        import runtime_config as rc
        raw = rc.get('self_day_start', raw)
    except Exception:
        pass
    return parse_hhmm(raw)


def _local(now=None):
    return datetime.datetime.fromtimestamp(now) if now is not None else datetime.datetime.now()


def logical_date(now=None, start=None):
    """now 属于哪个逻辑日。凌晨 start 之前算前一天。"""
    ts = _local(now)
    minutes = start if start is not None else start_minutes()
    if ts.hour * 60 + ts.minute < minutes:
        ts -= datetime.timedelta(days=1)
    return ts.date()


def logical_today(now=None):
    return logical_date(now).strftime('%Y-%m-%d')


def logical_yesterday(now=None):
    return (logical_date(now) - datetime.timedelta(days=1)).strftime('%Y-%m-%d')


def days_ago(now, days):
    return (logical_date(now) - datetime.timedelta(days=int(days))).strftime('%Y-%m-%d')


def day_start_ts(now=None, start=None):
    """now 所属逻辑日的起始时刻（默认 04:00）。"""
    day = logical_date(now, start)
    minutes = start if start is not None else start_minutes()
    dt = datetime.datetime.combine(day, datetime.time(0, 0)) + \
        datetime.timedelta(minutes=minutes)
    return dt.timestamp()


def at_or_after_start(now=None):
    ts = _local(now)
    return ts.hour * 60 + ts.minute >= start_minutes()


def midpoint_date(start_ts, end_ts):
    """一段区间的中点落在哪一天，日记日期用它。"""
    mid = (float(start_ts) + float(end_ts)) / 2.0
    return datetime.datetime.fromtimestamp(mid).strftime('%Y-%m-%d')
