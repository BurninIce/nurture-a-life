# -*- coding: utf-8 -*-
"""私聊主动开话题：状态、触发判定、素材挑选与终审。

只对 runtime_config.private_allow 里的对象生效；白名单为空时完全禁用。
一次主动后如果对方没回复（pending），就不再追发，直到对方再次说话。

状态落盘 state/private_topic.json：
    {uid: {last_incoming_ts, next_due_ts, last_proactive_ts,
           proactive_day, proactive_today, pending, recent_topics, ...}}
"""
import datetime
import json
import logging
import os
import random
import re
import threading
import time

import rp_config as cfg
import persona
import runtime_config as rc

LOG = logging.getLogger('private_topic')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_PATH = os.path.join(ROOT, 'state', 'private_topic.json')

_LOCK = threading.RLock()
_PATH = DEFAULT_PATH
_OVERRIDE = None
_CACHE = {}       # path -> data
_RNG = random.Random()


# ---------- 状态 ----------

def _path():
    return _OVERRIDE or persona.private_topic_path()


def set_path(path=None):
    """切换状态文件（自测/多实例隔离用）。path=None 恢复按角色解析。"""
    global _PATH, _OVERRIDE
    with _LOCK:
        _OVERRIDE = path
        _PATH = path or DEFAULT_PATH
    return _path()


def _empty_user():
    return {
        'last_incoming_ts': 0,
        'next_due_ts': 0,
        'last_proactive_ts': 0,
        'proactive_day': '',
        'proactive_today': 0,
        'pending': False,
        'recent_topics': [],
        'last_proactive_text': '',
    }


def _load():
    path = _path()
    if path in _CACHE:
        return _CACHE[path]
    data = {'version': 1, 'users': {}}
    try:
        with open(path, encoding='utf-8') as f:
            saved = json.load(f)
        if isinstance(saved, dict) and isinstance(saved.get('users'), dict):
            for uid, st in saved['users'].items():
                if isinstance(st, dict):
                    base = _empty_user()
                    base.update(st)
                    data['users'][str(uid)] = base
    except (OSError, ValueError):
        pass
    _CACHE[path] = data
    return data


def _save():
    path = _path()
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_load(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        LOG.exception('私聊主动状态写盘失败：%s', path)
        return False


def _today(now):
    return datetime.date.fromtimestamp(now).isoformat()


def _ensure_day(st, now):
    day = _today(now)
    if st.get('proactive_day') != day:
        st['proactive_day'] = day
        st['proactive_today'] = 0


def get_state(uid):
    with _LOCK:
        return _load()['users'].get(str(uid))


def all_users():
    with _LOCK:
        return dict(_load()['users'])


def _state_or_new(uid):
    data = _load()
    uid = str(uid)
    st = data['users'].get(uid)
    if st is None:
        st = _empty_user()
        data['users'][uid] = st
    return st


# ---------- 配置 ----------

def is_enabled():
    return bool(rc.get('private_proactive_enabled', cfg.PRIVATE_PROACTIVE_ENABLED))


def _int_cfg(key, default, lo=None, hi=None):
    try:
        v = int(rc.get(key, default))
    except (TypeError, ValueError):
        v = int(default)
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _idle_range():
    lo = _int_cfg('private_proactive_idle_min', cfg.PRIVATE_PROACTIVE_IDLE_MIN, 60)
    hi = _int_cfg('private_proactive_idle_max', cfg.PRIVATE_PROACTIVE_IDLE_MAX, 60)
    if hi < lo:
        hi = lo
    return lo, hi


def _active_window():
    lo = _int_cfg('private_proactive_hour_start',
                  cfg.PRIVATE_PROACTIVE_HOUR_START, 0, 23)
    hi = _int_cfg('private_proactive_hour_end',
                  cfg.PRIVATE_PROACTIVE_HOUR_END, 1, 24)
    if hi <= lo:
        hi = min(24, lo + 1)
    return lo, hi


def _active_hours(uid, now):
    """学习到的活跃小时；样本不足时回落到私聊自己的默认窗口。"""
    try:
        import group_activity as activity
        hours, info = activity.active_hours('p%s' % uid, now=now)
        if not info.get('source', '').startswith('default'):
            return hours, info
    except Exception:
        info = {}
    lo, hi = _active_window()
    return set(range(lo, hi)), info


# ---------- 时间线 ----------

def note_incoming(uid, ts=None, next_due=None, rng=None):
    """收到一条私聊：更新活跃时间、清 pending、重排下一次候选。"""
    now = float(ts or time.time())
    lo, hi = _idle_range()
    rng = rng or _RNG
    with _LOCK:
        st = _state_or_new(uid)
        _ensure_day(st, now)
        st['last_incoming_ts'] = max(float(st.get('last_incoming_ts') or 0), now)
        st['pending'] = False
        if next_due is None:
            next_due = now + rng.uniform(lo, hi)
        st['next_due_ts'] = float(next_due)
        _save()
        return dict(st)


def _mem_episode_ts(text):
    try:
        return time.mktime(time.strptime((text or '')[:16], '%Y-%m-%d %H:%M'))
    except (TypeError, ValueError):
        return 0


def bootstrap(uid, now=None, rng=None):
    """没有状态时，从记忆卡最近事件推断「上次聊过」，并给一个启动宽限。

    没有任何记忆事件 → 不建立状态（从没聊过的人不会被主动打扰）。
    """
    now = float(now or time.time())
    rng = rng or _RNG
    try:
        import memory_card as mem
        import rp_core as core
        with mem.state_scope(core.user_dir(uid)):
            m = mem.load()
    except Exception as e:
        LOG.debug('主动时间窗读取记忆失败（跳过）：%s', e, exc_info=True)
        return None
    times = [_mem_episode_ts(e.get('time')) for e in (m.get('episodes') or [])
             if not e.get('inactive')]
    times = [t for t in times if t > 0]
    if not times:
        return None
    grace_lo = _int_cfg('private_proactive_boot_grace_min',
                        cfg.PRIVATE_PROACTIVE_BOOT_GRACE_MIN, 0)
    grace_hi = _int_cfg('private_proactive_boot_grace_max',
                        cfg.PRIVATE_PROACTIVE_BOOT_GRACE_MAX, grace_lo)
    if grace_hi < grace_lo:
        grace_hi = grace_lo
    with _LOCK:
        st = _state_or_new(uid)
        st['last_incoming_ts'] = max(float(st.get('last_incoming_ts') or 0),
                                     max(times))
        st['next_due_ts'] = now + rng.uniform(grace_lo, grace_hi)
        _save()
        return dict(st)


def postpone(uid, now, seconds):
    with _LOCK:
        st = _state_or_new(uid)
        st['next_due_ts'] = float(now) + max(1, int(seconds))
        _save()
        return dict(st)


def mark_attempt(uid, now):
    """提交一次主动任务前调用：占掉当天配额并推迟下一次。"""
    now = float(now or time.time())
    min_gap = _int_cfg('private_proactive_min_gap',
                       cfg.PRIVATE_PROACTIVE_MIN_GAP, 0)
    with _LOCK:
        st = _state_or_new(uid)
        _ensure_day(st, now)
        st['last_proactive_ts'] = now
        st['proactive_today'] = int(st.get('proactive_today') or 0) + 1
        st['next_due_ts'] = now + max(60, min_gap)
        _save()
        return dict(st)


def mark_sent(uid, reply, seed=None, now=None):
    """消息真正发出后调用：进入 pending，并记下去重话题。"""
    now = float(now or time.time())
    keep = _int_cfg('private_proactive_recent_topics',
                    cfg.PRIVATE_PROACTIVE_RECENT_TOPICS, 1)
    with _LOCK:
        st = _state_or_new(uid)
        _ensure_day(st, now)
        st['pending'] = True
        st['last_proactive_text'] = (reply or '')[:200]
        topics = list(st.get('recent_topics') or [])
        if seed:
            topics.append({'id': seed.get('id'), 'text': (seed.get('text') or '')[:60],
                           'ts': now})
        elif reply:
            topics.append({'id': None, 'text': (reply or '')[:60], 'ts': now})
        st['recent_topics'] = topics[-keep:]
        _save()
        return dict(st)


def due(uid, now=None, rng=None):
    """是否该主动开口。返回 (bool, reason)。调用方负责白名单判断。"""
    if not is_enabled():
        return False, 'off'
    now = float(now or time.time())
    st = get_state(uid)
    if not st:
        if bootstrap(uid, now, rng=rng):
            return False, 'bootstrapped'
        return False, 'no_history'
    with _LOCK:
        st = _state_or_new(uid)
        _ensure_day(st, now)
        if st.get('pending'):
            return False, 'pending'
        last_in = float(st.get('last_incoming_ts') or 0)
        if not last_in:
            return False, 'no_incoming'
        recent_days = _int_cfg('private_proactive_recent_days',
                               cfg.PRIVATE_PROACTIVE_RECENT_DAYS, 1)
        if now - last_in > recent_days * 86400:
            return False, 'stale'
        idle_lo, _ = _idle_range()
        if now - last_in < idle_lo:
            return False, 'not_idle'
        next_due = float(st.get('next_due_ts') or 0)
        if next_due and now < next_due:
            return False, 'next_due'
        min_gap = _int_cfg('private_proactive_min_gap',
                           cfg.PRIVATE_PROACTIVE_MIN_GAP, 0)
        if now - float(st.get('last_proactive_ts') or 0) < min_gap:
            return False, 'min_gap'
        daily_max = _int_cfg('private_proactive_daily_max',
                             cfg.PRIVATE_PROACTIVE_DAILY_MAX, 0)
        if int(st.get('proactive_today') or 0) >= daily_max:
            return False, 'daily_quota'
        hour = datetime.datetime.fromtimestamp(now).hour
        hours, info = _active_hours(uid, now)
        if hour not in hours:
            return False, 'quiet_hours(%s)' % (info.get('source') or 'default')
        return True, 'ok(%s)' % (info.get('source') or 'default')


# ---------- 素材 ----------

def pick_seed(uid, mem_data, rng=None, allow_curiosity=False):
    """从记忆事件里挑一条最近、重要、且最近没主动提过的旧事。

    allow_curiosity=True 时（仅私聊主动），有一半机会改用她自己好奇清单里
    还没问过的一条——那是她当天真正惦记的事，比翻旧账更自然。
    """
    rng = rng or _RNG
    if allow_curiosity:
        try:
            import self_state
            if rng.random() < 0.5:
                thought = self_state.next_curiosity()
                if thought:
                    return {'id': 'cur-%s' % thought.get('id'),
                            'text': (thought.get('text') or '').strip()[:80],
                            'time': '', 'curiosity_id': thought.get('id')}
        except Exception:
            LOG.exception('好奇清单读取失败（回落记忆素材）')
    st = get_state(uid) or {}
    recent_ids = set()
    for t in st.get('recent_topics') or []:
        if isinstance(t, dict) and t.get('id') is not None:
            recent_ids.add(t.get('id'))
    try:
        import memory_card as mem
        dead = mem.superseded_ep_ids(mem_data or {})
    except Exception:
        dead = set()
    eps = []
    for e in (mem_data or {}).get('episodes') or []:
        if e.get('inactive') or e.get('id') in dead or e.get('id') in recent_ids:
            continue
        text = (e.get('summary') or '').strip()
        if not text:
            continue
        eps.append(e)
    if not eps:
        return None
    eps = eps[-12:]
    weights = []
    for i, e in enumerate(eps):
        try:
            imp = max(1, int(e.get('importance') or 1))
        except (TypeError, ValueError):
            imp = 1
        # 收藏事件是用户明确要求「固定保留 + 优先注入」的，主动提起时也优先
        weight = imp * (1.0 + i * 0.15) * (2.0 if e.get('favorite') else 1.0)
        weights.append(weight)
    try:
        chosen = rng.choices(eps, weights=weights, k=1)[0]
    except (AttributeError, IndexError):
        chosen = eps[-1]
    return {
        'id': chosen.get('id'),
        'text': (chosen.get('summary') or '').strip()[:80],
        # 跨零点的话题用开始时间，避免「昨晚的事」被写成今天凌晨
        'time': str(chosen.get('start_time') or chosen.get('time') or '')[:16],
    }


def build_hint(uid, seed, has_daily):
    """拼给 system 末尾的主动开口提示；没有任何素材时返回 ''。"""
    if not seed and not has_daily:
        return ''
    lines = [cfg.PRIVATE_PROACTIVE_HINT]
    if seed:
        when = seed.get('time') or ''
        header = (cfg.PRIVATE_CURIOSITY_SEED_HEADER if seed.get('curiosity_id') is not None
                  else cfg.PRIVATE_PROACTIVE_SEED_HEADER)
        lines.append(header)
        lines.append('- %s%s' % (('（%s）' % when) if when else '',
                                 seed.get('text') or ''))
    st = get_state(uid) or {}
    recent = [(t.get('text') or '').strip()
              for t in (st.get('recent_topics') or [])
              if isinstance(t, dict) and (t.get('text') or '').strip()]
    if recent:
        lines.append('最近你已经主动提过：' + '；'.join(recent[-4:])
                     + '。这次别再提这些。')
    return '\n'.join(lines)


# ---------- 终审 ----------

def _turns_text(turns, limit=8):
    lines = []
    for t in (turns or [])[-limit:]:
        if not isinstance(t, dict):
            continue
        text = (t.get('text') or '').strip()
        if not text:
            continue
        who = persona.direct_name() if t.get('self') else (t.get('nickname') or '对方')
        lines.append('%s：%s' % (who, text[:60]))
    return '\n'.join(lines) or '（暂时没有新消息）'


def make_judge(model_key=None, channel=None):
    """私聊主动开口的终审：这句话现在发过去自然吗。"""
    import memory_card as mem
    import rp_core as core

    def judge(candidate, ctx):
        kind, model_id, think = core.resolve_model(model_key)
        prompt = persona.prompt(
            'private_proactive',
            turns=_turns_text((ctx or {}).get('turns')),
            candidate=(candidate or '')[:120],
            name=persona.direct_name())
        raw = mem._gen_llm(prompt, model=model_id, think=think,
                           channel=channel or kind, num_predict=40,
                           profile=core.model_profile(model_key))
        m = re.search(r'\{.*\}', raw or '', re.S)
        if not m:
            return False, 'parse_fail'
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            return False, 'json_fail'
        return bool(obj.get('speak')), str(obj.get('why') or '')[:20]

    return judge


# ---------- 自测 ----------

def _selftest():
    import shutil
    import tempfile

    root = tempfile.mkdtemp(prefix='privtopictest_')
    old_path = _PATH
    old_data = rc._DATA
    try:
        set_path(os.path.join(root, 'private_topic.json'))
        rc._DATA = dict(rc._defaults())
        rc._DATA.update({
            'private_proactive_enabled': True,
            'private_proactive_idle_min': 600,
            'private_proactive_idle_max': 1200,
            'private_proactive_min_gap': 600,
            'private_proactive_daily_max': 1,
            'private_proactive_hour_start': 0,
            'private_proactive_hour_end': 24,
            'private_proactive_recent_days': 3,
        })
        results = []

        def check(name, ok, detail=''):
            results.append(bool(ok))
            print('%s %s%s' % ('[PASS]' if ok else '[FAIL]', name,
                               ('  → ' + str(detail)[:100]) if detail else ''))

        now = time.time()
        # 没有历史 → 不建立状态
        check('1 无记忆对象不主动', due('u0', now)[0] is False, due('u0', now))
        # 建立状态并到期
        note_incoming('u1', now - 7200, next_due=now - 1)
        ok, why = due('u1', now)
        check('2 到期且时段内 → 应主动', ok, why)
        mark_attempt('u1', now)
        mark_sent('u1', '今天看到一本旧画册', {'id': 1, 'text': '旧画册'}, now)
        ok, why = due('u1', now + 1)
        check('3 已发送未回复 → 不再追发', not ok and why == 'pending', why)
        note_incoming('u1', now + 10)
        ok, why = due('u1', now + 2000)
        check('4 回复后按每日上限拦截', not ok and why == 'daily_quota', why)
        # 素材挑选与去重
        mem_data = {'episodes': [
            {'id': 1, 'summary': '一起挑了一支钢笔', 'importance': 2,
             'time': '2026-10-30 20:00'},
            {'id': 2, 'summary': '聊到周末去旧书店', 'importance': 3,
             'time': '2026-10-31 21:00'},
        ]}
        seed = pick_seed('u1', mem_data, rng=random.Random(0))
        check('5 从记忆挑素材', bool(seed and seed.get('id')), seed)
        check('6 最近提过的话题被排除', seed.get('id') != 1, seed)
        # 白名单/开关由桥接负责；这里验证开关
        rc._DATA['private_proactive_enabled'] = False
        ok, why = due('u1', now + 100)
        check('7 总开关关闭 → 不主动', not ok and why == 'off', why)
        print('\n%d/%d passed' % (sum(results), len(results)))
        return 0 if all(results) else 1
    finally:
        rc._DATA = old_data
        set_path(old_path)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(_selftest())
