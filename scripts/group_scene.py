# -*- coding: utf-8 -*-
"""群聊气泡（群体层）：低分辨率群体认知，与个人层（member/memory 卡）互补。

设计边界：
- 群体层只承载**低分辨率、跨人**的认知：整体氛围 + 无人称的「最近发生过什么」
  + 每个活跃群友一条「只记风格、不记言论」的模糊画像。
- **职责边界（用户明确）**：关键事件、关键发言由**记忆卡**（memory_card.json）
  负责（逐人、精确、事件溯源）。群体层**不重复记录具体发言**，只记「这人什么脾性」。
- **风格抽取输入铁律**：members[uid].style 的输入必须来自**按 uid 隔离的个人数据**，
  绝不靠「让模型在群里认这句话是谁说的」来猜归属（多人称场景模型会认错人）。

存储：state/groups/<gid>/scene.json（与 activity.json 同目录）。
本模块只做存取，不做归纳——风格归纳/氛围总结由 rp_core 挂判官模型调用本模块写回。
"""
import datetime
import json
import os

import rp_config as cfg

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROOT = os.path.join(ROOT_DIR, 'state', 'groups')
_ROOT = DEFAULT_ROOT

_cache = {}       # gid -> data（进程内缓存）
_dirty = set()    # 已改动、待落盘的 gid

# 上限（滚动覆盖）
MAX_EVENTS = 20
MAX_MEMBERS = 8


def set_root(path=None):
    """切换群体层根目录（离线模拟/测试隔离用）。"""
    global _ROOT
    _ROOT = path or DEFAULT_ROOT
    _cache.clear()
    _dirty.clear()
    return _ROOT


def group_dir(gid):
    return os.path.join(_ROOT, str(gid))


def path_for(gid):
    return os.path.join(group_dir(gid), 'scene.json')


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M')


def _empty(gid):
    return {
        'gid': str(gid),
        'version': 1,
        'scene': '',              # 无人称的整体氛围描述（如「最近在聊技术，偏抬杠」）
        'recent_events': [],      # [{summary, importance, time}]，summary 无人称
        'members': {},            # {uid: {nickname, style}}，style 只记风格不记言论
        'vibe': '',               # 活跃度一句话（如「活跃，4-5 人常聊」）
        'updated': '',
    }


def load(gid):
    gid = str(gid)
    if gid in _cache:
        return _cache[gid]
    try:
        with open(path_for(gid), encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get('members'), dict):
            data = _empty(gid)
        data.setdefault('recent_events', [])
        data.setdefault('members', {})
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
    try:
        os.makedirs(group_dir(gid), exist_ok=True)
        dst = path_for(gid)
        tmp = dst + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dst)
        _dirty.discard(gid)
        return dst
    except OSError:
        return None


def _prune(data):
    """滚动覆盖：recent_events 保留最近 MAX_EVENTS 条，members 保留最近活跃的 MAX_MEMBERS 人。"""
    evs = data.get('recent_events') or []
    if len(evs) > MAX_EVENTS:
        data['recent_events'] = evs[-MAX_EVENTS:]
    mems = data.get('members') or {}
    if len(mems) > MAX_MEMBERS:
        # 按 last_seen 保留最近活跃的；无 last_seen 的排后
        def key(uid):
            m = mems[uid]
            return (m.get('last_seen') or '') or ''
        keep = sorted(mems, key=key, reverse=True)[:MAX_MEMBERS]
        data['members'] = {k: mems[k] for k in keep}


def record_event(gid, summary, importance=1):
    """记一条**无人称**的群事件（「有人争论过 X」而非「群友A说 X」）。

    summary 必须无人称——调用方负责保证措辞，本模块不做人称检查（避免误杀合法文本）。
    """
    gid = str(gid)
    if not (summary or '').strip():
        return load(gid)
    data = load(gid)
    imp = max(1, min(3, int(importance or 1)))
    data.setdefault('recent_events', []).append({
        'summary': str(summary).strip()[:120],
        'importance': imp,
        'time': _now(),
    })
    data['updated'] = _now()
    _prune(data)
    _dirty.add(gid)
    return data


def set_member_style(gid, uid, nickname='', style=''):
    """写入/刷新某群友的风格画像（只记风格、不记言论）。

    uid 是权威锚定（QQ 号），nickname 仅作展示。style 应来自按 uid 隔离的个人数据。
    """
    gid = str(gid)
    uid = str(uid)
    if not style and not nickname:
        return load(gid)
    data = load(gid)
    mem = data.setdefault('members', {}).setdefault(uid, {})
    if nickname:
        mem['nickname'] = str(nickname).strip()[:20]
    if style:
        mem['style'] = str(style).strip()[:60]
    mem['last_seen'] = _now()
    data['updated'] = _now()
    _prune(data)
    _dirty.add(gid)
    return data


def set_vibe(gid, scene='', vibe=''):
    """写入整体氛围（无人称）与活跃度一句话。"""
    gid = str(gid)
    data = load(gid)
    if scene:
        data['scene'] = str(scene).strip()[:120]
    if vibe:
        data['vibe'] = str(vibe).strip()[:60]
    data['updated'] = _now()
    _dirty.add(gid)
    return data


def to_inject(gid):
    """产出注入 system 的群体层文本（喂 rp_core.build_system(group_scene=...)）。

    格式：氛围 + 活跃度 + 无人称事件（最多 3 条）+ 成员风格画像。
    返回 ''（无内容时不注入，不占预算）。

    只输出**有风格**的成员：members 里的纯昵称是名册兜底（group_roster 直接读
    scene.json），注入进 system 只会和「群里的人」块重复一遍名字，没有信息量。
    没发言成员的「认得人」由成员块尾部的 others 附录负责（按 uid 去重），
    不要在这里补名字，否则同一批人会以两种格式各出现一遍。
    """
    data = load(gid)
    lines = []
    if data.get('scene'):
        lines.append(data['scene'])
    if data.get('vibe'):
        lines.append(data['vibe'])
    evs = data.get('recent_events') or []
    for e in evs[-3:]:
        lines.append('- %s' % (e.get('summary') or '').strip())
    for uid, m in (data.get('members') or {}).items():
        nick = m.get('nickname') or uid
        style = (m.get('style') or '').strip()
        if style:
            lines.append('- %s：%s' % (nick, style))
    return '\n'.join(x for x in lines if x).strip()


def snippets(gid):
    """产出喂 group_gate 的 scene_snippets（群级场景文本片段列表，用于「主动开话题」匹配）。

    只取无人称的 scene + 事件 summary，**不含成员风格**（成员风格不应被拿来
    做「她该不该接这句」的相似度匹配，避免把 A 的风格安到 B 的话题上）。
    """
    data = load(gid)
    out = []
    if data.get('scene'):
        out.append(data['scene'])
    for e in (data.get('recent_events') or [])[-5:]:
        s = (e.get('summary') or '').strip()
        if s:
            out.append(s)
    return out


def flush(gid):
    """立即落盘（bridge 在归纳/写回后调用，避免缓存丢失）。"""
    return save(gid)


# ---------- 自测 ----------

def _selftest():
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix='scenetest_')
    set_root(tmp)
    gid = '1000000'

    record_event(gid, '有人争论过一个技术方案，最后不了了之', 2)
    set_vibe(gid, scene='最近群里在聊技术，氛围偏理工男抬杠', vibe='活跃，4-5 人常聊')
    set_member_style(gid, '10000001', '用户', '话多，爱抬杠，喜欢反问')
    set_member_style(gid, '10000003', '群友A', '潜水，偶尔冒泡，偏温和')
    flush(gid)

    print('=== group_scene 自测 ===')
    print('--- to_inject（注入 system）---')
    print(to_inject(gid))
    print('--- snippets（喂 gate 主动开话题）---')
    print(snippets(gid))
    print('--- 滚动覆盖：events 上限 %d / members 上限 %d ---' % (MAX_EVENTS, MAX_MEMBERS))
    for i in range(MAX_EVENTS + 3):
        record_event(gid, '第 %d 条事件' % i, 1)
    data = load(gid)
    print('events 数 =', len(data['recent_events']), '（应=%d）' % MAX_EVENTS)

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
