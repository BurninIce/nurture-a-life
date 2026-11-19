# -*- coding: utf-8 -*-
"""工具路由：这一轮要不要查、查哪个。

判定原则：**宁可漏查，不可错查**。检索结果会进 system，查错一次就等于
把一段无关的百科文本塞进她的当下——宁可让她凭记忆聊，也不要污染对话。

三档（都只用规则，不花模型调用）：
    A 显式请求（帮我查/搜一下…）        → web_search，最高优先级
    B 世界事实（天气/股价/比分…）+ 时间 + 疑问 → web_search
    C 新闻词 + 疑问或时间              → news_lookup
其余一律不查。以下一律不路由：
    - 带“别/不用/不要”的否定请求
    - 私人话题（你/我/记得/心情/喜欢…）走 B 档——那是在聊天，不是在查资料
    - 只说“今天”没有具体事象（“今天好累啊”）

群聊默认只在 @ 时路由（旁听不查），因为检索会显著拉长响应时间。
开关全部走 runtime_config，默认关闭。
"""
import re
from collections import namedtuple

import runtime_config as rc
from . import get as get_tool

Route = namedtuple('Route', 'tool args reason')

_EXPLICIT = ('查一下', '查查', '查一查', '查下', '搜一下', '搜一搜', '搜下',
             '帮我查', '帮忙查', '替我查', 'google', '谷歌', '百度一下')
_NEGATED = re.compile(r'(别|不用|不要|无需|不必|甭)\s*(查|搜|搜索|联网)')
_NEWS = ('新闻', '热搜', '头条', '时事')
# 只有「世界上的具体事象」才值得联网；泛化的“最近/现在”不算
_ENTITY = ('天气', '气温', '温度', '下雨', '降雨', '下雪', '暴雨', '台风',
           '地震', '雾霾', '空气质量',
           '股价', '股票', '汇率', '油价', '金价', '房价', '利率', '票价',
           '比分', '赛程', '比赛结果', '上映', '票房', '发售', '开售',
           '发布会', '政策', '新规', '航班', '签证', '开服', '版本更新')
_TIME = ('今天', '今日', '现在', '目前', '最近', '最新', '这周', '本周',
         '今年', '昨天', '明天', '这几天', '近期')
_QUESTION = ('?', '？', '吗', '呢', '什么', '怎么', '如何', '多少', '哪', '谁',
             '是否', '有没有', '是不是', '能不能')
# 私人话题：走 B 档时一律不查——她该凭记忆和关系回答，而不是念搜索结果
_PERSONAL = ('你', '我', '咱', '他', '她', '记得', '感觉', '觉得', '心情',
             '喜欢', '爱', '累', '难过', '开心', '想你', '陪我')
# 原作检索要「明确指向这本书」：只出现“小说”二字不路由（可能在聊别的小说）
_NOVEL = ('原作', '原作小说', '外传', '安可', '小说里', '小说中')
_VOL_RE = re.compile(r'第\s*([0-9一二三四五六七八九十百]+)\s*卷')
_CN_DIGITS = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
              '六': 6, '七': 7, '八': 8, '九': 9}


def _volume(text):
    """从「第5卷 / 第五卷 / 第十五卷」里取卷号，取不到返回空串。"""
    m = _VOL_RE.search(text or '')
    if not m:
        return ''
    raw = m.group(1)
    if raw.isdigit():
        return '%02d' % int(raw)
    total, cur = 0, 0
    for ch in raw:
        if ch == '十':
            total += (cur or 1) * 10
            cur = 0
        elif ch == '百':
            total += (cur or 1) * 100
            cur = 0
        else:
            cur = _CN_DIGITS.get(ch, 0)
    total += cur
    return '%02d' % total if total else ''


def _tool_enabled(name):
    tool = get_tool(name)
    return bool(tool and tool.enabled())


def _on(key, default, gid=None):
    """开关生效值：本群覆盖 > runtime_config > 代码默认。"""
    gid = str(gid or '').strip()
    if gid:
        try:
            import group_settings
            val = group_settings.get(gid, key)
            if val is not None:
                return bool(val)
        except Exception:
            pass
    return bool(rc.get(key, default))


def route(message, *, is_group=False, at_self=False, gid=None):
    """返回 Route 或 None。纯函数，不产生副作用。"""
    if not _on('tools_enabled', False, gid):
        return None
    if is_group:
        # 默认只有被 @ 才查；旁听消息不联网
        if not at_self and not _on('tool_router_group_at', False, gid):
            return None
    elif not rc.get('tool_router_private', True):
        return None

    text = (message or '').strip()
    if not text:
        return None
    query = text[:200]

    asking = any(k in text for k in _QUESTION)
    timed = any(k in text for k in _TIME)

    # 新闻：光出现“新闻”两个字不算（“别看新闻了”），要有疑问或时间指向
    if (any(k in text for k in _NEWS) and (asking or timed)
            and _tool_enabled('news_lookup')):
        return Route('news_lookup', {'query': query}, 'news')

    novel_hit = any(k in text for k in _NOVEL) or bool(_VOL_RE.search(text))
    if novel_hit and _tool_enabled('novel_lookup'):
        line = 'gaiden' if '外传' in text else ('anco' if '安可' in text else '')
        return Route('novel_lookup', {'query': query, 'line': line,
                                      'volume': _volume(text)}, 'novel')

    explicit = any(k in text for k in _EXPLICIT) and not _NEGATED.search(text)
    entity = any(k in text for k in _ENTITY)
    personal = any(k in text for k in _PERSONAL)
    if explicit and _tool_enabled('web_search'):
        return Route('web_search', {'query': query}, 'explicit')
    # 弱触发：具体事象 + 时间 + 疑问，且不是私人话题
    if entity and timed and asking and not personal and _tool_enabled('web_search'):
        return Route('web_search', {'query': query}, 'live_info')
    return None
