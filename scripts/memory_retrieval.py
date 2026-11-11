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
    intent = (mu.cosine(q_vec, mu.embed(mu.MEMORY_INTENT_PROTO))
              >= mu.MEMORY_INTENT_THRESHOLD)
    hits = _semantic_hits(
        episodes, q_vec, now,
        mu.RECALL_INTENT_THRESHOLD if intent else mu.RECALL_THRESHOLD)
    if not intent:
        return hits[:k]

    # 意图召回：相关事件排前面，再用收藏/近因补满；去重避免同一条占两格
    out, seen = [], set()
    for ep in hits + _favorite_first(episodes, k):
        eid = ep.get('id')
        if eid in seen:
            continue
        seen.add(eid)
        out.append(ep)
        if len(out) >= k:
            break
    return out