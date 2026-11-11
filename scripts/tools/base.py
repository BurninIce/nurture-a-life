# -*- coding: utf-8 -*-
"""Tool 协议：外部能力的统一形状。

一个 Tool 只描述「能做什么、参数是什么、失败怎么办」，不关心调用时机。
调用时机由 host（bridge / router）决定，模型不直接持有执行权——
这是本项目 2026-10-29 方案 B 的核心边界：

    检索类（搜索/新闻/小说/游戏状态） → 可交给 router 按需触发
    副作用类（发 QQ / 游戏操作）      → 永远留在 host，不做成自由调用工具

permission 分级：
    read   只读本地/远端状态，无副作用
    egress 会把内容发到外部服务（搜索、视觉识别）
    write  有副作用（预留，必须 host 二次确认后才允许执行）
"""


class ToolResult(object):
    """工具的统一返回。text 是给模型看的事实摘要，不是原始响应。"""

    def __init__(self, ok, text='', source='', error='', **meta):
        self.ok = bool(ok)
        self.text = text or ''
        self.source = source or ''
        self.error = error or ''
        self.meta = meta or {}

    def to_dict(self):
        d = {'ok': self.ok, 'text': self.text, 'source': self.source,
             'error': self.error}
        d.update(self.meta)
        return d

    def __repr__(self):
        return 'ToolResult(ok=%s, source=%r, chars=%d, error=%r)' % (
            self.ok, self.source, len(self.text), self.error)


class Tool(object):
    """工具基类。子类只需要填元信息并实现 run()。"""

    name = ''
    description = ''
    params = {}          # JSON Schema 的 properties
    required = ()        # 必填参数名
    permission = 'read'  # read / egress / write
    timeout = 10.0       # 建议值；实际超时由各工具内部网络调用保证
    cache_ttl = 0        # 秒；0 = 不缓存
    enabled_key = None   # runtime_config 里的开关键；None = 只看 default_enabled
    default_enabled = True
    owner = ''

    def enabled(self):
        import runtime_config as rc
        if self.enabled_key:
            return bool(rc.get(self.enabled_key, self.default_enabled))
        return bool(self.default_enabled)

    def schema(self):
        return {
            'type': 'object',
            'properties': dict(self.params),
            'required': list(self.required),
        }

    def run(self, ctx, **kwargs):
        raise NotImplementedError('%s.run' % (self.name or type(self).__name__))
