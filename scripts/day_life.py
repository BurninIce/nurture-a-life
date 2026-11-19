# -*- coding: utf-8 -*-
"""她的日常：角色卡 life 段（固定作息/习惯）与日程条目规范化。

角色卡 prompts/personas/<id>/card.json 的 life 段：
    {"enabled": true,
     "routine": ["07:30-08:30 起床，泡一壶红茶", "23:30 洗漱"],
     "habits": ["午后容易犯困"]}

routine 每行「HH:MM-HH:MM 内容」或「HH:MM 内容」，用作每日日程生成的参考；
没有生成结果时，也按当前时段给注入块兜底。这里只做解析与校验，不读写状态文件。
"""
import datetime
import re

import card_source
import rp_config as cfg

TIME_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')
LINE_RE = re.compile(r'^\s*(\d{1,2}:\d{2})\s*(?:-\s*(\d{1,2}:\d{2}))?\s+(.+)$')
ITEM_MAX_CHARS = 48
ITEM_MIN_CHARS = 2
REFUSAL_MARKS = ('作为AI', '作为 AI', '抱歉', '无法生成', '请补充')
_BULLETS = '·•-—*　 \t'
_QUOTES = '「」“”"\'`'


def config(pid=None):
    try:
        data = card_source.load(pid=pid)
    except (OSError, ValueError):
        return {}
    life = data.get('life')
    return life if isinstance(life, dict) else {}


def enabled(pid=None):
    return config(pid).get('enabled', True) is not False


def routine(pid=None):
    return [str(x).strip() for x in (config(pid).get('routine') or [])
            if str(x).strip()]


def habits(pid=None):
    return [str(x).strip() for x in (config(pid).get('habits') or [])
            if str(x).strip()]


def normalize_time(value):
    """'7:00' / '07:00' / '7：00' → '07:00'；不合法返回 ''。"""
    text = str(value or '').strip().replace('：', ':')
    m = TIME_RE.match(text)
    if not m:
        return ''
    return '%02d:%02d' % (int(m.group(1)), int(m.group(2)))


def minutes(hhmm):
    t = normalize_time(hhmm)
    if not t:
        return None
    return int(t[:2]) * 60 + int(t[3:])


def clock(now=None):
    ts = datetime.datetime.fromtimestamp(now) if now is not None else datetime.datetime.now()
    return ts.strftime('%H:%M')


def clean_text(value):
    text = str(value or '').replace('\n', ' ').strip()
    text = text.strip(_BULLETS + _QUOTES).strip()
    text = re.sub(r'\s+', ' ', text)
    if not (ITEM_MIN_CHARS <= len(text) <= ITEM_MAX_CHARS):
        return ''
    squeezed = text.replace(' ', '')
    if any(m in squeezed for m in REFUSAL_MARKS):
        return ''
    return text


def parse_routine_line(line):
    """'07:30-08:30 起床' → {start, end, text}；格式不对返回 None。"""
    m = LINE_RE.match(str(line or ''))
    if not m:
        return None
    start = normalize_time(m.group(1))
    end = normalize_time(m.group(2)) if m.group(2) else ''
    text = clean_text(m.group(3))
    if not start or not text:
        return None
    return {'start': start, 'end': end, 'text': text}


def routine_slots(pid=None):
    out = []
    for line in routine(pid):
        slot = parse_routine_line(line)
        if slot:
            out.append(slot)
    out.sort(key=lambda s: minutes(s['start']))
    return out


def current_slot(now=None, pid=None):
    """当前时间落在哪个固定作息段；返回 {start,end,text}，没有作息返回 None。"""
    slots = routine_slots(pid)
    if not slots:
        return None
    cur = minutes(clock(now))
    picked = slots[0]
    for slot in slots:
        if minutes(slot['start']) <= cur:
            picked = slot
        else:
            break
    return picked


def routine_items(pid=None):
    """固定作息 → 日程条目（当天生成没拿到时的兜底）。"""
    return [{'time': s['start'], 'text': s['text'], 'key': False, 'source': 'routine'}
            for s in routine_slots(pid)]


def normalize_items(raw, source='plan'):
    """模型/判官给的条目 → 合法、去重、按时间排序的列表；最多 SELF_PLAN_MAX_ITEMS。"""
    limit = getattr(cfg, 'SELF_PLAN_MAX_ITEMS', 12)
    out = []
    seen = set()
    for row in raw or []:
        if isinstance(row, dict):
            time = normalize_time(row.get('time') or row.get('start'))
            text = clean_text(row.get('text'))
            key = bool(row.get('key'))
            src = str(row.get('source') or source)
        elif isinstance(row, str):
            slot = parse_routine_line(row)
            if not slot:
                continue
            time, text, key, src = slot['start'], slot['text'], False, source
        else:
            continue
        if not time or not text or time in seen:
            continue
        seen.add(time)
        out.append({'time': time, 'text': text, 'key': key, 'source': src})
    out.sort(key=lambda i: minutes(i['time']))
    return out[:limit]


def current_item(items, now=None):
    """日程里当前那一格：最后一个开始时间不晚于现在的条目；都在后面就取第一条。"""
    rows = [i for i in (items or []) if normalize_time(i.get('time'))]
    if not rows:
        return None
    cur = minutes(clock(now))
    rows.sort(key=lambda i: minutes(i['time']))
    picked = rows[0]
    if minutes(rows[0]['time']) > cur:
        return picked
    for row in rows:
        if minutes(row['time']) <= cur:
            picked = row
        else:
            break
    return picked
