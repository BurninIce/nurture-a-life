# -*- coding: utf-8 -*-
"""群发言决策：要不要说话、什么时候主动开口。

背景（用户需求 2026-10-20）：除了被 @ 必回，角色还应当能**自然融入**群聊。
但「每条消息都让模型判断说不说」不可行——8 人群每分钟 20-40 条消息，
即使判断只要 1 秒，也会把 12GB 卡的串行队列打满，并让真正该回的那条等到几十秒后。

因此分两层（与 §11.2 的"群发言决策"对应）：
  L1 廉价信号（规则 + 向量，~0-3ms）
      被@/点名/回复她、疑问句、话题相关度、兴趣词、情绪强度、被冷落、拟人随机
      → 合成 0~1.5 的「插话冲动」分数
  L2 模型终审（**只在候选区间调用**，num_predict≈40、temp 0）
      「这句我会想接吗」→ speak: true/false

阈值：被@/点名/回复她 → 直接回（不花判断成本）；
      其余 L1 ≥GATE_CONSIDER_AT 才花一次模型调用，由**模型自己决定**要不要接；
      judge 不可用时退化为 GATE_REPLY_AT 阈值判定。

口径：L1 决定"值不值得看一眼"，L2 决定"要不要开口"，真正的"说什么"仍是模型。
"""
import datetime
import json
import logging
import os
import random
import re
import time
from collections import namedtuple

import persona
import logutil
import rp_config as cfg
import runtime_config as rc

LOG = logging.getLogger('gate')

REPLY = 'reply'
CONSIDER = 'consider'
IGNORE = 'ignore'

Decision = namedtuple('Decision', 'action score signals reason')


# ---------- 阈值按模型族 ----------

_WARNED_FAMILY = set()


def model_family(model_id='', kind=''):
    """模型 → 阈值族名。kind='local'（Ollama）/ 'localapi'（本地 API）单独分族。"""
    if kind == 'local':
        return 'local'
    if kind == 'localapi':
        return 'localapi'
    m = (model_id or '').lower()
    if not m:
        return ''
    if 'flash' in m:
        return 'qwen-flash'
    if 'max' in m or 'plus' in m or 'turbo' in m:
        return 'qwen-max'
    return ''


def gate_thresholds(model_key=None, model_id=None, kind=None):
    """按模型族取 L1 阈值。返回 {'consider_at','reply_at','family'}。

    未知族 → 回落 GATE_THRESHOLD_FALLBACK 并告警一次（不静默抹平差异）。
    每族槽位为 None 时沿用全局 GATE_CONSIDER_AT / GATE_REPLY_AT，
    所以 runtime_config 的调参覆盖照旧生效。
    """
    if model_id is None and kind is None:
        try:
            import rp_core as core
            kind, model_id, _ = core.resolve_model(model_key)
        except Exception:
            kind, model_id = 'local', ''
    base = {'consider_at': cfg.GATE_CONSIDER_AT, 'reply_at': cfg.GATE_REPLY_AT}
    table = getattr(cfg, 'GATE_THRESHOLDS', None) or {}
    fam = model_family(model_id, kind)
    row = table.get(fam) if fam else None
    if row is None:
        fam = getattr(cfg, 'GATE_THRESHOLD_FALLBACK', 'local')
        row = table.get(fam) or {}
        if fam not in _WARNED_FAMILY:
            _WARNED_FAMILY.add(fam)
            LOG.warning('阈值族未知（model=%s kind=%s）→ 回落 %s；'
                        '建议跑 scripts/gate_threshold_calibrate.py 校准',
                        model_id, kind, fam)
    for k, v in (row or {}).items():
        if v is not None:
            base[k] = v
    base['family'] = fam
    return base


def _tune():
    """角色级发言门覆盖（persona.json 的 gate.<key>）；出错就当没有。"""
    try:
        return persona.gate_tuning()
    except Exception:
        return {}


_MISSING = object()


def _group_val(key, gid):
    """本群覆盖值；没设过返回 _MISSING（继续回退角色卡/全局）。"""
    gid = str(gid or '').strip()
    if not gid:
        return _MISSING
    try:
        import group_settings
        val = group_settings.get(gid, key)
    except Exception:
        return _MISSING
    return _MISSING if val is None else val


def _rc(key, cfg_val, gid=None):
    """运行时覆盖：本群设置 > 角色级调参 > 热生效值 > rp_config 常量。"""
    val = _group_val(key, gid)
    if val is not _MISSING:
        return val
    tune = _tune()
    if key in tune:
        return tune[key]
    try:
        return rc.get(key, cfg_val)
    except Exception:
        return cfg_val


def _tune_val(key, cfg_val):
    """发言决策权重/门槛：角色卡优先，缺省用 rp_config。"""
    return _tune().get(key, cfg_val)


def _expert_factor():
    return _tune().get('expert_cooldown_factor', cfg.GATE_EXPERT_COOLDOWN_FACTOR)


# ---------- 基础相似度 ----------

def _bigrams(s):
    s = re.sub(r'\s+', '', s or '')
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _lcs_len(a, b):
    """最长公共连续子串长度（中文专名命中：地名 / 专有名词 / 楼栋号）。"""
    a = re.sub(r'\s+', '', a or '')
    b = re.sub(r'\s+', '', b or '')
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def entity_sim(a, b, min_len=3):
    """专名命中度：公共子串 ≥min_len 才算，越长越接近 1。

    为什么需要：无可用的 embedding 时（Ollama 未起/熔断），
    短句的 Jaccard 只有 0.1 上下，会把"上次说的那家店"这种明确的记忆召回判丢。
    """
    n = _lcs_len(a, b)
    if n < min_len:
        return 0.0
    return min(1.0, n / 4.0)


def lexical_sim(a, b):
    """字符二元组 Jaccard（embedding 不可用时的退路）。"""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / float(len(A | B))


def _sim(a, b):
    """语义相似度优先，embedding 失败（Ollama 未起/熔断）自动退化为字面相似。"""
    try:
        from embedding import embed, cosine
        va, vb = embed(a), embed(b)
        if va is not None and vb is not None:
            return cosine(va, vb)
    except Exception:
        pass
    return lexical_sim(a, b)


def topic_similarity(text, ctx):
    """与「最近几轮群聊 + 她上一句」的最大相似度。"""
    refs = [t.get('text', '') for t in (ctx.get('turns') or [])[-6:] if not t.get('self')]
    if ctx.get('her_last_text'):
        refs.append(ctx['her_last_text'])
    refs = [r for r in refs[-4:] if r]
    if not refs:
        return 0.0
    return max(_sim(text, r) for r in refs)


def memory_similarity(text, ctx):
    """与「她的记忆」的最大相似度。

    两路来源：
    - ctx['memory_snippets']：**她对当前发言人的记忆**（rp_core.memory_snippets(uid)）
    - ctx['scene_snippets']：**群级场景**（她听见但没接话的内容，§11.4 的 group scene）
      只用于匹配、不注入 prompt——避免把 A 说的话安到 B 头上（隐私与归属）。

    这是"之前聊到过的内容 → 更愿意接话"的落地：命中的是她**记得的事**，
    不是泛泛的话题相关；所以门槛更高、权重也更高。
    语义与专名两路取最大——专名（店名/楼栋号）在无 embedding 时也能命中。
    """
    refs = [s for s in (ctx.get('memory_snippets') or []) if s]
    refs += [s for s in (ctx.get('scene_snippets') or []) if s]
    if not refs:
        return 0.0
    best = 0.0
    for r in refs:
        best = max(best, _sim(text, r), entity_sim(text, r, min_len=3))
    return best


def is_expert(text):
    """是否属于她本行的话题（原作设定线）。这类内容最容易变成说教。"""
    return any(w in (text or '') for w in persona.gate().get('expert_words', ()))


# ---------- 信号 ----------

def is_direct(text, ctx=None):
    """是否在**直接对她说话**：@ 她 / 回复她的消息 / 开头点她的名 / 带名字的问句。

    注意"第三方提及"（"角色刚才那句好狠"）不算直达——群里别人议论她很常见，
    每条都必回会把她变成抢话的机器人。这类只加分（sig['name']），由 L2 决定接不接。
    """
    ctx = ctx or {}
    if ctx.get('at_bot') or ctx.get('reply_to_her'):
        return True
    t = (text or '').strip()
    if not t:
        return False
    head = t.lstrip('@＠ 　:：')
    name = persona.direct_name()
    if head.startswith('/' + name):
        return True
    if head.startswith(name):
        rest = head[len(name):]
        # 只有"呼语"才算直达：名字后面直接停顿/感叹，或马上出现"你"。
        # "角色刚才那句好狠" 名字后面接的是陈述，属于别人在议论她 → 不算直达。
        if not rest or rest[0] in '，,：:！!？?、。…~～ 　' or rest[0] in '啊呀诶':
            return True
        if '你' in rest[:10]:
            return True
    if name in t and has_question(t):
        return True
    return False


def is_repeat(ctx, uid, text):
    """同一人在 GATE_REPEAT_WINDOW 内发了完全一样的话。

    两种来源都要挡：QQ 反向 WS 断线重连会**重投**已经处理过的事件；
    群友刷屏同一句会让她连回好几遍——两种都很难看。
    """
    prev = ((ctx or {}).get('last_text_by_uid') or {}).get(str(uid))
    if not prev:
        return False
    prev_text, prev_ts = prev
    if (text or '').strip() != (prev_text or '').strip():
        return False
    now = (ctx or {}).get('now') or time.time()
    return (now - (prev_ts or 0)) < cfg.GATE_REPEAT_WINDOW


def has_question(text):
    return any(h in (text or '') for h in cfg.GATE_QUESTION_HINTS)


def has_interest(text):
    return any(w in (text or '') for w in persona.gate().get('interest_words', ()))


def emotion_level(text):
    t = text or ''
    hits = sum(1 for h in cfg.GATE_EMOTION_HINTS if h in t)
    if hits == 0 and len(t) >= 40:
        hits = 1
    return min(1.0, hits / 2.0)


def _random_bonus(text):
    """按文本定值的随机项：同一句话结果稳定（便于复现），但整体上像随手一插。"""
    rnd = random.Random(hash(text or '') & 0xFFFF)
    return rnd.random()


def cooldown_hit(ctx, uid, now, expert=False):
    """冷却是否命中：同群间隔 / 同一人间隔 / 每分钟上限。返回原因或 None。

    间隔设为 0 表示该项不限制（同群/同人各自独立；每分钟上限另算）。

    expert=True（她本行的话题）时同群冷却按 GATE_EXPERT_COOLDOWN_FACTOR **缩短**——
    这是她乐意聊的领域，允许顺着话题往下接，而不是刚开口就被按住。
    （乘子仍作用于"冷却时长"：>1 就变回"更克制"，方便随时调口味。）
    """
    group_cd = _rc('group_cooldown', cfg.GATE_GROUP_COOLDOWN) \
        * (_expert_factor() if expert else 1.0)
    last = ctx.get('last_reply_ts') or 0
    if group_cd > 0 and last and now - last < group_cd:
        return 'group_cooldown'
    user_cd = _rc('user_cooldown', cfg.GATE_USER_COOLDOWN)
    lu = (ctx.get('last_reply_by_uid') or {}).get(str(uid))
    if user_cd > 0 and lu and now - lu < user_cd:
        return 'user_cooldown'
    recent = [ts for ts in (ctx.get('reply_ts_list') or []) if now - ts <= 60]
    if len(recent) >= _rc('max_per_min', cfg.GATE_MAX_PER_MIN):
        return 'rate_limit'
    return None


def cooldown_remaining(ctx, uid, now=None, expert=False):
    """还要等多少秒才允许发言（bridge 用它做"延后"而不是"丢弃"）。0 表示可以。"""
    ctx = ctx or {}
    now = now or ctx.get('now') or time.time()
    group_cd = _rc('group_cooldown', cfg.GATE_GROUP_COOLDOWN) \
        * (_expert_factor() if expert else 1.0)
    waits = []
    last = ctx.get('last_reply_ts') or 0
    if group_cd > 0 and last:
        waits.append(group_cd - (now - last))
    user_cd = _rc('user_cooldown', cfg.GATE_USER_COOLDOWN)
    lu = (ctx.get('last_reply_by_uid') or {}).get(str(uid))
    if user_cd > 0 and lu:
        waits.append(user_cd - (now - lu))
    recent = sorted(ts for ts in (ctx.get('reply_ts_list') or []) if now - ts <= 60)
    if len(recent) >= _rc('max_per_min', cfg.GATE_MAX_PER_MIN):
        # 等最早那条满 60 秒
        waits.append(60 - (now - recent[0]))
    waits = [w for w in waits if w > 0]
    return max(waits) if waits else 0.0


def score(text, ctx, uid=''):
    """L1 打分：返回 (score, signals)。signals 供落盘回溯（"为什么不回这句"）。

    注意：冷却**不参与打分**——它只拦"要不要发"，不压低"想不想说"。
    早先版本把冷却做成乘子，结果非 @ 的消息永远到不了终审线，实际退化成"只会回 @"。
    """
    ctx = ctx or {}
    sig = {}

    if is_direct(text, ctx):
        sig['direct'] = cfg.GATE_W_DIRECT
        return cfg.GATE_W_DIRECT, sig

    # 别人在议论她（不是问她）：给分但不置顶，接不接交给 L2
    if persona.direct_name() in (text or ''):
        sig['name'] = cfg.GATE_W_NAME

    if has_question(text):
        sig['question'] = _tune_val('w_question', cfg.GATE_W_QUESTION)
    sim = topic_similarity(text, ctx)
    if sim > 0.05:
        sig['topic'] = round(_tune_val('w_topic', cfg.GATE_W_TOPIC) * sim, 4)
        sig['topic_sim'] = round(sim, 4)
    if has_interest(text):
        sig['interest'] = _tune_val('w_interest', cfg.GATE_W_INTEREST)
    # 记忆命中：这句在说她记得的事 → 显著提高自主接话意愿
    msim = memory_similarity(text, ctx)
    if msim >= _tune_val('memory_sim_at', cfg.GATE_MEMORY_SIM_AT):
        sig['memory'] = round(_tune_val('w_memory', cfg.GATE_W_MEMORY) * msim, 4)
        sig['memory_sim'] = round(msim, 4)
    if is_expert(text):
        sig['expert'] = cfg.GATE_W_EXPERT  # 她真正懂的话题：更愿意接（正分，不是只做标记）
    emo = emotion_level(text)
    if emo > 0:
        sig['emotion'] = round(_tune_val('w_emotion', cfg.GATE_W_EMOTION) * emo, 4)
    # 延续：她刚说过话且话题还在同一个方向上——人类最自然的插话时机
    her_ts = ctx.get('her_last_ts') or 0
    if her_ts and (ctx.get('now') or time.time()) - her_ts < 180 and sim > 0.15:
        sig['continuation'] = _tune_val('w_continuation', cfg.GATE_W_CONTINUATION)
    if ctx.get('neglected'):
        sig['neglect'] = _tune_val('w_neglect', cfg.GATE_W_NEGLECT)
    sig['random'] = round(_tune_val('w_random', cfg.GATE_W_RANDOM)
                          * _random_bonus(text), 4)

    # 只累加权重项（*_sim 是调试信息，不参与求和）
    total = sum(sig.get(k, 0) for k in
                ('name', 'question', 'topic', 'interest', 'memory', 'expert',
                 'emotion', 'continuation', 'neglect', 'random'))
    return round(total, 4), sig


def note_seen(ctx, uid, text, ts=None):
    """登记一条**已处理**的消息（bridge / sim 每条都要调，供重复检测用）。

    必须在 decide 之后调用——先登记再判断会把每条消息都判成重复。
    """
    ctx = ctx or {}
    ts = ts or ctx.get('now') or time.time()
    ctx.setdefault('last_text_by_uid', {})[str(uid)] = ((text or '').strip(), ts)
    return ctx


# ---------- 决策 ----------

def decide(text, *, uid='', nickname='', ctx=None, judge=None, direct=False,
           stats=None, model_key=None):
    """返回 Decision(action, score, signals, reason)。

    流程（与 rp_config 里的语义一致）：
      1) 被 @ / 点名 / 回复她 → 直接回（仅受每分钟上限保护）
      2) L1 分数 < CONSIDER_AT → 沉默，**不花模型调用**
      3) 冷却命中 → 沉默（不发，但记录原因；bridge 可在冷却后再触发）
      4) 有 judge → 交由模型终审（这就是"模型自己决定时机"）
         无 judge（离线/模型不可用）→ 退化为 REPLY_AT 阈值判定

    judge: callable(text, nickname, ctx) -> (bool, why)
    model_key: 判官/生成模型 key——阈值按模型族取（见 gate_thresholds）
    stats: 传 dict 时累加 {'judged': n, 'replied': n}（成本观测）
    """
    ctx = ctx or {}
    if direct:
        ctx = dict(ctx, at_bot=True)
    s, sig = score(text, ctx, uid=str(uid))
    expert = is_expert(text)
    stats = stats if isinstance(stats, dict) else {}
    th = gate_thresholds(model_key)
    tune = _tune()
    for key in ('consider_at', 'reply_at'):
        if key in tune:
            th[key] = tune[key]

    # 0) 重复消息（重投/刷屏）：连 @ 也不重复回，否则她会被逼成复读机
    if is_repeat(ctx, uid, text):
        stats['repeated'] = stats.get('repeated', 0) + 1
        return Decision(IGNORE, s, sig, 'repeat(<%ds)' % cfg.GATE_REPEAT_WINDOW)

    if is_direct(text, ctx):
        # @必回：限频只约束"主动插话"。若此刻不宜立刻发（同群间隔/每分钟上限），
        # 仍返回 REPLY，但标记 over_rate 并给出等待秒数，由 bridge **延后发送**而不是丢弃。
        now = ctx.get('now') or time.time()
        wait = cooldown_remaining(ctx, uid, now)
        if wait > 0:
            stats['over_rate'] = stats.get('over_rate', 0) + 1
            return Decision(REPLY, s, sig, 'direct_over_rate(排队 %.0fs)' % wait)
        stats['replied'] = stats.get('replied', 0) + 1
        return Decision(REPLY, s, sig, 'direct')

    if s < th['consider_at']:
        return Decision(IGNORE, s, sig, 'score<%.2f' % th['consider_at'])

    cd = cooldown_hit(ctx, uid, ctx.get('now') or time.time(), expert=expert)
    if cd:
        return Decision(IGNORE, s, sig, 'cooldown:%s%s' % (cd, '(expert)' if expert else ''))

    if judge is None:
        ok = s >= th['reply_at']
        stats['replied'] = stats.get('replied', 0) + (1 if ok else 0)
        return Decision(REPLY if ok else IGNORE, s, sig,
                        'offline_threshold(%.2f)' % th['reply_at'])

    stats['judged'] = stats.get('judged', 0) + 1
    try:
        ok, why = judge(text, nickname, dict(ctx, expert=expert))
    except Exception as e:
        logutil.throttled(LOG, 'judge_error', logging.WARNING,
                          'L2 判定异常，本轮按不回处理：%s', e, exc_info=True)
        return Decision(IGNORE, s, sig, 'judge_error:%s' % e)
    if ok:
        stats['replied'] = stats.get('replied', 0) + 1
    return Decision(REPLY if ok else IGNORE, s, sig, 'judge:%s' % (why or ''))


# ---------- 主动开话题 ----------

def proactive_due(ctx):
    """是否可以考虑主动开口。返回 (bool, reason)。

    条件：群内静默 ≥ GATE_IDLE_SEC、距上次主动 ≥ 30min、当天次数未超、**在群活跃时段**。
    活跃时段来源（优先级）：ctx['active_hours']（由 group_activity 学习得到）
    → 回落 GATE_ACTIVE_HOURS 默认窗口。学习不足时 group_activity 自带回落并注明原因。
    """
    ctx = ctx or {}
    gid = ctx.get('gid')
    if not _rc('proactive_enabled', True, gid):
        return False, 'proactive_off'
    now = ctx.get('now') or time.time()
    turns = ctx.get('turns') or []
    last_msg = max([t.get('ts') or 0 for t in turns] or [0])
    if not last_msg or now - last_msg < _rc('gate_idle_sec', cfg.GATE_IDLE_SEC, gid):
        return False, 'not_idle'
    if now - (ctx.get('last_proactive_ts') or 0) < _rc(
            'gate_proactive_min_gap', cfg.GATE_PROACTIVE_MIN_GAP, gid):
        return False, 'min_gap'
    if (ctx.get('proactive_today') or 0) >= _rc(
            'gate_proactive_daily_max', cfg.GATE_PROACTIVE_DAILY_MAX, gid):
        return False, 'daily_quota'
    hour = datetime.datetime.fromtimestamp(now).hour
    hours = ctx.get('active_hours')
    if hours is None:
        lo, hi = cfg.GATE_ACTIVE_HOURS
        ok_hour = lo <= hour < hi
        src = 'default'
    else:
        ok_hour = hour in set(hours)
        src = ctx.get('active_source') or 'learned'
    if not ok_hour:
        return False, 'quiet_hours(%s)' % src
    return True, 'ok(%s)' % src


def build_judge_prompt(text, nickname, ctx):
    """L2 终审 prompt（插入一条消息的场景）。"""
    ctx = ctx or {}
    turns = ctx.get('turns') or []
    name = persona.direct_name()
    lines = []
    for t in turns[-8:]:
        who = name if t.get('self') else (t.get('nickname') or '群友')
        lines.append('%s：%s' % (who, (t.get('text') or '')[:60]))
    prompt = persona.prompt(
        'gate_judge', turns='\n'.join(lines) or '（暂无）',
        nickname=nickname or '群友',
        text=(text or '')[:120],
        relation=ctx.get('relation') or '相识',
        name=name)
    if ctx.get('expert'):
        prompt += ('\n注意：这是她真正熟悉、也乐意聊的话题。只要这句她确实接得上，'
                   '就接下——她乐意顺着往下展开。她只是不会抢着解说设定、也不会把话说成上课。')
    return prompt


def build_proactive_prompt(candidate, ctx):
    """L2 终审 prompt（她想主动开口的场景）。"""
    turns = (ctx or {}).get('turns') or []
    name = persona.direct_name()
    lines = []
    for t in turns[-6:]:
        who = name if t.get('self') else (t.get('nickname') or '群友')
        lines.append('%s：%s' % (who, (t.get('text') or '')[:60]))
    return persona.prompt(
        'gate_proactive', turns='\n'.join(lines) or '（群里安静了一会儿）',
        candidate=(candidate or '')[:120], name=name)


def make_llm_judge(model_key=None, channel=None):
    """把 L2 终审接到真实模型（bridge 用）。返回 judge 可调用对象。

    失败/解析不出来 → (False, 'parse_fail')，即"不确定就不说话"。
    """
    import memory_card as mem
    import rp_core as core

    def judge(text, nickname, ctx):
        kind, model_id, think = core.resolve_model(model_key)
        prompt = build_judge_prompt(text, nickname, ctx)
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


def make_proactive_judge(model_key=None, channel=None):
    """主动开口的终审：这句话放现在自然吗（bridge 用）。"""
    import memory_card as mem
    import rp_core as core

    def judge(candidate, ctx):
        kind, model_id, think = core.resolve_model(model_key)
        prompt = build_proactive_prompt(candidate, ctx or {})
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


# ---------- 落盘（可回溯） ----------

def log_decision(path, entry):
    """把一次决策追加到 decisions.jsonl（"为什么不回我"必须查得清）。"""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        entry = dict(entry)
        entry.setdefault('ts', datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except (OSError, ValueError):
        pass


# ---------- 自测（离线，不需要 QQ 与模型） ----------

SCENARIO = [
    ('群友A', '今天空调又坏了，热死', 0),
    ('群友C', '我这边也在画图，脑袋要炸了', 3),
    ('群友B', '@角色 你在不在', 6),
    ('小满', '午饭吃什么啊', 9),
    ('桃子', '我想喝咖啡，有人一起吗', 12),
    ('阿泽', '咖啡因对我没用', 15),
    ('老白', '你们年轻人啊，晚上少喝点咖啡', 18),
    ('阿岚', '我烤了司康，要不要来尝一块', 21),
    ('群友A', '角色你觉得司康配果酱好么', 24),
    ('群友B', '她又没说话，估计在忙', 27),
    ('小满', '对了，谁看过那部新上映的电影', 240),
    ('群友C', '我看过，结局有点突然', 243),
    # 记忆命中：这句在说她记得的事（司康那回）
    ('群友A', '上次说的那个司康，我今天真去买了', 246),
    # 她本行的话题：她真正懂的东西 → 更愿意接、可以展开
    ('群友B', '那个世界观设定到底怎么算的', 252),
]


def _run_pass(title, judge, stats):
    """按时间线跑一遍场景。judge=None 表示离线退化（不调模型）。"""
    print('\n--- %s ---' % title)
    print('%-4s %-5s %-26s %-6s %-9s %s' % ('#', '说话人', '消息', 'L1分', '动作', '原因'))
    print('-' * 92)
    ctx = {'turns': [], 'relation': '相识',
           'memory_snippets': ['司康配果酱更合适', '群友A在走廊那头上班',
                               '阿岚烤了司康']}
    now0 = time.time()
    rows = []
    # 替身回复文本：让"话题延续"信号有真实的可比对象（不是拿原句凑相似度）
    replies = {3: '在呢。找我有什么事？', 9: '呵呵，甜点倒是可以，配果酱更合适呢。'}
    for i, (nick, text, off) in enumerate(SCENARIO, 1):
        now = now0 + off
        ctx['now'] = now
        ctx['last_reply_ts'] = ctx.get('_last_reply', 0)
        ctx['neglected'] = bool(ctx.get('_last_reply')) and now - ctx['_last_reply'] > 300
        d = decide(text, uid=nick, nickname=nick, ctx=ctx, judge=judge, stats=stats)
        rows.append((i, nick, text, d))
        if d.action == REPLY:
            ctx['_last_reply'] = now
            ctx['her_last_ts'] = now
            ctx['her_last_text'] = replies.get(i, '（她应了一句）')
            ctx.setdefault('last_reply_by_uid', {})[nick] = now
            ctx.setdefault('reply_ts_list', []).append(now)
        ctx['turns'].append({'nickname': nick, 'text': text, 'ts': now})
        print('%-4d %-5s %-26s %-6.2f %-9s %s' % (
            i, nick, text[:24], d.score, d.action, d.reason))
    return rows


def _selftest():
    print('=== 群发言决策自测（离线；L2 用替身，不必启动模型）===')
    print('阈值：L1≥%.2f 才进终审；被 @ 直接回\n' % cfg.GATE_CONSIDER_AT)
    print('当前模型族阈值：%s\n' % gate_thresholds())

    stats_a = {}
    # 替身 A：来者不拒（展示 L1 放进来多少、要花多少次模型调用）
    _run_pass('替身 A：终审一律通过（上界）', lambda t, n, c: (True, '想接'), stats_a)

    # 替身 B：近似真实模型的挑剔度（只接问句/她感兴趣的话题）
    def picky(t, n, c):
        like = any(h in t for h in ('吗', '呢', '么', '?', '？')) or \
            any(w in t for w in persona.gate().get('interest_words', ()))
        return like, ('这句想接' if like else '与我无关')
    stats_b = {}
    _run_pass('替身 B：只在问句/感兴趣话题时接（近似真实）', picky, stats_b)

    n_msg = len(SCENARIO)
    print('\n=== 成本与效果 ===')
    for name, st in (('替身A', stats_a), ('替身B', stats_b)):
        print('%-6s 消息 %d 条 → 模型终审调用 %d 次（%.1f%%）· 实际开口 %d 次'
              % (name, n_msg, st.get('judged', 0),
                 100.0 * st.get('judged', 0) / n_msg, st.get('replied', 0)))

    print('\n=== 主动开话题时机 ===')
    now0 = time.time()
    learned = set(range(19, 24))  # 假设学习到：群里 19-23 点才热闹
    cases = (
        ('刚聊完', 30, None, None, None),
        ('静默 5 分钟', 300, None, None, None),
        ('静默 12 分钟（默认窗口）', 720, None, None, None),
        ('21:00 静默 12 分钟（学习窗口）', 720, None, learned, 'learned:weekday(5天)'),
        ('静默 12 分钟但今天已开 6 次', 720, 6, None, None),
        ('静默 12 分钟但凌晨', 720, None, None, None),
        ('凌晨 + 学习窗口 19-23', 720, None, learned, 'learned:weekday(5天)'),
    )
    for label, off, today, hours, src in cases:
        now_ts = now0 + off
        if '凌晨' in label:
            now_ts = datetime.datetime.now().replace(hour=3, minute=0).timestamp()
        if label.startswith('21:00'):
            now_ts = datetime.datetime.now().replace(hour=21, minute=0).timestamp()
        ctx = {'turns': [{'nickname': '群友A', 'text': '嗯', 'ts': now_ts - off}],
               'now': now_ts, 'last_proactive_ts': now_ts - 4000,
               'proactive_today': today or 0,
               'active_hours': hours, 'active_source': src}
        ok, why = proactive_due(ctx)
        print('%-28s → %s（%s）' % (label, '可以开口' if ok else '不开口', why))

    print('\n=== 熟悉话题：更容易接 + 冷却更短（她刚说过话 3 秒）===')
    now = time.time()
    for label, sample in (('普通话题', '今天天气不错'), ('她本行的话题', '那个世界观的设定到底怎么算')):
        ctx = {'now': now, 'last_reply_ts': now - 3, 'reply_ts_list': [now - 3]}
        e = is_expert(sample)
        s, sig = score(sample, ctx, uid='x')
        w = cooldown_remaining(ctx, 'x', now, expert=e)
        print('%-10s → L1 %.2f（expert 项 %s）| 还需等 %2.0f 秒（乘子 ×%.1f）'
              % (label, s, sig.get('expert', 0), w,
                 cfg.GATE_EXPERT_COOLDOWN_FACTOR if e else 1.0))

    print('\n结论：L1 只负责挡掉明显无关的消息，真正的"要不要插话"由 L2 模型判定；'
          '上线后 decisions.jsonl 可逐条复核。')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
