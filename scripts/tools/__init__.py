# -*- coding: utf-8 -*-
"""工具注册表与统一执行入口。

调用方只依赖两个函数：

    tools.run(name, ctx, **args) -> ToolResult    # 永不抛异常
    tools.render_block(result)   -> str           # 可注入 system 的事实块

所有工具共享同一套语义：
    - 失败不抛异常，失败也要留日志（项目铁律：静默失败必须留痕）
    - 缓存键 = 工具名 + 参数
    - 每次调用记 ms / ok / source，便于定位「看着成功、其实返回空」的后端问题
"""
import logging
import time

from .base import Tool, ToolResult
from . import budget, cache

LOG = logging.getLogger('tools')

_REGISTRY = {}


def register(tool):
    if not tool.name:
        raise ValueError('tool.name 不能为空')
    _REGISTRY[tool.name] = tool
    return tool


def get(name):
    return _REGISTRY.get(name)


def all_tools():
    return dict(_REGISTRY)


def schemas(only_enabled=False):
    """工具清单（未来接原生 function calling 时直接喂给模型）。"""
    out = {}
    for name, tool in sorted(_REGISTRY.items()):
        if only_enabled and not tool.enabled():
            continue
        out[name] = {
            'description': tool.description,
            'parameters': tool.schema(),
            'permission': tool.permission,
        }
    return out


def _brief(args):
    """日志里只留参数摘要：图片 URL 可能有上千字符。"""
    parts = []
    for k, v in sorted((args or {}).items()):
        s = str(v)
        parts.append('%s=%s' % (k, s[:80] + ('...' if len(s) > 80 else '')))
    return ' '.join(parts)


def run(name, ctx=None, **kwargs):
    """执行工具。任何异常都转成 ok=False，绝不向上抛。"""
    tool = get(name)
    if tool is None:
        LOG.warning('tool=%s 未注册', name)
        return ToolResult(False, error='unknown_tool:%s' % name)
    if not tool.enabled():
        return ToolResult(False, error='disabled:%s' % name)

    cached = cache.get(name, kwargs)
    if cached is not None:
        cached.meta['cached'] = True
        LOG.info('tool=%s args=%s ok=%s cached=1', name, _brief(kwargs), cached.ok)
        return cached

    if tool.permission == 'egress':
        allowed, used, limit = budget.try_consume()
        if not allowed:
            LOG.warning('tool=%s 跳过：今日外部调用额度用尽 %s/%s', name, used, limit)
            return ToolResult(False, error='budget_exhausted', used=used, limit=limit)

    t0 = time.time()
    try:
        result = tool.run(ctx or {}, **kwargs)
        if not isinstance(result, ToolResult):
            result = ToolResult(bool(result), str(result or ''))
    except Exception as e:
        LOG.exception('tool=%s 执行异常', name)
        result = ToolResult(False, error='exception:%s' % e)
    result.meta.setdefault('ms', int((time.time() - t0) * 1000))
    result.meta.setdefault('cached', False)
    if result.ok:
        cache.put(name, kwargs, result, tool.cache_ttl)
    LOG.info('tool=%s args=%s ok=%s ms=%s source=%s',
             name, _brief(kwargs), result.ok, result.meta.get('ms'), result.source)
    return result


def render_block(result):
    from .render import render
    return render(result)


# 内置工具在注册表就绪后导入（它们通过 register() 自注册）
from . import media       # noqa: E402,F401
from . import web_search  # noqa: E402,F401
from . import news        # noqa: E402,F401
from . import novel       # noqa: E402,F401
from . import router      # noqa: E402,F401
