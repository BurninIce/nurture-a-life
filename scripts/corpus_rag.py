# -*- coding: utf-8 -*-
"""
语料 RAG：角色语料（samples_clean.jsonl）语义检索，作说话风格参考注入 system。

- 检索源：984 条角色语料（静态风格基准，与记忆系统的动态互动记忆分离）
- 机制：用户发言 embedding → 语料 embedding 余弦 → top-N（无阈值，语料均为角色风格）
- 缓存：embedding 预计算存 state/personas/<id>/corpus_embeddings.json（gitignore，首次构建 ~1 分钟）
- 复用 scripts/embedding.py（bge-m3），无硬编码关键词
"""
import json
import os

from embedding import embed, cosine
import persona

TOP_N = 2  # 注入条数（默认，可被 rp_config 覆盖）

_samples = {}      # samples_path -> [{text, type, ...}]
_embeddings = {}   # cache_path -> [vec, ...]


def available(pid=None):
    return bool(persona.corpus_spec(pid))


def _load_samples(path):
    if path in _samples:
        return _samples[path]
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            text = (obj.get('text') or '').strip()
            if not text:
                continue
            rows.append({'text': text[:120],
                         'type': obj.get('type'),
                         'scene': obj.get('scene') or '',
                         'trait': obj.get('trait') or ''})
    _samples[path] = rows
    return rows


def _load_embeddings(cache_path, samples):
    if cache_path in _embeddings:
        return _embeddings[cache_path]
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding='utf-8') as f:
                _embeddings[cache_path] = json.load(f)
            return _embeddings[cache_path]
        except (OSError, ValueError):
            pass
    # 首次构建：批量 embedding（Ollama /api/embed 一次传多条，快得多）
    import urllib.request
    from embedding import OLLAMA_URL, EMBED_MODEL
    texts = [s['text'] for s in samples]
    vecs = [None] * len(texts)
    # 分批（Ollama 批量上限 ~64）
    for start in range(0, len(texts), 32):
        batch = texts[start:start + 32]
        try:
            payload = {'model': EMBED_MODEL, 'input': batch}
            req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode('utf-8'),
                                         headers={'Content-Type': 'application/json'})
            resp = json.loads(urllib.request.urlopen(req, timeout=60).read().decode('utf-8'))
            embs = resp.get('embeddings') or []
            for i, v in enumerate(embs):
                vecs[start + i] = v
        except Exception as e:
            print('batch %d 失败: %s' % (start, e))
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(vecs, f)
    _embeddings[cache_path] = vecs
    return vecs


def search(query, top_n=TOP_N, pid=None):
    """返回当前角色语料里与用户发言最相关的 top_n 条；无语料角色返回 []。"""
    spec = persona.corpus_spec(pid)
    if not spec or not os.path.isfile(spec.get('samples') or ''):
        return []
    samples = _load_samples(spec['samples'])
    if not samples:
        return []
    vecs = _load_embeddings(spec['cache'], samples)
    q_vec = embed(query or '')
    if q_vec is None:
        return []
    scored = []
    for s, v in zip(samples, vecs):
        if v is None:
            continue
        sim = cosine(q_vec, v)
        scored.append((sim, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:top_n]]


def format_rag(samples, pid=None):
    """【语料参考】块：检索到的当前角色语料样本（风格示例，非照抄）。"""
    if not samples:
        return ''
    from rp_common import load_templates
    t = load_templates()
    spec = persona.corpus_spec(pid) or {}
    block_fmt = (spec.get('header') or t.get(
        'RAG_BLOCK_FORMAT',
        '【语料参考】过去在类似情境下说过的话（参考其口吻与句式，不照搬内容）：'))
    item_fmt = t.get('RAG_ITEM_FORMAT', '- 「{items}」')
    lines = [block_fmt]
    for s in samples:
        txt = s.get('text', '').strip()
        if txt:
            lines.append(item_fmt.format(items=txt))
    return '\n'.join(lines)
