# -*- coding: utf-8 -*-
"""记忆检索：语义命中 + 近因/收藏排序。

从 memory_card.py 拆出，只负责「从事件库里挑出这一轮该回忆哪几条」。
存储、写入、归纳仍在 memory_card；embed / cosine / 阈值 / 时间工具统一来自
memory_util（叶子模块），依赖方向单向，不再反向 import memory_card。
测试里替换 `memory_util.embed = fake` 之类的 patch 仍然生效。
"""
import datetime
import math

import memory_util as mu


def _recency_bonus(ep, now=None):
    dt = mu._parse_time(mu.episode_time(ep))
    if dt is None:
        return 0.0
    now = now or datetime.datetime.now()
    days = max(0, (now - dt).days)
    return math.exp(-days / 7.0) * mu.RECALL_RECENCY_WEIGHT


def _favorite_first(episodes, k):
    """收藏事件排前面，其余按近因倒序（强制召回与退化路径共用）。"""
    out, seen = [], set()
    for ep in list(reversed(episodes)):
        if ep.get('favorite') and ep.get('id') not in seen:
            out.append(ep)
            seen.add(ep.get('id'))
    for ep in reversed(episodes):
        if len(out) >= k:
            break
        if ep.get('id') not in seen:
            out.append(ep)
            seen.add(ep.get('id'))
    return out[:k]


def _semantic_hits(episodes, q_vec, now, threshold):
    """按语义相似度过滤并排序（相似度决定是否命中，加权只影响顺序）。"""
    scored = []
    for ep in episodes:
        text = ' '.join([ep.get('summary', ''), ep.get('user_said', ''),
                         ep.get('character_said', '')])
        sim = mu.cosine(q_vec, mu.embed(text))
        if sim >= threshold:
            score = (sim + ep.get('importance', 1) * 0.02
                     + _recency_bonus(ep, now))
            if ep.get('favorite'):
                score += mu.RECALL_FAVORITE_WEIGHT
            scored.append((score, sim, ep))
    scored.sort(key=lambda x: -x[0])
    return [ep for _, _, ep in scored]


def strong_relation_ids(episodes):
    """带强关系标签的事件 id（告白/思念/关系转折等）。"""
    return {e.get('id') for e in episodes
            if set(e.get('tags') or ()) & mu.RELATION_EPISODE_TAGS}


def query_intent(q_vec):
    """(是否问记忆, 是否问关系/思念)。

    两个维度都靠语义原型判断，没有关键词表；embedding 失败时都不触发，
    退回普通检索——宁可漏召回，也不要凭猜测把话题引到感情上。
    """
    if q_vec is None:
        return False, False
    memory = max(mu.cosine(q_vec, mu.embed(mu.MEMORY_INTENT_PROTO)),
                 mu.cosine(q_vec, mu.embed(mu.MEMORY_INTENT_PROTO_2))) \
        >= mu.MEMORY_INTENT_THRESHOLD
    relation = max(mu.cosine(q_vec, mu.embed(mu.RELATION_INTENT_PROTO)),
                   mu.cosine(q_vec, mu.embed(mu.INTIMATE_INTENT_PROTO))) \
        >= mu.RELATION_INTENT_THRESHOLD
    return memory, relation


def blocked_card_sources(data, query):
    """普通话题下，来源全在强关系事件里的卡片条目不该每轮注入。

    返回需要屏蔽的事件 id 集合；问的是我们之间的感情时返回空集（卡片恢复完整）。
    卡片条目是常驻注入，比召回更持久：一条只从告白夜归纳出的画像会在此后每一轮
    把语气往亲密方向推，哪怕讨论的是完全无关的事。
    """
    episodes = [e for e in data['episodes'] if not e.get('inactive')]
    strong = strong_relation_ids(episodes)
    if not strong:
        return set()
    _memory, relation = query_intent(mu.embed(query or ''))
    if relation:
        return set()
    return strong


def retrieve(data, query, k=None):
    """语义检索触发回忆。

    两阶段（无硬编码关键词，全靠语义）：
    1) 记忆意图识别：与"问记忆"原型句相似度 ≥ 阈值 → 语义命中优先，近因补足。
       问「什么时候」时近期事件未必相关，只给最近几条会让模型编日期。
    2) 普通语义检索：与事件库余弦相似度 Top-K（超阈值才触发）
    embedding 失败时退化为近因召回。
    收藏事件不参与遗忘，排序时加分（固定保留 + 优先注入）。
    """
    k = mu.RECALL_K if k is None else k
    episodes = [e for e in data['episodes']
                if not e.get('inactive') and e.get('id') not in mu.superseded_ep_ids(data)]
    if not episodes:
        return []
    q_vec = mu.embed(query or '')
    if q_vec is None:
        return _favorite_first(episodes, k)  # 退化：近因（收藏优先）

    now = datetime.datetime.now()
    memory_intent, relation_intent = query_intent(q_vec)
    strong = strong_relation_ids(episodes)
    if not (memory_intent or relation_intent):
        # 普通话题：强关系事件不参与召回，否则一句话能把整轮拽回感情确认
        pool = [e for e in episodes if e.get('id') not in strong]
        return _semantic_hits(pool, q_vec, now, mu.RECALL_THRESHOLD)[:k]

    hits = _semantic_hits(episodes, q_vec, now, mu.RECALL_INTENT_THRESHOLD)
    # 问关系/思念时，补位只从强关系事件里挑：否则补进来的是最近几件日常，
    # 问「想不想我」却答出前天买了什么书。
    fill_pool = episodes if memory_intent else [e for e in episodes
                                                if e.get('id') in strong]
    out, seen = [], set()
    for ep in hits + _favorite_first(fill_pool, k):
        eid = ep.get('id')
        if eid in seen:
            continue
        seen.add(eid)
        out.append(ep)
        if len(out) >= k:
            break
    return out