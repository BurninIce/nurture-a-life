# -*- coding: utf-8 -*-
"""新闻/时事工具（egress）。

与 web_search 共用后端，但 prompt 不同：只取最近几天、按条输出事实，
避免模型把旧闻当新闻、或写成评论。
"""
import rp_config as cfg
from . import register
from .base import Tool, ToolResult
from .web_search import _clip, _search, search_timeout


class NewsTool(Tool):
    name = 'news_lookup'
    description = '检索最近几天的新闻要点。'
    params = {
        'query': {'type': 'string', 'description': '新闻主题'},
        'max_chars': {'type': 'integer', 'description': '返回摘要的最大字数'},
    }
    required = ('query',)
    permission = 'egress'
    timeout = cfg.TOOL_SEARCH_TIMEOUT
    cache_ttl = cfg.TOOL_NEWS_CACHE_TTL
    enabled_key = 'news_enabled'
    default_enabled = False

    def run(self, ctx, query, max_chars=None):
        q = (query or '').strip()
        if not q:
            return ToolResult(False, error='empty_query')
        ok, text = _search(cfg.TOOL_NEWS_PROMPT.format(query=q), search_timeout())
        if not ok:
            return ToolResult(False, error=text)
        return ToolResult(True, _clip(text, max_chars or cfg.TOOL_MAX_CHARS),
                          source='qwen_news')


register(NewsTool())
