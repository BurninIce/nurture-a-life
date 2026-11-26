# -*- coding: utf-8 -*-
"""群活跃时段学习：主动开话题只能在群友真的活跃的时候。

用户需求（2026-10-20）：主动开口的时间约束要贴群友作息，而且**这个时段由智能体
观察几天后自己判断**，不要写死 9-23 点。

做法（纯统计，不花模型调用）：
1. 每条群消息 `record(gid, ts)` 记一个「日期 + 小时」计数
2. 每群独立存 `state/groups/<gid>/activity.json`，只留最近 `GATE_ACTIVITY_KEEP_DAYS` 天
3. 观察满 `GATE_ACTIVITY_MIN_DAYS` 天后，按**工作日 / 周末两套画像**算每小时日均消息数；
   取 ≥ max(峰值×RATIO, 绝对下限) 的小时，补单小时空洞，再向前后各放宽 MARGIN 小时
4. 数据不够（天数不足 / 峰值太低）→ 回落 `GATE_ACTIVE_HOURS` 默认窗口，并在 source 里注明原因

为什么按"日均"而不是总次数：避免某天刷屏把凌晨也顶上活跃。
为什么分工作日/周末：8 人群的作息差别主要在这条线上，30 天数据足够撑两套画像。
"""
import datetime
import json
import os

import rp_config as cfg

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROOT = os.path.join(ROOT_DIR, 'state', 'groups')
_ROOT = DEFAULT_ROOT

_cache = {}      # gid -> data（进程内缓存，减少读盘）
_dirty = {}      # gid -> 未落盘的消息条数


def set_root(path=None):
    """切换群状态根目录（离线模拟/测试隔离用）。"""
    global _ROOT
    _ROOT = path or DEFAULT_ROOT
    _cache.clear()
    _dirty.clear()
    return _ROOT


def group_dir(gid):
    return os.path.join(_ROOT, str(gid))


def path_for(gid):
    return os.path.join(group_dir(gid), 'activity.json')


def _empty(gid):
    return {'gid': str(gid), 'version': 1, 'days': {}, 'updated': ''}


def load(gid):
    gid = str(gid)
    if gid in _cache:
        return _cache[gid]
    try:
        with open(path_for(gid), encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get('days'), dict):
            data = _empty(gid)
    except (OSError, ValueError):
        data = _empty(gid)
    _cache[gid] = data
    return data


def save(gid):
    """原子写（临时文件 + os.replace）。"""
    gid = str(gid)
    data = _cache.get(gid)
    if data is None:
        return None
    d = group_dir(gid)
    try:
        os.makedirs(d, exist_ok=True)
        dst = path_for(gid)
        tmp = dst + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, dst)
        _dirty[gid] = 0
        return dst
    except OSError:
        return None


def record(gid, ts=None):
    """记一条群消息（按小时分桶）。累计 N 条落盘一次。"""
    gid = str(gid)
    if ts is None:
        dt = datetime.datetime.now()
    elif isinstance(ts, datetime.datetime):
        dt = ts
    else:
        dt = datetime.datetime.fromtimestamp(ts)
    data = load(gid)
    days = data.setdefault('days', {})
    key = dt.strftime('%Y-%m-%d')
    row = days.get(key)
    if not isinstance(row, list) or len(row) != 24:
        row = [0] * 24
        days[key] = row
    row[dt.hour] += 1
    data['updated'] = dt.strftime('%Y-%m-%d %H:%M')
    _prune(data)
    _dirty[gid] = _dirty.get(gid, 0) + 1
    if _dirty[gid] >= cfg.GATE_ACTIVITY_SAVE_EVERY:
        save(gid)
    return data


def _prune(data):
    """只保留最近 N 天。"""
    days = data.get('days') or {}
    keep = cfg.GATE_ACTIVITY_KEEP_DAYS
    if len(days) <= keep:
        return
    keys = sorted(days.keys())
    for k in keys[:-keep]:
        days.pop(k, None)


def _as_dt(ts):
    """接受 None / datetime / unix 时间戳（bridge 传 float，自测传 datetime）。"""
    if ts is None:
        return datetime.datetime.now()
    if isinstance(ts, datetime.datetime):
        return ts
    return datetime.datetime.fromtimestamp(ts)


def _default_hours():
    lo, hi = cfg.GATE_ACTIVE_HOURS
    return set(range(lo, hi))


def learn(gid, now=None):
    """学习当前时段的活跃小时集合。返回 dict：

    {'hours': set, 'source': 'learned:weekday(4天)' | 'default(insufficient:2<3)',
     'days': n, 'peak': 峰值日均, 'threshold': 阈值, 'avg': [24 个日均]}
    """
    now = _as_dt(now)
    data = load(gid)
    days = data.get('days') or {}
    is_weekend = now.weekday() >= 5
    min_days = cfg.GATE_ACTIVITY_MIN_DAYS

    def rows_of(weekend):
        out = []
        for k, row in days.items():
            try:
                d = datetime.date.fromisoformat(k)
            except ValueError:
                continue
            if (d.weekday() >= 5) == weekend and isinstance(row, list) and len(row) == 24:
                out.append(row)
        return out

    rows = rows_of(is_weekend)
    span = 'weekend' if is_weekend else 'weekday'
    if len(rows) < min_days:
        rows_all = rows_of(False) + rows_of(True)
        if len(rows_all) >= min_days:
            rows = rows_all
            span = 'all'
    n = len(rows)
    if n < min_days:
        return {'hours': _default_hours(),
                'source': 'default(insufficient:%d<%d)' % (n, min_days),
                'days': n, 'peak': 0.0, 'threshold': 0.0, 'avg': [0.0] * 24}

    avg = [sum(r[h] for r in rows) / float(n) for h in range(24)]
    peak = max(avg)
    if peak < cfg.GATE_ACTIVITY_MIN_PEAK:
        return {'hours': _default_hours(),
                'source': 'default(low_signal:peak=%.1f<%d)'
                          % (peak, cfg.GATE_ACTIVITY_MIN_PEAK),
                'days': n, 'peak': peak, 'threshold': 0.0, 'avg': avg}

    thr = max(peak * cfg.GATE_ACTIVITY_PEAK_RATIO, cfg.GATE_ACTIVITY_MIN_MSGS_PER_DAY)
    active = set(h for h in range(24) if avg[h] >= thr)
    # 补单小时空洞（19 有、21 有、20 没有 → 20 也算）
    for h in range(1, 23):
        if h not in active and (h - 1) in active and (h + 1) in active:
            active.add(h)
    # 前后放宽（人一般会早到/晚走）
    m = cfg.GATE_ACTIVITY_MARGIN_HOURS
    ext = set()
    for h in active:
        for k in range(h - m, h + m + 1):
            if 0 <= k <= 23:
                ext.add(k)
    return {'hours': ext, 'source': 'learned:%s(%d天)' % (span, n),
            'days': n, 'peak': round(peak, 2), 'threshold': round(thr, 2), 'avg': avg}


def active_hours(gid, now=None):
    """便捷入口：返回 (活跃小时集合, 学习信息)。额外把缓存落盘。"""
    info = learn(gid, now=now)
    if _dirty.get(str(gid)):
        save(gid)
    return info['hours'], info


def ranges_text(hours):
    """{19,20,21,22} → '19:00-23:00'（用于日志/调试）。"""
    if not hours:
        return '（无）'
    hs = sorted(hours)
    out = []
    start = prev = hs[0]
    for h in hs[1:]:
        if h == prev + 1:
            prev = h
            continue
        out.append('%02d:00-%02d:00' % (start, prev + 1))
        start = prev = h
    out.append('%02d:00-%02d:00' % (start, prev + 1))
    return '、'.join(out)


def summary(gid, now=None):
    """一行人类可读摘要（日志/面板用）。"""
    info = learn(gid, now=now)
    return '%s · %s · 样本 %d 天 · 峰值 %.1f 条/时' % (
        ranges_text(info['hours']), info['source'], info['days'], info['peak'])


# ---------- 自测（合成 4 天作息，验证学习与冷启动回落） ----------

def _selftest():
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp(prefix='acttest_')
    set_root(tmp)
    gid = '900001'
    base = datetime.date(2026, 10, 12)  # 周一
    # 工作日：19-23 点热闹（每小时 8 条）、12 点 3 条、凌晨 0 条
    # 周末：10-14 与 20-23 点热闹
    for i in range(4):  # 周一到周四
        d = base + datetime.timedelta(days=i)
        for hour, cnt in ((3, 0), (12, 3), (19, 8), (20, 9), (21, 7), (22, 6), (23, 4)):
            for k in range(cnt):
                record(gid, datetime.datetime.combine(
                    d, datetime.time(hour, k % 60)))
    for i in (5, 6):  # 周六、周日
        d = base + datetime.timedelta(days=i)
        for hour, cnt in ((10, 6), (11, 7), (12, 8), (13, 6), (14, 4),
                          (20, 8), (21, 9), (22, 6)):
            for k in range(cnt):
                record(gid, datetime.datetime.combine(
                    d, datetime.time(hour, k % 60)))
    save(gid)

    print('=== 群活跃时段学习自测（合成 6 天数据：4 工作日 + 2 周末）===')
    wd = datetime.datetime(2026, 10, 19, 21, 0)   # 周一
    we = datetime.datetime(2026, 10, 17, 12, 0)   # 周六
    for label, now in (('工作日视角', wd), ('周末视角', we)):
        info = learn(gid, now=now)
        print('%-6s → %-22s %s（样本 %d 天，峰值 %.1f，阈值 %.1f）'
              % (label, ranges_text(info['hours']), info['source'],
                 info['days'], info['peak'], info['threshold']))
        for h in (3, 9, 12, 19, 23):
            mark = '活跃' if h in info['hours'] else '安静'
            print('        %02d:00 %s' % (h, mark))

    print('\n--- 冷启动（只有 1 天数据）---')
    gid2 = '900002'
    record(gid2, datetime.datetime(2026, 10, 19, 21, 0))
    save(gid2)
    info = learn(gid2, now=wd)
    print('1 天数据 → %s（%s）' % (ranges_text(info['hours']), info['source']))
    print('说明：观察不足 %d 天时用默认窗口 %s，不会瞎猜。'
          % (cfg.GATE_ACTIVITY_MIN_DAYS, ranges_text(_default_hours())))

    print('\n--- 摘要输出 ---')
    print(summary(gid, now=wd))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
