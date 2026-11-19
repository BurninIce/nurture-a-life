# -*- coding: utf-8 -*-
"""日常生成·日程与心事（功能名「她的今天」）：日程表、事件日志、日记与好奇清单。

数据流：
    daily_scene 每日生成 {scene, outfit, plan}  → 今天的初始日程
    私聊每轮回复后：记一条事件日志；判官按对话里明确出现的事实增删改日程
                    （add/remove/update，key=true 表示有约/答应过的事）
    跨天：归档前一天的日程，凭日程 + 事件日志写日记、补充好奇清单
    注入：rp_core 在记忆卡之后注入【今天的安排】；群聊只放日程，日记与好奇只给私聊

状态落盘 state/personas/<id>/self_state.json：
    {date, items, seq, diary, curiosity, log, history, refresh_day, source}
判官改了什么只写进 self_state_log.jsonl，方便回溯。
"""
import datetime
import json
import logging
import os
import re
import threading
import time

import card_source
import daily_scene
import day_life
import persona
import rp_config as cfg

LOG = logging.getLogger('self_state')

_OVERRIDE = None
_LOCK = threading.RLock()
_CACHE = {}          # path -> data
_REFRESH_LOCK = threading.Lock()
_FAIL_UNTIL = 0.0
_FAIL_LOCK = threading.Lock()


# ---------- 状态文件 ----------

def set_path(path=None):
    """切换状态文件（离线自测/多实例隔离用）。path=None 恢复按角色解析。"""
    global _OVERRIDE
    _OVERRIDE = path
    return _path()


def _path():
    return _OVERRIDE or persona.self_state_path()


def _load():
    path = _path()
    if path in _CACHE:
        return _CACHE[path]
    data = _empty()
    try:
        with open(path, encoding='utf-8') as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = None
    if isinstance(saved, dict):
        for key, default in data.items():
            val = saved.get(key)
            if isinstance(val, type(default)):
                data[key] = val
    _CACHE[path] = data
    return data


def _empty():
    return {'version': 1, 'date': '', 'items': [], 'seq': 0, 'source': '',
            'diary': [], 'curiosity': [], 'log': [], 'history': [],
            'refresh_day': ''}


def _save(data=None):
    data = data if data is not None else _load()
    path = _path()
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        LOG.exception('自我状态写盘失败：%s', path)
        return False


def _today(now=None):
    ts = datetime.datetime.fromtimestamp(now) if now is not None else datetime.datetime.now()
    return ts.strftime('%Y-%m-%d')


def _yesterday(now=None):
    ts = datetime.datetime.fromtimestamp(now) if now is not None else datetime.datetime.now()
    return (ts - datetime.timedelta(days=1)).strftime('%Y-%m-%d')


def _days_ago(now, days):
    ts = datetime.datetime.fromtimestamp(now) if now is not None else datetime.datetime.now()
    return (ts - datetime.timedelta(days=days)).strftime('%Y-%m-%d')


def _small(text, limit):
    t = re.sub(r'\s+', ' ', str(text or '')).strip()
    return t[:limit]


# ---------- 日程 ----------

def _assign_ids(data, items):
    base = int(data.get('seq') or 0)
    for item in items:
        base += 1
        item['id'] = str(base)
    data['seq'] = base
    return items


def _seed_locked(data, now):
    """当天还没有条目时：用 daily_scene 的 plan；没有就按角色卡固定作息兜底。"""
    if data.get('items'):
        return data['items']
    plan = []
    try:
        plan = daily_scene.get_plan()
    except Exception:
        LOG.exception('读取今日日程失败（按固定作息兜底）')
    items = day_life.normalize_items(plan, source='plan')
    source = 'plan' if any(i.get('source') == 'plan' for i in items) else ''
    if not items:
        items = day_life.normalize_items(day_life.routine_items(), source='routine')
        source = 'routine' if items else ''
    _assign_ids(data, items)
    data['items'] = items
    data['source'] = source
    if items:
        _save(data)
    return items


def ensure_day(now=None):
    """日期变了就归档前一天日程；返回是否发生了跨天。"""
    now = time.time() if now is None else float(now)
    today = _today(now)
    with _LOCK:
        data = _load()
        if data.get('date') == today:
            return False
        old_date = data.get('date') or ''
        if old_date and data.get('items'):
            data['history'] = (data.get('history') or [])[-(cfg.SELF_HISTORY_KEEP - 1):]
            data['history'].append({'date': old_date, 'items': data['items']})
        data['date'] = today
        data['items'] = []
        data['source'] = ''
        data['log'] = [x for x in (data.get('log') or []) if isinstance(x, dict)]
        data['log'] = data['log'][-cfg.SELF_LOG_KEEP:]
        _prune(data, now)
        _save(data)
        return True


def items(now=None):
    """当天日程（可为空）。只读，不触发写盘。"""
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load()
        if data.get('date') == _today(now) and data.get('items'):
            return [dict(i) for i in data['items']]
    rows = []
    try:
        rows = daily_scene.get_plan()
    except Exception:
        pass
    rows = day_life.normalize_items(rows, source='plan')
    return rows or day_life.normalize_items(day_life.routine_items(), source='routine')


def sync_plan(now=None):
    """ticker 用：daily_scene 的正式日程到了，替换掉早上按固定作息兜底的条目。

    只要日程里已经有判官改过的条目就不再覆盖（对话已经把它改成了她真实的一天）。
    """
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load()
        ensure_day(now)
        data = _load()
        if any(i.get('source') == 'judge' for i in data.get('items') or []):
            return False
        if data.get('source') == 'plan':
            return False
        plan = []
        try:
            plan = daily_scene.get_plan()
        except Exception:
            return False
        if not any(i.get('source') == 'plan' for i in plan):
            return False
        fresh = day_life.normalize_items(plan, source='plan')
        _assign_ids(data, fresh)
        data['items'] = fresh
        data['source'] = 'plan'
        _save(data)
        return True


def _items_text(rows):
    lines = []
    for row in rows or []:
        lines.append('%s | %s | %s | %s' % (
            row.get('id'), row.get('time'), row.get('text'),
            'key' if row.get('key') else '-'))
    return '\n'.join(lines) or '（今天还没有排日程）'


def apply_ops(data, ops, now=None):
    """把判官的增删改落到 data['items']；返回 (改动数, 实际应用的 op 列表)。

    校验很保守：只认 add/remove/update，时间必须合法，文本长度受限；
    找不到 id、重复条目、超出条数上限的一律忽略。
    """
    rows = list(data.get('items') or [])
    cap = getattr(cfg, 'SELF_PLAN_MAX_ITEMS', 12)
    cur = day_life.minutes(day_life.clock(now)) if now is not None else None
    applied = []
    for op in (ops or [])[:cfg.SELF_ADJUST_MAX_OPS]:
        if not isinstance(op, dict):
            continue
        kind = str(op.get('op') or '').lower()
        if kind == 'add':
            time_s = day_life.normalize_time(op.get('time'))
            text = day_life.clean_text(op.get('text'))
            if not time_s or not text or len(rows) >= cap:
                continue
            if cur is not None and day_life.minutes(time_s) < cur:
                continue
            if any(i.get('text') == text for i in rows):
                continue
            data['seq'] = int(data.get('seq') or 0) + 1
            item = {'id': str(data['seq']), 'time': time_s, 'text': text,
                    'key': bool(op.get('key')), 'source': 'judge'}
            rows.append(item)
            applied.append(dict(op, id=item['id']))
        elif kind == 'remove':
            target = str(op.get('id') or '').strip()
            hit = next((i for i in rows if str(i.get('id')) == target), None)
            if hit is None:
                continue
            rows.remove(hit)
            applied.append(dict(op))
        elif kind == 'update':
            target = str(op.get('id') or '').strip()
            hit = next((i for i in rows if str(i.get('id')) == target), None)
            if hit is None:
                continue
            time_s = day_life.normalize_time(op.get('time')) if op.get('time') else hit['time']
            text = day_life.clean_text(op.get('text')) if op.get('text') else hit['text']
            if not time_s or not text:
                continue
            if cur is not None and op.get('time') and day_life.minutes(time_s) < cur:
                continue
            hit.update({'time': time_s, 'text': text,
                        'key': bool(op.get('key')) if 'key' in op else hit.get('key', False),
                        'source': 'judge'})
            applied.append(dict(op))
    rows.sort(key=lambda i: day_life.minutes(i.get('time')) or 0)
    data['items'] = rows[:cap]
    return len(applied), applied


# ---------- 注入 ----------

def _latest_diary(data, now):
    cutoff = _days_ago(now, 2)
    for row in reversed(data.get('diary') or []):
        date = str(row.get('date') or '')
        if date and row.get('text') and date >= cutoff:
            return row
    return None


def _eligible_curiosity(data, now):
    ttl = cfg.SELF_CURIOSITY_TTL_DAYS * 86400
    gap = cfg.SELF_CURIOSITY_GAP_SEC
    out = []
    for c in data.get('curiosity') or []:
        if not c.get('text'):
            continue
        ts = float(c.get('ts') or 0)
        if ts and now - ts > ttl:
            continue
        asked = float(c.get('asked_ts') or 0)
        if asked and now - asked < gap:
            continue
        out.append(c)
    out.sort(key=lambda c: (1 if c.get('asked_ts') else 0, -float(c.get('ts') or 0)))
    return out[0] if out else None


def block(uid=None, private=False, now=None):
    """【今天的安排】注入块。只读：不建状态、不写盘，拿不到就返回 ''。

    private=True 时才带日记与好奇清单；群聊只放日程，避免把私人心事带进群里。
    """
    if not day_life.enabled():
        return ''
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load()
    rows = []
    if data.get('date') == _today(now):
        rows = [dict(i) for i in data.get('items') or []]
    if not rows:
        try:
            rows = daily_scene.get_plan()
        except Exception:
            rows = []
    if not rows:
        rows = day_life.routine_items()

    cur_clock = day_life.clock(now)
    cur_min = day_life.minutes(cur_clock)
    now_item = day_life.current_item(rows, now) if rows else None
    upcoming = [i for i in rows
                if (day_life.minutes(i.get('time')) or 0) > (cur_min or 0)]
    key_item = next((i for i in upcoming if i.get('key')), None)

    lines = [cfg.SELF_HEADER]
    first_min = day_life.minutes(rows[0].get('time')) if rows else None
    if now_item and (first_min is None or first_min <= (cur_min or 0)):
        lines.append('现在（%s）：%s' % (cur_clock, now_item.get('text')))
    elif now_item:
        lines.append('今天：%s %s' % (now_item.get('time'), now_item.get('text')))
    if key_item and key_item is not now_item:
        lines.append('记着的安排：%s %s' % (key_item.get('time'), key_item.get('text')))
    elif upcoming and upcoming[0] is not now_item:
        lines.append('接下来：%s %s' % (upcoming[0].get('time'), upcoming[0].get('text')))

    if private:
        diary = _latest_diary(data, now)
        if diary:
            lines.append('她自己写下的（%s）：%s'
                         % (str(diary.get('date') or '')[5:], diary.get('text')))
        thought = _eligible_curiosity(data, now)
        if thought:
            lines.append('她心里还惦记着：%s' % thought.get('text'))
    if len(lines) == 1:
        return ''
    lines.append(cfg.SELF_BLOCK_NOTE)
    return '\n'.join(lines)


# ---------- 事件日志 / 判官调整 ----------

def _audit(entry):
    path = os.path.join(os.path.dirname(_path()) or '.', 'self_state_log.jsonl')
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except (OSError, ValueError):
        pass


def _parse_ops(raw):
    text = (raw or '').strip()
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return []
    ops = obj.get('ops') if isinstance(obj, dict) else None
    if not isinstance(ops, list):
        return []
    return [o for o in ops if isinstance(o, dict)
            and str(o.get('op') or '').lower() in ('add', 'remove', 'update')]


def _judge(prompt, model_key):
    import memory_card as mem
    import rp_core as core
    kind, model_id, think = core.resolve_model(model_key)
    return mem._gen_llm(prompt, model=model_id, think=think, channel=kind,
                        num_predict=220, profile=core.model_profile(model_key))


def adjust(user_text, reply, episode, model_key=None, uid=None, now=None):
    """判官按本轮对话调整今天的日程。返回 {changed, ops, raw?}；失败不抛给调用方。"""
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load()
        ensure_day(now)
        data = _load()
        _seed_locked(data, now)
        rows = [dict(i) for i in data.get('items') or []]
    episode_line = ('（本轮记下的事：%s）' % _small(episode, 80)) if episode else ''
    prompt = cfg.SELF_ADJUST_PROMPT.format(
        name=persona.display_name(),
        clock=day_life.clock(now),
        items=_items_text(rows),
        user=_small(user_text, 120) or '（无）',
        reply=_small(reply, 120) or '（无）',
        episode=episode_line,
        max_ops=cfg.SELF_ADJUST_MAX_OPS)
    raw = _judge(prompt, model_key) or ''
    ops = _parse_ops(raw)
    if not ops:
        return {'changed': 0, 'ops': [], 'raw': _small(raw, 80)}
    with _LOCK:
        data = _load()
        if data.get('date') != _today(now):
            return {'changed': 0, 'ops': [], 'raw': 'day_changed'}
        changed, applied = apply_ops(data, ops, now)
        if changed:
            _save(data)
    if changed:
        _audit({'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'uid': str(uid) if uid else None, 'episode': _small(episode, 80),
                'ops': applied})
    return {'changed': changed, 'ops': applied, 'raw': _small(raw, 80)}


def note_turn(uid, user_text, reply, info=None, model_key=None,
              speaker='', is_group=False, now=None):
    """回复后记录一条事件日志；私聊里出现记忆事件时交给判官调整日程。

    日志只保留摘要（事实发生什么），日记生成时作为素材；群聊与私聊都记，
    但日程调整只走私聊——群聊里每轮每个人都改一次日程没有意义。
    """
    info = info or {}
    now = time.time() if now is None else float(now)
    episode = _small(info.get('episode'), 80)
    summary = episode or ('%s → %s' % (_small(user_text, 40), _small(reply, 50)))
    if not summary.strip(' →'):
        return
    entry = {'date': _today(now), 'time': day_life.clock(now), 'summary': summary,
             'importance': int(info.get('importance') or 0),
             'uid': str(uid) if uid else '', 'speaker': _small(speaker, 20),
             'group': bool(is_group)}
    with _LOCK:
        data = _load()
        ensure_day(now)
        data = _load()
        data['log'] = (data.get('log') or [])[-cfg.SELF_LOG_KEEP + 1:] + [entry]
        _save(data)
    if not is_group and episode and model_key:
        try:
            res = adjust(user_text, reply, episode, model_key=model_key, uid=uid, now=now)
            if res.get('changed'):
                LOG.info('日程被对话调整 %d 处：%s', res['changed'], res['ops'])
        except Exception:
            LOG.exception('日程调整失败（不影响记忆）')


# ---------- 日记 / 好奇清单 ----------

def _prune(data, now):
    ttl = cfg.SELF_CURIOSITY_TTL_DAYS * 86400
    diary = [d for d in (data.get('diary') or [])
             if isinstance(d, dict) and d.get('date') and d.get('text')]
    data['diary'] = diary[-cfg.SELF_DIARY_KEEP:]
    curious = []
    for c in data.get('curiosity') or []:
        if not isinstance(c, dict) or not c.get('text'):
            continue
        if float(c.get('ts') or 0) and now - float(c.get('ts') or 0) > ttl:
            continue
        curious.append(c)
    data['curiosity'] = curious[-cfg.SELF_CURIOSITY_MAX:]


def next_curiosity(now=None):
    """挑一条还没问过（或问过超过 6 小时）的好奇；只读，不标记已问。"""
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load()
    hit = _eligible_curiosity(data, now)
    return dict(hit) if hit else None


def mark_asked(curiosity_id, now=None):
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load()
        for c in data.get('curiosity') or []:
            if str(c.get('id')) == str(curiosity_id):
                c['asked_ts'] = now
                _save(data)
                return True
    return False


def _parse_diary(raw):
    text = (raw or '').strip()
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return '', []
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return '', []
    if not isinstance(obj, dict):
        return '', []
    diary = _small(obj.get('diary'), 400)
    rows = obj.get('curiosity') if isinstance(obj.get('curiosity'), list) else []
    curious = []
    for row in rows:
        text = day_life.clean_text(row) if isinstance(row, str) else ''
        if text and text not in curious:
            curious.append(text)
    return diary, curious


def _chat(prompt):
    from rp_common import chat_api
    try:
        import runtime_config as rc
        key = rc.get('gen_model') or cfg.DAILY_MODEL
    except Exception:
        key = cfg.DAILY_MODEL
    kind, model = cfg.model_of(key)
    return chat_api([{'role': 'system', 'content': '只输出 JSON。'},
                     {'role': 'user', 'content': prompt}],
                    kind, model,
                    temperature=cfg.SELF_DIARY_TEMPERATURE,
                    max_tokens=cfg.SELF_DIARY_MAX_TOKENS)


def refresh(now=None):
    """跨天后写日记、补好奇清单。返回是否真的生成（失败置冷却，避免 ticker 反复打 API）。"""
    global _FAIL_UNTIL
    now = time.time() if now is None else float(now)
    today = _today(now)
    with _LOCK:
        data = _load()
        ensure_day(now)
        data = _load()
        if data.get('refresh_day') == today:
            return False
        _seed_locked(data, now)
        history = data.get('history') or []
        prev = history[-1] if history else None
        logs = [x for x in (data.get('log') or [])
                if isinstance(x, dict) and x.get('date') in (today, _yesterday(now))]
        if not (prev or logs or daily_scene.get_cached()):
            return False
        yesterday = _yesterday(now)
        plan_rows = (prev or {}).get('items') or data.get('items') or []
        plan_text = '\n'.join('· %s %s' % (i.get('time'), i.get('text'))
                              for i in plan_rows) or '（没有留下记录）'
        log_text = '\n'.join('· %s %s %s' % (x.get('date', '')[5:], x.get('time', ''),
                                             x.get('summary', ''))
                             for x in logs[-12:]) or '（没有留下记录）'
        ex = card_source.load().get('static', {}).get('Examples') or []
        voice = '\n'.join('· ' + str(x) for x in ex) or '（没有示例）'
        routine = '\n'.join('· ' + x for x in day_life.routine()) or '（没有固定作息）'
        habits = '；'.join(day_life.habits()) or '（没有特别记录）'
        prompt = cfg.SELF_DIARY_PROMPT.format(
            name=persona.display_name(), voice=voice, routine=routine,
            plan=plan_text, log=log_text, habits=habits)
    raw = _chat(prompt) or ''
    diary, curious = _parse_diary(raw)
    if not diary and not curious:
        with _FAIL_LOCK:
            _FAIL_UNTIL = time.time() + cfg.SELF_RETRY_SEC
        LOG.warning('日记/好奇生成无有效内容：%s', _small(raw, 80))
        return False
    with _LOCK:
        data = _load()
        if data.get('date') != today:
            return False
        if diary:
            date = (prev or {}).get('date') or yesterday
            data['diary'] = [d for d in (data.get('diary') or [])
                             if d.get('date') != date]
            data['diary'].append({'date': date, 'text': diary})
        known = {str(c.get('text')) for c in (data.get('curiosity') or [])}
        for text in curious:
            if text in known:
                continue
            data['seq'] = int(data.get('seq') or 0) + 1
            data['curiosity'].append({
                'id': 'c%s' % data['seq'], 'text': text, 'ts': now, 'asked_ts': 0})
            known.add(text)
        data['refresh_day'] = today
        _prune(data, now)
        _save(data)
    _audit({'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'kind': 'diary', 'date': today, 'diary': diary,
            'curiosity': curious})
    LOG.info('日记与好奇清单已更新（日记 %d 字，好奇 %d 条）', len(diary), len(curious))
    return True


def refresh_async():
    """ticker 每轮调用：跨天或当天还没生成日记时，后台补一次。"""
    if not day_life.enabled():
        return False
    now = time.time()
    data = _load()
    if data.get('date') == _today(now) and data.get('refresh_day') == _today(now):
        return False
    with _FAIL_LOCK:
        if now < _FAIL_UNTIL:
            return False
    if _REFRESH_LOCK.locked():
        return False
    pid = persona.current()

    def run():
        if not _REFRESH_LOCK.acquire(False):
            return
        try:
            with persona.context(pid):
                sync_plan(now)
                refresh(now)
        except Exception:
            LOG.exception('日记/好奇清单刷新失败')
            with _FAIL_LOCK:
                globals()['_FAIL_UNTIL'] = time.time() + cfg.SELF_RETRY_SEC
        finally:
            _REFRESH_LOCK.release()

    threading.Thread(target=run, name='self-state', daemon=True).start()
    return True
