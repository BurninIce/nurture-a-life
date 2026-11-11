# -*- coding: utf-8 -*-
"""记忆共享原语：时间标签、场景判定、取代链、注入模板、检索阈值与 embedding。

这些是 memory_card / memory_format / memory_retrieval 三个模块共用的叶子依赖。
单独成模块的目的只有一个：让依赖方向保持单向（三者 → 本模块），
不再互相 `import memory_card` 形成循环，也不需要把 import 藏在函数体里。

约定：
- 本模块**不 import memory_card**，也不读写 state/。
- embed / cosine / 模板缓存都是模块级可变属性；测试直接替换
  `memory_util.embed = fake`、`memory_util._TEMPLATES = {...}` 即可生效。
- 阈值与权重的注释包含实测依据，改动前先读清楚。
"""
import datetime
import re

from embedding import embed, cosine

# 每轮回忆检索条数
RECALL_K = 3

# 语义召回：与用户发言的余弦相似度阈值（超阈值才触发回忆注入）
RECALL_THRESHOLD = 0.55
# 记忆意图：用户发言与"问记忆"原型句的相似度阈值（意图识别而非关键词）
MEMORY_INTENT_THRESHOLD = 0.62
MEMORY_INTENT_PROTO = '你记得我们之前聊过什么吗？刚才说过的事情你还记得吗？'
# 意图召回时的语义门槛：比普通召回低，否则「什么时候」这类问句只剩最近几条，
# 模型找不到对应事件就会凭空编日期（2026-11-02 实测：两天前的河边散步被答成上个月初）
RECALL_INTENT_THRESHOLD = 0.45
# 排序加权：近因（7 天半衰期）+ 收藏（固定保留、优先注入）
RECALL_RECENCY_WEIGHT = 0.05
RECALL_FAVORITE_WEIGHT = 0.08


def _parse_time(text):
    """解析事件/条目的时间戳；兼容 'YYYY-MM-DD HH:MM'、带秒、纯日期。"""
    s = str(text or '').strip()
    if not s:
        return None
    for fmt, cut in (('%Y-%m-%d %H:%M', 16), ('%Y-%m-%d %H:%M:%S', 19),
                     ('%Y-%m-%d', 10)):
        try:
            return datetime.datetime.strptime(s[:cut], fmt)
        except ValueError:
            continue
    return None


def time_label(when, *, with_time=False, now=None):
    """把时间戳转成注入用的相对标签（今天/昨天/前天/N天前/绝对日期）。

    记忆注入要的是「这件事离现在多远」——只给绝对日期，模型还得自己算，
    实测会把 4 天前和昨天的事当成同一段过往。相对标签每轮现算，永远准确。
    with_time=True 时补上时分（回忆原话用）；纯日期输入不会补出假的 00:00。
    """
    s = str(when or '').strip()
    dt = _parse_time(s)
    if dt is None:
        return ''
    now = now or datetime.datetime.now()
    days = (now.date() - dt.date()).days
    if days < 0:
        days = 0
    if days == 0:
        label = '今天'
    elif days == 1:
        label = '昨天'
    elif days == 2:
        label = '前天'
    elif days < 7:
        label = '%d天前' % days
    elif dt.year == now.year:
        label = '%d月%d日' % (dt.month, dt.day)
    else:
        label = '%d年%d月%d日' % (dt.year, dt.month, dt.day)
    if with_time and len(s) >= 16:
        label += ' %02d:%02d' % (dt.hour, dt.minute)
    return label


def episode_time(ep):
    """事件发生时间：优先话题开始时间。

    话题跨零点闭合时 time 是末轮（00:10），用它会把这件「昨晚的事」记成今天凌晨；
    start_time 是话题第一轮，才对应「昨晚」。
    """
    return (ep or {}).get('start_time') or (ep or {}).get('time') or ''


# 注入模板（从 prompts/injection_templates.md 读取，程序外内容）
_TEMPLATES = None


def _tpl():
    global _TEMPLATES
    if _TEMPLATES is None:
        from rp_common import load_templates
        _TEMPLATES = load_templates()
    return _TEMPLATES


# 一次性场景状态（「现在和你一起在咖啡店」「刚挑完钢笔」）不是长期记忆。
# 它一旦进入记忆卡就会被每轮注入，把之后所有对话钉在同一个地点：
# 2026-10-29 实测，10-28 的一条「现在和你一起在咖啡店」让当天书店场景的四条事件
# 全被边界模型记成咖啡店，且被重归纳一代代抄下去。
# 只用于 self_disclosure（角色自述），不碰 facts——「你现在在北京工作」是真实用户事实。
_SCENE_STATUS_RE = re.compile(
    r'(?:现在|此刻|这会儿|眼下|刚刚?)[^，。；]{0,12}(?:和你|陪你|一起|在|正)'
    r'|(?:正在|正)[^，。；]{0,8}(?:陪你|和你一起|在|坐|站|走|待|挑|翻|喝|吃|聊|看)'
    r'|刚[挑吃喝完看完说聊买做收找洗切煮烤借还捡拾打开合上放回拿起放下]'
)


def is_scene_status(text):
    """这条角色自述是不是「当下的场景/动作」而非稳定事实。"""
    return bool(_SCENE_STATUS_RE.search(text or ''))


# ---------- 取代链（记忆的新旧版本控制） ----------
# 设计要点：取代关系用**事件 id**作锚点，不用卡片条目序号。
#   卡片每轮重归纳整组重建（card[key] = items[:N]），条目没有稳定 id，序号会漂移；
#   事件库不可变、id 单调递增，是唯一能跨重归纳存活的引用。
# 存放位置：data 顶层 'supersedes'，**不在 card 里**——card 会被整组覆盖冲掉。

def _live_supersedes(data):
    """当前有效的取代关系。

    一条关系生效要求 new 与 old 都仍在库里（存在且未 inactive）：
      - **new 失效/被删 → 关系作废，被取代者自动恢复**
        （对应「删掉取代它的那条，被取代的重新生效」）
      - old 失效 → 关系无意义，跳过
    这样 forget() 淡出旧事件时，取代链会自然收敛，不需要额外清理。
    """
    eps = {e.get('id'): e for e in (data.get('episodes') or [])}
    live = []
    for rel in (data.get('supersedes') or []):
        if not isinstance(rel, dict):
            continue
        new, old = rel.get('new'), rel.get('old')
        if new is None or old is None or new == old:
            continue
        ne, oe = eps.get(new), eps.get(old)
        if ne is None or oe is None:
            continue
        if ne.get('inactive') or oe.get('inactive'):
            continue
        live.append(rel)
    return live


def superseded_ep_ids(data):
    """处于「已被取代」状态的事件 id 集合（检索层据此排除旧事件）。"""
    return {rel['old'] for rel in _live_supersedes(data)}


def superseded_map(data):
    """{被取代事件 id: {取代它的事件 id, ...}}（注入层据此判断条目是否过期）。

    比 superseded_ep_ids 多一层「谁取代了它」：归并式条目（"现居东京，曾住大阪"
    同时引用 #5 与 #12）必须能看出自己**已吸收新事实**，否则会被误判过期、
    整条不注入——真实模型就是这么归纳的（实测 qwen-max 会把新旧合并成一条）。
    """
    out = {}
    for rel in _live_supersedes(data):
        out.setdefault(rel['old'], set()).add(rel['new'])
    return out


def item_expired(item, superseded):
    """卡片条目是否已过期（应停止注入）。

    superseded: superseded_map() 的 {old: {new}}（**必须传 dict**，传 set/None 一律不过滤）。
    过期条件 = 引用了被取代的旧事件，**且**没有同时引用取代它的事件。
      src=[5]      + 12 取代 5  → 过期（只基于旧事实）
      src=[5,12]   + 12 取代 5  → **不过期**（已合并新事实）
      src=[]                    → 不过期（判不出就显示，宁可多给不可错删）
    类型不对时**不做判断**（返回 False）：传错只会残留过期事实，而误判全部过期
    会让角色当场失忆——两者代价差一个量级，方向必须保守。
    """
    src = set(item.get('src') or [])
    if not src or not isinstance(superseded, dict):
        return False
    dead = src & set(superseded)
    if not dead:
        return False
    for d in dead:
        if src & set(superseded.get(d) or ()):
            return False
    return True