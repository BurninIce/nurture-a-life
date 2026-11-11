# -*- coding: utf-8 -*-
"""角色设定条目检索（角色卡 lore，本地，只读）。

与 novel_rag 的分工：
    novel_rag  原作正文片段 → 被问到「原作第几卷」时查原文
    lore_rag   角色卡里整理好的设定条目（人物、世界观、能力…）
               → 每轮都查，命中才注入【Canon notes】

检索顺序（宁漏勿错）：
    1. 本句关键词：按最长匹配占位（长词命中后不会再算其中的短词），
       按命中关键词总长排序，低于最高分一半的丢弃，最多 LORE_KEYWORD_TOP_K 条
    2. 本句没命中：用检索 query（她上一句 + 本句）对条目 probe 做向量相似，
       ≥ LORE_MIN_SCORE 的最多 LORE_EMBED_TOP_K 条；本句像追问时再补她上一句关键词命中的 1 条
向量缓存 state/personas/<id>/lore_index/index.json；源条目或 embedding 模型变了自动重建，
建不出来就只走关键词。
"""
import hashlib
import json
import logging
import os
import re
import threading

import card_source
import persona
import rp_config as cfg
import runtime_config as rc

LOG = logging.getLogger('lore')

_lock = threading.Lock()
_index = {}   # (pid, key) -> vectors
_OVERRIDES = {}   # pid -> 测试指定目录
# 本句没关键词时，只有像追问（「后来呢」「为什么」）才沿用她上一句的话题
_FOLLOW_CUES = ('?', '？', '吗', '呢', '为什么', '怎么', '后来', '然后', '当时', '那时', '真的')


def set_index_dir(path, pid=None):
    pid = pid or persona.current()
    with _lock:
        if path:
            _OVERRIDES[pid] = path
        else:
            _OVERRIDES.pop(pid, None)
        for k in [k for k in _index if k[0] == pid]:
            _index.pop(k, None)


def _index_dir(pid=None):
    pid = pid or persona.current()
    return _OVERRIDES.get(pid) or persona.lore_index_dir(pid)


def _entries(pid=None):
    return card_source.lore_entries(data=card_source.load(pid=pid))


def keyword_scores(text, entries=None):
    """{entry_id: score}。score = 该条目被认领的关键词长度之和。"""
    entries = entries if entries is not None else _entries()
    text = (text or '').lower()
    if not text:
        return {}
    words = sorted({k.lower() for e in entries for k in e.get('keywords', [])},
                   key=len, reverse=True)
    taken = [False] * len(text)
    claimed = set()
    for w in words:
        start = text.find(w)
        while start >= 0:
            end = start + len(w)
            if not any(taken[start:end]):
                for i in range(start, end):
                    taken[i] = True
                claimed.add(w)
            start = text.find(w, start + 1)
    scores = {}
    for e in entries:
        s = sum(len(k) for k in {k.lower() for k in e.get('keywords', [])} if k in claimed)
        if s:
            scores[e['id']] = s
    return scores


def _top_keyword(text, entries, k):
    scores = keyword_scores(text, entries)
    if not scores:
        return []
    best = max(scores.values())
    order = [e['id'] for e in entries if scores.get(e['id'], 0) * 2 >= best]
    order.sort(key=lambda i: -scores[i])     # sort 稳定：同分保持 JSON 顺序
    return order[:k]


# ---------- 向量 ----------

def _source_key(entries, model):
    blob = json.dumps([[e['id'], e.get('probe', '')] for e in entries], ensure_ascii=False)
    return model + ':' + hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]


def _index_path(pid=None):
    return os.path.join(_index_dir(pid), 'index.json')


def _vectors(entries, pid=None):
    """{entry_id: vector}；不可用返回 None。"""
    import embedding
    pid = pid or persona.current()
    key = _source_key(entries, embedding.active_model())
    with _lock:
        hit = _index.get((pid, key))
        if hit is not None:
            return hit
    try:
        with open(_index_path(pid), encoding='utf-8') as f:
            disk = json.load(f)
        if disk.get('key') == key:
            with _lock:
                _index[(pid, key)] = disk['vectors']
            return disk['vectors']
    except (OSError, ValueError, KeyError):
        pass
    vectors = {}
    for e in entries:
        vec = embedding.embed(e.get('probe') or e['text'])
        if vec is None:
            LOG.warning('lore index: embedding 不可用，本轮只走关键词')
            return None
        vectors[e['id']] = vec
    try:
        os.makedirs(_index_dir(pid), exist_ok=True)
        tmp = _index_path(pid) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'key': key, 'vectors': vectors}, f)
        os.replace(tmp, _index_path(pid))
    except OSError:
        LOG.warning('lore index 写盘失败（仅内存缓存）')
    with _lock:
        _index[(pid, key)] = vectors
    return vectors


def embed_scores(query, entries=None, pid=None):
    """[(entry_id, score)] 降序；embedding 不可用或维度不一致返回 []。"""
    import embedding
    pid = pid or persona.current()
    entries = entries if entries is not None else _entries(pid)
    if not (query or '').strip():
        return []
    vectors = _vectors(entries, pid)
    if not vectors:
        return []
    q = embedding.embed(query)
    if q is None:
        return []
    out = []
    for e in entries:
        v = vectors.get(e['id'])
        if v is None or len(v) != len(q):
            LOG.warning('lore index 维度与查询不一致，跳过语义兜底')
            return []
        out.append((e['id'], embedding.cosine(q, v)))
    out.sort(key=lambda x: -x[1])
    return out


# ---------- 对外 ----------

def search(message, prev_reply='', query=None, pid=None):
    """返回命中的条目 dict 列表（已排序、已去重）。"""
    pid = pid or persona.current()
    if not rc.get('lore_enabled', cfg.LORE_ENABLED):
        return []
    entries = _entries(pid)
    by_id = {e['id']: e for e in entries}
    ids = _top_keyword(message, entries, cfg.LORE_KEYWORD_TOP_K)
    if not ids:
        import embedding
        min_score = cfg.LORE_MIN_SCORE_BY_MODEL.get(
            embedding.active_model().split(':')[0], cfg.LORE_MIN_SCORE)
        hits = embed_scores(query if query is not None else message, entries, pid)
        ids = [i for i, s in hits[:cfg.LORE_EMBED_TOP_K] if s >= min_score]
        if any(c in (message or '') for c in _FOLLOW_CUES):
            for i in _top_keyword(prev_reply, entries, 1):
                if i not in ids:
                    ids.append(i)
    return [by_id[i] for i in ids]


def term_pattern(pid=None):
    """术语的转义正则（长词在前），供 clean_output 还原模型写的【术语】括号。"""
    words = sorted({k for e in _entries(pid) for k in e.get('keywords', [])},
                   key=len, reverse=True)
    return '|'.join(re.escape(w) for w in words) if words else None


def format_notes(entries, pid=None):
    if not entries:
        return ''
    data = card_source.load(pid=pid)['lore']
    lines = [data['header'], data['note']]
    for e in entries:
        line = '- ' + e['text']
        if e.get('anchor'):
            line += ' Key line:「%s」' % e['anchor']
        lines.append(line)
    return '\n'.join(lines)


def block(message, prev_reply='', query=None, pid=None):
    try:
        return format_notes(search(message, prev_reply=prev_reply,
                                   query=query, pid=pid), pid=pid)
    except Exception:
        LOG.exception('lore 检索失败，本轮不注入')
        return ''
