# -*- coding: utf-8 -*-
"""感知类工具：图片识别、链接抓取。

P0 阶段它们仍由 host 在「决策之前」调用（_enhance_text），
因为群发言决策需要看到完整内容；这里只统一超时/缓存/日志/失败语义。
未来若要做「再仔细看看这张图」，router 可以直接复用同一个工具。
"""
from . import register
from .base import Tool, ToolResult


class DescribeImageTool(Tool):
    name = 'describe_image'
    description = '识别一张图片的内容，返回一句话客观描述。'
    params = {'image_url': {'type': 'string', 'description': '图片 URL'}}
    required = ('image_url',)
    permission = 'egress'
    timeout = 30.0
    cache_ttl = 3600
    enabled_key = 'image_understand'
    default_enabled = True

    def run(self, ctx, image_url):
        import media_understand
        text = media_understand.describe_image(image_url)
        return ToolResult(bool(text), text, source='vision')


class FetchLinkTool(Tool):
    name = 'fetch_link'
    description = '抓取网页的标题与正文摘要。'
    params = {'url': {'type': 'string', 'description': '网页 URL'}}
    required = ('url',)
    permission = 'egress'
    timeout = 15.0
    cache_ttl = 3600
    enabled_key = 'link_fetch'
    default_enabled = True

    def run(self, ctx, url):
        import media_understand
        text = media_understand.fetch_link(url)
        if not text:
            return ToolResult(False, error='fetch_empty')
        # media_understand 返回 '标题｜摘要'，长度已由它自己限制，这里不重复裁剪
        return ToolResult(True, text, source='link')


register(DescribeImageTool())
register(FetchLinkTool())
