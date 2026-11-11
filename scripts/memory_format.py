# -*- coding: utf-8 -*-
"""记忆注入格式化：记忆卡摘要、关系块、回忆块。

从 memory_card.py 拆出，纯展示层，不读写 state。
模板、时间与取代判定统一来自 memory_util（叶子模块），依赖方向单向；
测试里直接替换 `memory_util._TEMPLATES` 仍然生效。
"""
import card_source
import memory_util as mu
import rp_config as cfg


def _person(txt):
    """人称净化：把归纳模型的泛称「用户」换成「你」（仅此一词）。

    记忆卡是 per-uid 的，注入对象就是卡主本人，所以「用户」恒指对方、「你」恒等于它。
    不清洗的后果很直接：system 里出现第三人称「用户」，模型会把这句话
    当成「有个叫用户的人在旁边」，于是把对方的事实说成自己的。

    **「对方」刻意不替换**：它是相对代词，指代随语境翻转——
    「和用户一起在咖啡店」里的用户=卡主（该换），
    但「会追问确认对方态度」里的对方=角色（换了就变成"确认你自己的态度"）。
    统一替换会造出比原文更糟的句子，所以只处理指代唯一确定的「用户」。
    """
    return (txt or '').replace('用户', '你')


def _card_item_text(item, text, tpl):
    """卡片条目带日期：[10月31日] 文案。没有日期的老条目不硬凑。"""
    when = mu.time_label(item.get('time') if isinstance(item, dict) else '')
    if not when:
        return text
    fmt = tpl.get('CARD_ITEM_FORMAT', '[{when}] {text}')
    try:
        return fmt.format(when=when, text=text)
    except (KeyError, IndexError, ValueError):
        return '[%s] %s' % (when, text)


def format_card(card, short=False, superseded=None, include_user=True):
    """记忆卡摘要（注入 system，<300 token）。

    include_user=False：脱敏模式，只保留 self_disclosure（角色自己的事），
    不注入 facts / impressions / reflections（关于用户的画像）。

    superseded: `superseded_map(data)` 的结果（{旧事件id: {取代者id}}）。
                **只作用于 facts**（见下方注释）。src 命中映射的条目不注入，
                除非它同时引用了取代者（归并式条目已吸收新事实）。判据见 item_expired()。
    **过滤必须发生在 [:3] 截断之前**：否则「前 3 条恰好全过期」会让整块注入变空，
    表现为角色突然失忆，且现象与「记忆损坏」极像、极难定位。

    每条都带日期标签（今天/昨天/N天前/绝对日期）：条目会在卡里存活很久，
    不带日期时模型无法判断「住在大阪」是上周还是半年前的事。
    """
    superseded = superseded or {}

    def _live(items):
        return [it for it in items or [] if not mu.item_expired(it, superseded)]

    t = mu._tpl()

    def _join(items):
        return '；'.join(_card_item_text(it, _person(it['text']), t) for it in items)

    lines = []
    # **取代过滤只作用于 facts**：搬家/改名这类取代针对的是「会随现实改变的事实」，
    # 而 impressions 是性格特质、self_disclosure 是角色自述，一条事件常混着多个话题
    # （#5「住大阪 + 在捣鼓发动机」被 #12「搬到东京」取代，不该把"对机械有兴趣"一并作废）。
    # 对这两类做事件级过滤会大面积误杀，表现为角色性格突然变薄。
    # 角色可能被叫的任意名字（短名 + 别名）；facts 里出现这些名字 = 把她的自述
    # 误归成了用户事实，不注入。
    self_names = cfg.self_names()
    if include_user and card['facts']:
        facts = [it for it in _live(card['facts'])
                 if not any(n in it['text'] for n in self_names)][:3]
        if facts:
            lines.append(t.get('CARD_FACTS_LABEL', '关于你：') + _join(facts))
    if include_user and card['impressions']:
        lines.append(t.get('CARD_IMPRESSIONS_LABEL', '对你的印象：')
                     + _join(card['impressions'][:3]))
    if card['self_disclosure']:
        # 一次性场景状态不注入：旧数据里残留的「现在和你一起在咖啡店」会把每一轮
        # 都钉在同一个地点，必须在截断之前滤掉（过滤顺序同 facts 的过期过滤）。
        sd = [it for it in card['self_disclosure']
              if not mu.is_scene_status(it.get('text'))][:3]
        if sd:
            label = t.get('CARD_DISCLOSURE_LABEL', '{name}曾说：')
            try:
                label = label.format(name=cfg.self_name())
            except (KeyError, IndexError, ValueError):
                label = cfg.self_name() + '曾说：'
            lines.append(label + _join(sd))
    # 反思层：只注入 importance>=2 的（importance=1 低置信不注入，抑制自我强化）
    refl = [r for r in card.get('reflections', [])
            if include_user and r.get('importance', 1) >= 2]
    if refl:
        # 主语必须是「你」：反思记的是**对方的**行为模式。旧版裸写「反思：」，
        # 注入后紧跟在「角色曾说」下面，模型读成「我的性格特点」，
        # 于是把"聊到时间就追问不休"当成自己的人设。
        label = t.get('CARD_REFLECTIONS_LABEL', '你的习惯：')
        lines.append(label + _join(refl[:3]))
    return '\n'.join(lines)


def relation_block(card, group=False):
    """关系块：按当前说话人的三值分档给言行指引（角色卡 relationship），不暴露数值。"""
    return card_source.relation_text(card, group=group)


def _fill_when(fmt, when, **kw):
    """渲染回忆行：模板有 {when} 就用它，老模板没有则把日期前缀补在最前面。"""
    if '{when}' in fmt:
        return fmt.format(when=('[%s] ' % when) if when else '', **kw)
    text = fmt.format(**kw)
    if not when:
        return text
    return ('[%s] ' % when) + text.lstrip('- ')


def recall_block(episodes):
    """回忆块：检索到的事件，突出用户原话（最直接的"记得"证据）。

    每行都带「这件事离现在多远」（昨天 21:42 / 4天前 / 10月28日）：
    只有原话没有时间，模型会把一周前的事当成刚发生的事，答「昨天」时当然会错。
    """
    if not episodes:
        return ''
    t = mu._tpl()
    said_fmt = t.get('RECALL_SAID_FORMAT', '- {when}你说过：「{said}」')
    summary_fmt = t.get('RECALL_SUMMARY_FORMAT', '- {when}{summary}')
    header = cfg.RECALL_HEADER
    lines = [header]
    for ep in episodes[:mu.RECALL_K]:
        when = mu.time_label(mu.episode_time(ep), with_time=True)
        user = ep.get('user_said', '')
        if user:
            lines.append(_fill_when(said_fmt, when, said=user[:50]))
        else:
            # 摘要里也可能残留「用户」（老事件就是这样生成的），一并净化
            lines.append(_fill_when(summary_fmt, when,
                                    summary=_person((ep.get('summary') or '')[:50])))
    return '\n'.join(lines)
