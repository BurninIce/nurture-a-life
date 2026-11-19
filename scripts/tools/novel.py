# -*- coding: utf-8 -*-
"""原作小说检索工具（read，本地索引，不出网）。

与 web_search 的区别：不联网、不花检索额度、延迟只有一次本地 embedding；
但只在明确提到原作/卷数/外传时才路由，避免把闲聊变成读书会。
"""
import rp_config as cfg
from . import register
from .base import Tool, ToolResult


class NovelLookupTool(Tool):
    name = 'novel_lookup'
    description = '在《原作小说》原作小说（含外传/安可短篇）里检索情节片段。'
    params = {
        'query': {'type': 'string', 'description': '要查的情节或人物问题'},
        'line': {'type': 'string', 'description': 'main / gaiden / anco，空=全部'},
        'volume': {'type': 'string', 'description': '卷号，如 05'},
    }
    required = ('query',)
    permission = 'read'
    timeout = 20.0
    cache_ttl = cfg.TOOL_NOVEL_CACHE_TTL
    enabled_key = 'novel_enabled'
    default_enabled = False

    def enabled(self):
        import persona
        return super(NovelLookupTool, self).enabled() and \
            bool(persona.capabilities().get('novel'))

    def run(self, ctx, query, line='', volume=''):
        import novel_rag
        q = (query or '').strip()
        if not q:
            return ToolResult(False, error='empty_query')
        if not novel_rag.available():
            return ToolResult(False, error='index_missing')
        rows = novel_rag.search(q, top_k=cfg.TOOL_NOVEL_TOP_K,
                                line=line or None, volume=volume or None,
                                min_score=cfg.TOOL_NOVEL_MIN_SCORE)
        if not rows:
            return ToolResult(False, error='no_hit')
        text = novel_rag.format_excerpts(rows, cfg.TOOL_NOVEL_EXCERPT_CHARS)
        if not text:
            return ToolResult(False, error='empty_excerpt')
        return ToolResult(True, text, source='novel',
                          hits=[(r.get('volume'), r.get('chapter'), r.get('score'))
                                for r in rows])


register(NovelLookupTool())
