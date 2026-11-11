# -*- coding: utf-8 -*-
"""
语义检索（embedding）模块 — 本地 Ollama embedding，供记忆回忆检索使用。

- embed(text)：调 Ollama /api/embed，返回向量（带文本缓存）
- cosine(a, b)：余弦相似度
- 模型由 `hw_profile` 按显存推荐（12GB+ → bge-m3；8GB → bge-small-zh-v1.5，
  因为它要和 9B 生成模型共处一张卡，1.2G 的 bge-m3 会把 9B 挤出显存）
- 推荐模型没拉下来时**自动退回 bge-m3** 一次，不至于让检索直接哑掉
- 不依赖 rp_config（保持与 UI 配置解耦）
"""
import json
import logging
import time
import urllib.error
import urllib.request

import hw_profile as hw
import paths

_LOG = logging.getLogger('embedding')

OLLAMA_URL = 'http://127.0.0.1:%d/api/embed' % paths.PORT_OLLAMA
EMBED_MODEL = hw.EMBED_MODEL        # 按显存档位推荐（可用 NAL_EMBED 覆盖）
EMBED_FALLBACK = 'bge-m3'           # 兜底：库最大、中文最稳
_active = EMBED_MODEL

_cache = {}  # 文本 -> 向量（进程内缓存，事件库小可接受；重启重算）

# 失败熔断：Ollama 未启动/不可达时，单次调用要等连接超时（Windows 上 localhost
# 的 IPv6→IPv4 回退约 1s/次）；8 人群聊里每轮要 embed 查询 + 每条事件，
# 累积可到数秒。熔断后 BACKOFF 秒内直接返回 None（检索退化为近因），不再干等。
BACKOFF_SEC = 30
_down_until = 0.0
_was_down = False


def active_model():
    return _active


def _mark_down(reason=''):
    global _down_until, _was_down
    now = time.time()
    transition = now >= _down_until
    _down_until = now + BACKOFF_SEC
    _was_down = True
    if transition:
        # 持续故障时每个熔断窗口最多一条：热路径每轮都会调 embed
        _LOG.warning('embedding 不可用，%d 秒内退化为字面相似度（%s）',
                     BACKOFF_SEC, reason or '未知原因')


def _swap_model(name):
    """切换 embedding 模型时必须清缓存——不同模型的向量维度不同，混用会算出 0。"""
    global _active
    _active = name
    _cache.clear()


def _post(model, text):
    payload = {'model': model, 'input': text[:500]}
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read().decode('utf-8'))
    vec = resp.get('embeddings') or resp.get('embedding')
    if not vec:
        return None
    # /api/embed 返回 [[...]]（嵌套），/api/embeddings 返回 [...]（扁平）
    if isinstance(vec[0], list):
        vec = vec[0]
    return vec


def embed(text):
    """返回文本向量（缓存）。失败返回 None（熔断期内直接返回，不再尝试）。"""
    global _was_down
    if not text or not text.strip():
        return None
    if text in _cache:
        return _cache[text]
    if time.time() < _down_until:
        return None
    try:
        vec = _post(_active, text)
    except urllib.error.HTTPError as e:
        # 400/404 多半是"模型没拉下来"：退回 bge-m3 再试一次，别整条链路哑掉
        if e.code in (400, 404) and _active != EMBED_FALLBACK:
            _swap_model(EMBED_FALLBACK)
            try:
                vec = _post(_active, text)
            except Exception as exc:
                _mark_down('HTTP %s，兜底模型也失败：%s' % (e.code, exc))
                return None
        else:
            _mark_down('HTTP %s' % e.code)
            return None
    except Exception as e:
        _mark_down(str(e))
        return None
    if not vec:
        return None
    if _was_down:
        _was_down = False
        _LOG.info('embedding 已恢复（模型 %s）', _active)
    _cache[text] = vec
    return vec


def cosine(a, b):
    """余弦相似度（a/b 为向量，None 或维度不同返回 0）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
