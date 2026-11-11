# -*- coding: utf-8 -*-
"""网页搜索工具（egress）。

后端目前用千问 compatible-mode 的 enable_search。**不要**在搜索失败时退回
「不联网直接生成」——那等于把幻觉包装成检索结果。失败就返回 ok=False，
由 render 注入「不许编」的约束。
"""
import rp_config as cfg
from rp_common import chat_qwen
from . import register
from .base import Tool, ToolResult

NO_RESULT_MARK = '未检索到可靠信息'


def search_timeout():
    """超时走 runtime_config 热读；import 时冻结会让面板改了不生效。"""
    import runtime_config as rc
    try:
        return float(rc.get('tool_search_timeout', cfg.TOOL_SEARCH_TIMEOUT))
    except (TypeError, ValueError):
        return float(cfg.TOOL_SEARCH_TIMEOUT)


def _clip(text, limit):
    text = (text or '').strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + '…'
    return text


def _search(prompt, timeout):
    """调一次联网检索并返回事实文本；失败返回 (False, 错误原因)。"""
    try:
        text = chat_qwen([{'role': 'user', 'content': prompt}],
                         model=cfg.TOOL_SEARCH_MODEL,
                         max_tokens=cfg.TOOL_SEARCH_MAX_TOKENS,
                         timeout=timeout,
                         enable_search=True)
    except Exception as e:
        return False, 'search_failed:%s' % e
    text = (text or '').strip()
    if not text:
        return False, 'empty_result'
    if NO_RESULT_MARK in text[:40]:
        return False, 'no_reliable_result'
    return True, text


class WebSearchTool(Tool):
    name = 'web_search'
    description = '联网检索一个事实性问题，返回简要事实摘要。'
    params = {
        'query': {'type': 'string', 'description': '要检索的问题或关键词'},
        'max_chars': {'type': 'integer', 'description': '返回摘要的最大字数'},
    }
    required = ('query',)
    permission = 'egress'
    timeout = cfg.TOOL_SEARCH_TIMEOUT
    cache_ttl = cfg.TOOL_WEB_CACHE_TTL
    enabled_key = 'web_search_enabled'
    default_enabled = False

    def run(self, ctx, query, max_chars=None):
        q = (query or '').strip()
        if not q:
            return ToolResult(False, error='empty_query')
        ok, text = _search(cfg.TOOL_SEARCH_PROMPT.format(query=q), search_timeout())
        if not ok:
            return ToolResult(False, error=text)
        return ToolResult(True, _clip(text, max_chars or cfg.TOOL_MAX_CHARS),
                          source='qwen_search')


register(WebSearchTool())
