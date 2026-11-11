# -*- coding: utf-8 -*-
"""原作小说检索（离线，只读）。

与 corpus_rag 的分工：
    corpus_rag  角色的说话样本 → 学「怎么说」
    novel_rag   原作正文片段   → 查「发生过什么」

索引由 novel_index.py 离线构建（默认 state/personas/<id>/novel_index/，不入库）：
    chunks.jsonl   每行 {i, line, volume, chapter, text}
    embeddings.npy float32，与行号对齐，已归一化
    meta.json      {built_at, model, dim, count, sources}

检索只读本地文件 + 调一次 Ollama embedding，不出网。
"""
import json
import logging
import os
import threading

import rp_config as cfg
import persona

LOG = logging.getLogger('tools')

_DIR_OVERRIDE = None
_lock = threading.Lock()

# 索引按目录缓存：线程内角色不同时不会读到别的角色已加载的索引。
_cache = {}   # dir -> (chunks, vectors, meta)


def set_dir(path):
    """切换索引目录（CLI / 测试用）；path 为空 → 恢复跟随当前角色。"""
    global _DIR_OVERRIDE
    with _lock:
        _DIR_OVERRIDE = path or None
        _cache.pop(path, None)


def _dir():
    return _DIR_OVERRIDE or persona.novel_index_dir()


def _paths():
    d = _dir()
    return (os.path.join(d, 'chunks.jsonl'),
            os.path.join(d, 'embeddings.npy'),
            os.path.join(d, 'meta.json'))


def available(pid=None):
    """索引是否齐全；pid 用于判断非当前角色（能力位检查不能借当前角色）。"""
    d = _DIR_OVERRIDE or persona.novel_index_dir(pid)
    return all(os.path.exists(os.path.join(d, n))
               for n in ('chunks.jsonl', 'embeddings.npy', 'meta.json'))


def stats():
    if not available():
        return {}
    try:
        with open(_paths()[2], encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _load():
    """惰性加载索引；文件缺失返回 False。"""
    d = _dir()
    with _lock:
        if d in _cache:
            return True
        chunks_path, vec_path, meta_path = _paths()
        if not all(os.path.exists(p) for p in (chunks_path, vec_path, meta_path)):
            return False
        try:
            import numpy as np
            rows = []
            with open(chunks_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            with open(meta_path, encoding='utf-8') as f:
                meta = json.load(f)
            vectors = np.load(vec_path)
        except (OSError, ValueError, ImportError):
            return False
        if len(rows) != len(vectors):
            return False
        _cache[d] = (rows, vectors, meta)
        return True


def _filter(chunks, line=None, volume=None):
    idx = range(len(chunks))
    if line:
        idx = [i for i in idx if chunks[i].get('line') == line]
    if volume:
        want = str(volume).lstrip('0')
        idx = [i for i in idx if str(chunks[i].get('volume', '')).lstrip('0') == want]
    return list(idx)


def search(query, top_k=3, line=None, volume=None, min_score=0.28):
    """返回 [{text, line, volume, chapter, score}]，按相似度降序。"""
    if not (query or '').strip() or not _load():
        return []
    chunks, vectors, _meta = _cache[_dir()]
    import numpy as np
    from embedding import embed
    q = embed(query)
    if q is None:
        return []
    qv = np.asarray(q, dtype='float32')
    norm = float(np.linalg.norm(qv))
    if not norm:
        return []
    qv = qv / norm
    if vectors.shape[1] != qv.shape[0]:
        # 换 embedding 模型（如 8GB → 12GB 档位切到 bge-m3）后维度不同，必须重建索引
        LOG.warning('novel index 维度 %d 与查询向量 %d 不一致：请重建索引（novel_index.py --build）',
                    vectors.shape[1], qv.shape[0])
        return []

    idx = _filter(chunks, line, volume)
    if not idx:
        return []
    mat = vectors[np.asarray(idx)]
    scores = mat.dot(qv)
    order = np.argsort(-scores)[:max(1, int(top_k)) * 4]
    # 同一章常常连续命中好几块，全塞进去等于复读同一段情节；先按章节去重，不够再放宽
    def _take(pos, used):
        score = float(scores[pos])
        if score < min_score:
            return None
        row = dict(chunks[idx[pos]])
        row['score'] = round(score, 3)
        used.add(row['i'])
        return row

    out, used, seen = [], set(), set()
    for pos in order:
        row = dict(chunks[idx[pos]])
        key = (row.get('volume'), row.get('chapter'))
        if key in seen:
            continue
        seen.add(key)
        picked = _take(pos, used)
        if picked:
            out.append(picked)
        if len(out) >= top_k:
            break
    if len(out) < top_k:
        for pos in order:
            if chunks[idx[pos]]['i'] in used:
                continue
            picked = _take(pos, used)
            if picked:
                out.append(picked)
            if len(out) >= top_k:
                break
    return out


_SENT_END = '。！？…」』”"\n'


def _clip(text, limit):
    """按句末标点截断，避免把半句话喂给模型。"""
    if not limit or len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(ch) for ch in _SENT_END[:-1])
    if cut >= limit * 0.5:
        return head[:cut + 1] + '…'
    return head.rstrip() + '…'


def format_excerpts(rows, limit=cfg.TOOL_NOVEL_EXCERPT_CHARS):
    """把检索结果压成给模型看的事实文本（带卷/章出处）。"""
    parts = []
    for r in rows:
        text = (r.get('text') or '').strip()
        if not text:
            continue
        chapter = (r.get('chapter') or '').strip()
        first = text.split('\n', 1)[0].strip()
        if chapter and (first == chapter or first.startswith(chapter)):
            text = text[len(first):].strip()
        if not text:
            continue
        text = _clip(text, limit)
        where = r.get('volume') or ''
        if chapter:
            where = '%s %s' % (where, chapter)
        where = where.strip()
        parts.append('《%s》%s' % (where, text) if where else text)
    return '\n'.join(parts)
