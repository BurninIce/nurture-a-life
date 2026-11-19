# -*- coding: utf-8 -*-
"""群成员名册：让「没在最近发言者里」的群友也能被认出来。

问题（2026-11-02 线上实测）：成员块只取内存里的最近 8 位发言者，桥接一重启就清空。
用户问「群友是谁」时，群友明明有成员卡（state/members/10000002/），
角色却答「我不认识这个人」；她也压根没有 @ 人的能力。

数据来源：NapCat 的 get_group_member_list（OneBot v11，反向 WS 上同步调用）。
落盘 state/groups/<gid>/roster.json，TTL 内直接用缓存，避免每条消息都问一遍。
拿不到名册时退化为 group_scene 里登记过的发言人（已持久化，跨重启保留）。

只做「这人在不在本群、叫什么」这一件事；人物画像仍由 member_card 负责。
"""
import datetime
import json
import os
import threading
import time

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROOT = os.path.join(ROOT_DIR, 'state', 'groups')
_ROOT = DEFAULT_ROOT

FRESH_SEC = 600          # 名册多久算过期（每条消息最多一次同步刷新/10 分钟）
RETRY_SEC = 60           # 拉取失败后多久再试，避免每条消息都卡在超时上
NAME_CACHE_SEC = 60      # 成员 → 名字形式 的进程内缓存

_cache = {}       # gid -> data
_name_cache = {}  # gid -> (ts, [(uid, roster_nick, forms)])
_others_cache = {}  # (gid, excluded) -> (ts, [{'uid','nickname',...}])
_sync_try = {}    # gid -> 上次同步尝试时间
_bg = {}          # gid -> 上次后台刷新时间
_bg_lock = threading.Lock()

_NAME_FIELDS = ('nickname', 'alias', 'call_name', 'identity')


def set_root(path=None):
    """切换名册根目录（离线模拟/测试隔离用）。"""
    global _ROOT
    _ROOT = path or DEFAULT_ROOT
    _cache.clear()
    _name_cache.clear()
    _others_cache.clear()
    _sync_try.clear()
    _bg.clear()
    return _ROOT


def group_dir(gid):
    return os.path.join(_ROOT, str(gid))


def path_for(gid):
    return os.path.join(group_dir(gid), 'roster.json')


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M')


def _num(v):
    s = str(v)
    return int(s) if s.isdigit() else v


def load(gid):
    gid = str(gid)
    if gid in _cache:
        return _cache[gid]
    data = {'gid': gid, 'updated_ts': 0, 'updated': '', 'members': []}
    try:
        with open(path_for(gid), encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict) and isinstance(raw.get('members'), list):
            data.update({
                'updated_ts': float(raw.get('updated_ts') or 0),
                'updated': str(raw.get('updated') or ''),
                'members': [m for m in raw['members'] if isinstance(m, dict)],
            })
    except (OSError, ValueError):
        pass
    _cache[gid] = data
    return data


def save(data):
    gid = str(data.get('gid') or '')
    os.makedirs(group_dir(gid), exist_ok=True)
    dst = path_for(gid)
    tmp = dst + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, dst)
    _cache[gid] = data
    _name_cache.pop(gid, None)
    for k in [k for k in _others_cache if k[0] == gid]:
        _others_cache.pop(k, None)
    return dst


def _norm_members(raw):
    out, seen = [], set()
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        uid = str(it.get('user_id') or it.get('uid') or '').strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        card = str(it.get('card') or '').strip()
        nick = card or str(it.get('nickname') or '').strip()
        out.append({'uid': uid, 'nickname': nick[:40], 'card': card[:40],
                    'qq_nick': str(it.get('nickname') or '').strip()[:40]})
    return out


def refresh(gid, link=None, force=False):
    """向 NapCat 要一次本群成员列表；拿不到就原样返回旧名册。"""
    gid = str(gid)
    data = load(gid)
    now = time.time()
    if not force and data.get('members') and now - (data.get('updated_ts') or 0) < FRESH_SEC:
        return data
    if link is None or not hasattr(link, 'call_api'):
        return data
    try:
        raw = link.call_api('get_group_member_list', {'group_id': _num(gid)}, timeout=6)
    except TypeError:
        try:
            raw = link.call_api('get_group_member_list', {'group_id': _num(gid)})
        except Exception:
            return data
    except Exception:
        return data
    members = _norm_members(raw) if isinstance(raw, list) else []
    if not members:
        return data
    new = {'gid': gid, 'updated_ts': now, 'updated': _now(), 'members': members}
    save(new)
    return new


def _refresh_worker(gid, link):
    try:
        refresh(gid, link, force=True)
    except Exception:
        pass


def refresh_async(gid, link=None, min_gap=RETRY_SEC):
    """后台拉一次名册，不阻塞回复线程。"""
    gid = str(gid)
    now = time.time()
    with _bg_lock:
        if now - _bg.get(gid, 0) < min_gap:
            return
        _bg[gid] = now
    threading.Thread(target=_refresh_worker, args=(gid, link),
                     name='roster-%s' % gid, daemon=True).start()


def ensure(gid, link=None):
    """取本群名册：过期就刷新。首次没有名册时同步拉一次（本地 WS，几十毫秒），
    让当前这条消息就能认出人；已经有名册则只在后台更新，不拖慢回复。"""
    gid = str(gid)
    now = time.time()
    data = load(gid)
    if data.get('members') and now - (data.get('updated_ts') or 0) < FRESH_SEC:
        return data
    if now - _sync_try.get(gid, 0) >= RETRY_SEC:
        _sync_try[gid] = now
        fresh = refresh(gid, link, force=True)
        if fresh.get('members'):
            return fresh
        return data
    refresh_async(gid, link)
    return data


def _scene_members(gid):
    try:
        import group_scene
        return group_scene.load(gid).get('members') or {}
    except Exception:
        return {}


def _card_members(gid):
    """兜底的「在本群说过话的人」：member_card.touch 会把 G<群号> 记进 tags。

    名册 API 拿不到时（NapCat 不支持/超时），这条能保证重启后仍认识本群老人——
    2026-11-02 事故里群友的成员卡一直带着 G1000000，却没被任何注入路径读到。
    """
    tag = 'G%s' % str(gid)
    try:
        import member_card
        root = member_card.users_root()
        dirs = os.listdir(root)
    except Exception:
        return []
    out = []
    for d in dirs:
        try:
            with open(os.path.join(root, d, 'member.json'), encoding='utf-8') as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(m, dict) or tag not in (m.get('tags') or []):
            continue
        uid = str(m.get('uid') or d).strip()
        if uid:
            out.append({'uid': uid, 'nickname': str(m.get('nickname') or ''),
                        'card': '', 'qq_nick': ''})
    return out


def candidates(gid):
    """本群已知成员 [{'uid','nickname'}]：名册 > 气泡 > 成员卡标签。"""
    out, seen = [], set()
    for m in load(gid).get('members') or []:
        uid = str(m.get('uid') or '').strip()
        if uid and uid not in seen:
            seen.add(uid)
            out.append({'uid': uid, 'nickname': str(m.get('nickname') or '')})
    for uid, m in _scene_members(gid).items():
        uid = str(uid).strip()
        if uid and uid not in seen:
            seen.add(uid)
            out.append({'uid': uid, 'nickname': str((m or {}).get('nickname') or '')})
    for m in _card_members(gid):
        uid = m['uid']
        if uid not in seen:
            seen.add(uid)
            out.append({'uid': uid, 'nickname': m['nickname']})
    return out


def _relationship_score(uid, member):
    """关系程度排序键（越大越熟），供 known_members 使用。

    档位与三值来自**当前角色**对这个人的记忆卡（state/personas/<id>/users/<uid>）。
    没互动过的人不拿默认卡的「相识/30/30」冒充关系，一律按陌生处理。
    """
    rank = stats = interactions = 0
    try:
        import memory_card
        import persona
        path = os.path.join(persona.user_dir(uid), 'memory_card.json')
        if os.path.isfile(path):
            # 先判存在再进 scope：state_scope 会建目录，不能给整群建空档案
            with memory_card.state_scope(os.path.dirname(path)):
                data = memory_card.load()
            interactions = int(data.get('interaction_count') or 0)
            card = data.get('card') or {}
            if interactions > 0:
                rel = str(card.get('relationship') or '')
                if rel in memory_card.RELATIONSHIPS:
                    rank = memory_card.RELATIONSHIPS.index(rel)
                stats = sum(int(card.get(k) or 0)
                            for k in ('affection', 'trust', 'familiarity'))
    except Exception:
        pass
    return (rank, stats, 1 if member.get('promoted') else 0,
            interactions, int(member.get('msg_count') or 0),
            str(member.get('last_seen') or ''))


def known_members(gid, exclude=(), limit=None):
    """本群已知成员 [{'uid','nickname','card'}]，按与她的关系程度排序。

    供成员块分层注入：靠前的展开成完整一行，其余只并成一行名字。
    关系程度 = 当前角色的关系档位/三值 > 晋升 > 互动次数 > 发言数 > 最近互动；
    没互动过的人按陌生排在后面（2026-11-08：没在本轮说话的群友不再被忘掉）。
    """
    import member_card
    import persona
    gid = str(gid)
    try:
        pid = persona.current()
    except Exception:
        pid = ''
    skip = tuple(sorted(str(u) for u in (exclude or ())))
    key = (gid, pid, skip)
    cached = _others_cache.get(key)
    now = time.time()
    if cached and now - cached[0] < NAME_CACHE_SEC:
        out = cached[1]
        return out[:limit] if limit else out
    bucket = set(skip)
    items = []
    for m in candidates(gid):
        uid = str(m.get('uid') or '').strip()
        if not uid or uid in bucket:
            continue
        card = member_card.load(uid)
        # 没卡的人 load 出来的昵称就是 uid，必须退回名册昵称
        card_nick = str(card.get('nickname') or '').strip()
        if not card_nick or card_nick == uid:
            card_nick = ''
        nick = card_nick or str(m.get('nickname') or '').strip()
        # 昵称就是 QQ 号的名册条目没有称呼价值，报出来只是噪声
        if not nick or nick == uid:
            continue
        items.append({'uid': uid, 'nickname': nick, 'card': card,
                      '_score': _relationship_score(uid, card)})
    items.sort(key=lambda x: x['_score'], reverse=True)
    out = [{'uid': x['uid'], 'nickname': x['nickname'], 'card': x['card']}
           for x in items]
    _others_cache[key] = (now, out)
    return out[:limit] if limit else out


def _forms(uid, roster_entry):
    """一个成员的全等价名字：群名片/群昵称 + 成员卡里的昵称/曾用名/她起的称呼/本名。"""
    names = [roster_entry.get('nickname'), roster_entry.get('card'),
             roster_entry.get('qq_nick')]
    try:
        import member_card
        m = member_card.load(uid)
        names += [m.get(k) for k in _NAME_FIELDS]
    except Exception:
        pass
    out, seen = [], set()
    for n in names:
        n = str(n or '').strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _table(gid):
    """gid → [(uid, nickname, forms)]，带进程内缓存（成员卡每 60 秒最多读一遍）。"""
    gid = str(gid)
    cached = _name_cache.get(gid)
    now = time.time()
    if cached and now - cached[0] < NAME_CACHE_SEC:
        return cached[1]
    table = []
    for m in load(gid).get('members') or []:
        uid = str(m.get('uid') or '')
        if uid:
            table.append((uid, str(m.get('nickname') or ''),
                          _forms(uid, m)))
    # group_scene 的兜底成员（名册没拿到时至少还有最近发言人）
    have = {uid for uid, _, _ in table}
    for uid, m in _scene_members(gid).items():
        uid = str(uid)
        if uid and uid not in have:
            nick = str((m or {}).get('nickname') or '')
            table.append((uid, nick, _forms(uid, {'nickname': nick})))
            have.add(uid)
    # 成员卡标签兜底：在本群说过话、但名册和气泡都没覆盖到的人
    for m in _card_members(gid):
        uid = m['uid']
        if uid and uid not in have:
            table.append((uid, m['nickname'], _forms(uid, m)))
            have.add(uid)
    _name_cache[gid] = (now, table)
    return table


def match_text(gid, text):
    """从一句话里找出本群成员（任一名字形式出现在文本里）。

    名字形式包含成员卡的 call_name / identity：群里问的是「群友」，
    而名册上的群名片可能叫「肌霸群友（…）」，只比全名会漏。
    """
    text = text or ''
    if not text:
        return []
    out, seen = [], set()
    for uid, nick, forms in _table(gid):
        if uid in seen:
            continue
        hit = any(len(f) >= 2 and f in text for f in forms)
        if not hit:
            hit = ('@' + uid) in text or ('＠' + uid) in text
        if hit:
            seen.add(uid)
            out.append({'uid': uid, 'nickname': nick or uid})
    return out


def name_map(gid):
    """名字形式 → uid，供回复里的 <@名字> 解析（长名字优先由调用方排序）。"""
    mp = {}
    for uid, _nick, forms in _table(gid):
        for f in forms:
            if len(f) >= 2 and f not in mp:
                mp[f] = uid
    return mp


def display_name(gid, uid):
    """uid → 展示名（回复里的 @ 标记转展示文本用）。成员卡昵称优先，群名片兜底。"""
    uid = str(uid or '')
    if not uid:
        return ''
    try:
        import member_card
        nick = str((member_card.load(uid).get('nickname') or '').strip())
        if nick and nick != uid:
            return nick
    except Exception:
        pass
    for u, nick, forms in _table(gid):
        if u == uid:
            return nick or (forms[0] if forms else uid)
    return ''


def uids(gid):
    return {uid for uid, _nick, _forms_ in _table(gid)}


def _selftest():
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix='rostertest_')
    set_root(tmp)
    gid = '123456'
    calls = []

    class FakeLink(object):
        def __init__(self, data):
            self.data = data

        def call_api(self, action, params=None, timeout=8):
            calls.append((action, params))
            return self.data

    check = lambda name, ok, detail='': print(
        '%s %s%s' % ('[PASS]' if ok else '[FAIL]', name,
                     ('  → ' + str(detail)[:80]) if detail else ''))
    raw = [{'user_id': 1001, 'nickname': '群友A', 'card': ''},
           {'user_id': 1002, 'nickname': '群友C', 'card': '群友C澈'},
           {'user_id': 1003, 'nickname': '群友B', 'card': ''}]
    data = refresh(gid, FakeLink(raw), force=True)
    check('1 拉取名册并落盘', len(data['members']) == 3 and os.path.isfile(path_for(gid)))
    check('2 群名片优先', load(gid)['members'][1]['nickname'] == '群友C澈')
    check('3 命中成员', [m['uid'] for m in match_text(gid, '群友C澈今天来了吗')] == ['1002'])
    check('4 不误伤', match_text(gid, '今天好累') == [])
    check('5 名册 + scene 兜底去重',
          [m['uid'] for m in candidates(gid)] == ['1001', '1002', '1003'])
    check('6 uid 集合', uids(gid) == {'1001', '1002', '1003'})
    check('7 名字映射', name_map(gid).get('群友A') == '1001')
    check('8 拉取失败保留旧名册', refresh(gid, FakeLink(None), force=True)['members'])
    check('9 过期判定随时间', ensure(gid, FakeLink(None)) is not None)
    set_root()
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    _selftest()
