# -*- coding: utf-8 -*-
"""OneBot 入站消息的纯解析层：事件 → Msg、CQ 码/消息段归一化、长回复切分。

从 qq_bridge.py 拆出，目的有两个：
  - qq_bridge 只保留调度与生成，不再混协议解析；
  - split_reply / parse_message 等纯函数可被 bridge_commands 顶层导入，
    不再需要函数内 `from qq_bridge import ...` 绕循环依赖。

这里不做发言决策、不碰记忆写入，也不负责发送。
"""
import json
import re
import time
from html import unescape

import member_card
import persona
import rp_config as cfg

# OneBot 消息段 / CQ 码
_CQ_AT = re.compile(r'\[CQ:at,qq=([^,\]]+)(?:,name=([^\]]+))?\]')
_CQ_IMAGE = re.compile(r'\[CQ:image[^\]]*\]')
_CQ_FACE = re.compile(r'\[CQ:face[^\]]*\]')
_CQ_REPLY = re.compile(r'\[CQ:reply,id=([^,\]]+)[^\]]*\]')
_CQ_OTHER = re.compile(r'\[CQ:[^\]]+\]')
# 从 CQ:image 码里抠 url 字段（NapCat 图片段带 url= 临时下载地址）
_CQ_IMAGE_URL = re.compile(r'\[CQ:image[^\]]*?url=([^,\]]+)')
# 从文本里识别裸链接
_URL_IN_TEXT = re.compile(r'https?://[^\s\u4e00-\u9fff，。！？、；：""''<>（）()【】]+')

PLACEHOLDER_IMAGE = '[图片]'
PLACEHOLDER_FACE = '[表情]'

# QQ 分享卡片（json/xml 段）：NapCat 只给原始串，标题和跳转链接要自己抠。
# 抓取走 _URL_IN_TEXT，所以片段里的 URL 必须原样放在结尾。
_CARD_URL_KEYS = ('jumpUrl', 'jump_url', 'qqdocurl', 'url', 'preview')
_XML_TEXT_TAG = re.compile(r'<(title|summary|desc)[^>]*>(.*?)</\1>', re.S)
_XML_URL_ATTR = re.compile(r'url="([^"]+)"')
_CDATA = re.compile(r'<!\[CDATA\[(.*?)\]\]>', re.S)


def _is_http(u):
    return isinstance(u, str) and u.strip().lower().startswith(('http://', 'https://'))


def _json_card(raw):
    """QQ json 卡片 → (title, desc, url)。

    结构随 app 变（structmsg 的 news/detail_1、miniapp 的 detail_1…），
    统一按字段名浅递归找，不绑定具体 app。
    """
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except Exception:
            return '', '', ''
    elif isinstance(raw, dict):
        obj = raw
    else:
        return '', '', ''
    got = {'title': '', 'desc': '', 'url': ''}

    def walk(o):
        if isinstance(o, list):
            for v in o:
                walk(v)
            return
        if not isinstance(o, dict):
            return
        if not got['title']:
            for k in ('title', 'prompt'):
                v = o.get(k)
                if isinstance(v, str) and v.strip():
                    got['title'] = v.strip()
                    break
        if not got['desc']:
            for k in ('desc', 'summary'):
                v = o.get(k)
                if isinstance(v, str) and v.strip():
                    got['desc'] = v.strip()
                    break
        if not got['url']:
            for k in _CARD_URL_KEYS:
                if _is_http(o.get(k)):
                    got['url'] = o[k].strip()
                    break
        for v in o.values():
            walk(v)

    walk(obj)
    return got['title'], got['desc'], got['url']


def _xml_card(raw):
    """QQ xml 卡片（<msg>…<title>/<summary>…<source url="…">）→ (title, desc, url)。"""
    raw = unescape(str(raw or ''))
    title = desc = ''
    for m in _XML_TEXT_TAG.finditer(raw):
        val = re.sub(r'<[^>]+>', '', _CDATA.sub(r'\1', m.group(2))).strip()
        if not val:
            continue
        if m.group(1) == 'title' and not title:
            title = val
        elif not desc:
            desc = val
    m = _XML_URL_ATTR.search(raw)
    return title, desc, (m.group(1).strip() if m else '')


def _card_fragment(title, desc, url):
    """卡片 → 进上下文的文本片段；没有可读内容时返回空串。"""
    clean = lambda s: re.sub(r'[（）【】\r\n]+', ' ', s or '').strip()
    title = clean(title)[:60]
    desc = clean(desc)[:80]
    if desc and title and desc != title:
        label = '%s｜%s' % (title, desc)
    else:
        label = title or desc
    if label:
        return ('（链接：%s）' % label) + (url or '')
    return url or ''


class Msg(object):
    """一条入站消息（已从 OneBot 结构里抽平）。"""
    __slots__ = ('uid', 'nick', 'gid', 'text', 'mid', 'at_self', 'reply_to_her',
                 'ts', 'is_group', 'images', 'links', 'want_tts', 'tts_style', 'ats',
                 'reply_mid')

    def __init__(self, uid, nick, gid, text, mid, at_self=False,
                 reply_to_her=False, ts=None, is_group=True):
        self.uid = str(uid)
        self.nick = nick or self.uid
        self.gid = str(gid) if gid else ''
        self.text = text or ''
        self.mid = str(mid or '')
        self.at_self = at_self
        self.reply_to_her = reply_to_her
        self.ts = ts or time.time()
        self.is_group = is_group
        self.images = []   # 图片 URL 列表（识别用）
        self.links = []    # 链接 URL 列表（抓取用）
        # 引用了哪条消息（引用段只给 message_id）；引用的图要回消息缓存里取
        self.reply_mid = ''
        self.want_tts = False   # 这条消息触发了语音输出（/tts 指令）
        self.tts_style = ''     # 'daily' / 'battle'
        # 本条 @ 了谁：[{'uid','nickname'}...]（不含 @她自己的那段）。
        # 2026-10-28：以前只在文本里拼成「@名字」就把 qq 号丢了，
        # 于是「@某甲 这人喜欢猫」进记忆后无法确定"这人"是谁 —— 归属做不成。
        # at 是**确定性**依据，比让模型猜昵称可靠得多。
        self.ats = []

    def __repr__(self):
        return '<Msg %s@%s %r>' % (self.nick, self.gid or 'private', self.text[:20])


def _resolve_at_name(qq, name):
    """@ 段缺昵称时，从成员卡反查；查不到才回落 QQ 号。

    协议层偶尔只给 qq 号，不补昵称，模型就会把数字当成名字（2026-10-28 /
    2026-11-01 两次把「群友」的标签写到别人头上，源头都是 about 变成纯数字）。
    成员卡是本地已有的稳定昵称源，优先用它。
    """
    name = (name or '').strip()
    if name:
        return name
    try:
        nick = (member_card.load(qq).get('nickname') or '').strip()
        if nick and nick != str(qq):
            return nick
    except Exception:
        pass
    return str(qq)


def _text_from_cq(raw, self_id):
    """字符串形式的消息（部分实现用 CQ 码）→ (纯文本, at_self, reply_id, images, ats)。

    images: 图片 url 列表（去重保序），供视觉识别用。
    ats:    @ 到的其他人 [{'uid','nickname'}...]，供记忆归属定位（不含 @她自己的）。
    """
    at_self = False
    reply_id = ''
    images = []
    ats = []

    def _at(m):
        nonlocal at_self
        qq = (m.group(1) or '').strip()
        name = (m.group(2) or '').strip()
        if str(qq) == str(self_id):
            at_self = True
            return '@%s' % persona.direct_name()
        if qq:
            who = _resolve_at_name(qq, name)
            ats.append({'uid': qq, 'nickname': who})
            return '@%s' % who
        return '@%s' % name

    m_reply = _CQ_REPLY.search(raw or '')
    if m_reply:
        reply_id = (m_reply.group(1) or '').strip()
    # 先抠图片 url（在替换成占位符之前）
    for m in _CQ_IMAGE_URL.finditer(raw or ''):
        u = (m.group(1) or '').strip()
        if u and u not in images:
            images.append(u)
    text = _CQ_REPLY.sub('', raw or '')
    text = _CQ_AT.sub(_at, text)
    text = _CQ_IMAGE.sub(PLACEHOLDER_IMAGE, text)
    text = _CQ_FACE.sub(PLACEHOLDER_FACE, text)
    text = _CQ_OTHER.sub('', text).strip()
    return text, at_self, reply_id, images, ats


def _text_from_segments(segs, self_id):
    """数组形式的消息段 → (纯文本, at_self, reply_id, images, ats)。

    images: 图片 url 列表（去重保序），供视觉识别用。
    ats:    @ 到的其他人 [{'uid','nickname'}...]，供记忆归属定位。
    """
    at_self = False
    reply_id = ''
    parts = []
    images = []
    ats = []
    for s in segs or []:
        if not isinstance(s, dict):
            continue
        typ = s.get('type')
        data = s.get('data') or {}
        if typ == 'text':
            parts.append(str(data.get('text') or ''))
        elif typ == 'at':
            qq = str(data.get('qq') or '').strip()
            name = str(data.get('name') or '').strip()
            if qq == str(self_id):
                at_self = True
                parts.append('@%s' % persona.direct_name())
            else:
                who = _resolve_at_name(qq, name) if qq else name
                if qq:
                    ats.append({'uid': qq, 'nickname': who})
                parts.append('@%s' % who)
        elif typ == 'image':
            parts.append(PLACEHOLDER_IMAGE)
            u = str(data.get('url') or '').strip()
            if u and u not in images:
                images.append(u)
        elif typ == 'face':
            parts.append(PLACEHOLDER_FACE)
        elif typ == 'reply':
            reply_id = str(data.get('id') or '').strip()
        elif typ in ('json', 'xml'):
            # 分享卡片：标题进文本，URL 留给 parse_message 收进 links 供 _enhance_text 抓取
            card = _json_card(data.get('data')) if typ == 'json' else _xml_card(data.get('data'))
            frag = _card_fragment(*card)
            if frag and frag not in parts:
                parts.append(frag)
        # record / video / file 等：不进上下文（暂无多模态）
    return ''.join(parts).strip(), at_self, reply_id, images, ats


def parse_message(ev, self_id='', our_mids=frozenset()):
    """OneBot 事件 → Msg；不是消息事件返回 None。"""
    if ev.get('post_type') != 'message':
        return None
    mt = ev.get('message_type')
    if mt not in ('group', 'private'):
        return None
    sender = ev.get('sender') or {}
    uid = ev.get('user_id')
    gid = ev.get('group_id') if mt == 'group' else ''
    nick = sender.get('card') or sender.get('nickname') or str(uid or '')

    raw_msg = ev.get('message')
    if isinstance(raw_msg, list):
        text, at_self, reply_id, images, ats = _text_from_segments(raw_msg, self_id)
    else:
        text, at_self, reply_id, images, ats = _text_from_cq(str(raw_msg or ''), self_id)

    msg = Msg(uid=uid, nick=nick, gid=gid, text=text, mid=ev.get('message_id'),
              at_self=at_self, reply_to_her=(reply_id in our_mids),
              ts=float(ev.get('time') or time.time()), is_group=(mt == 'group'))
    msg.images = images
    msg.ats = ats
    msg.reply_mid = reply_id
    # 链接：从文本里裸链接提取（图片 url 不算）
    msg.links = list(dict.fromkeys(m.group(0) for m in _URL_IN_TEXT.finditer(text)))
    return msg


def has_real_text(text):
    """只有占位符（[图片]/[表情]）的消息不值得进上下文。"""
    t = (text or '')
    t = t.replace(PLACEHOLDER_IMAGE, '').replace(PLACEHOLDER_FACE, '')
    t = t.replace('@%s' % persona.direct_name(), '')
    return bool(t.strip(' 　\n\t，。！？~～'))


def _turns_for_reply(turns, msg):
    """构造 messages 用的群聊历史：剔除"当前这条"在 turns 里已有的副本。

    根因（2026-10-28 实际事故）：handle_group 在 submit **之前**就 add_incoming(msg)，
    于是 turns 末尾已经含当前这句；_run_job 又以 message=msg.text 追加一次，
    模型看到同一个人连着说两遍同一句话，就把它解释成"你连发两次"——
    实测每一句被 @ 的话都触发这句套话，且它会被下一轮的 turns 继续喂回去，越滚越牢。

    优先按 mid 精确剔除：排队时 turns 里可能已有**更晚**的消息，
    不能简单砍掉尾巴（否则会丢真实上下文）。
    mid 缺失（部分实现不给 message_id）时，回落为剔除最后一条同昵称同文本的。
    """
    turns = list(turns or [])
    mid = getattr(msg, 'mid', None)
    if mid not in (None, ''):
        return [t for t in turns
                if t.get('self') or str(t.get('mid') or '') != str(mid)]
    text = (getattr(msg, 'text', '') or '').strip()
    nick = (getattr(msg, 'nick', '') or '').strip()
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        if (not t.get('self')
                and (t.get('text') or '').strip() == text
                and (t.get('nickname') or '').strip() == nick):
            return turns[:i] + turns[i + 1:]
    return turns


def _repeats_self(reply, turns, min_len=8, limit=3, scan=12):
    """这句话是否在复读她最近几句里的长片段（口头禅固化）。

    事故（2026-10-28 15:19-15:46）：她先说了一次「连发两次……是怕我装作没看见吗」，
    这句进了 assistant 历史，之后**每一句被 @ 的话**都套同一模板，7 连击。
    角色卡里写着「不重复说话」，但对这种模板化复读不起作用——模型在历史里看到
    "我上回就是这么说的"，会把它当成自己的说话方式继续沿用。这里是硬兜底。

    判据：与最近 limit 条自己的回复存在 ≥min_len 的公共子串。用子串而不是整体相似度，
    是因为复读往往是"同一句模板 + 换半句尾巴"（相似度算下来才 0.6 左右，卡不住）。
    """
    reply = (reply or '').strip()
    if len(reply) < min_len:
        return False
    prev = [(t.get('text') or '').strip()
            for t in (turns or [])[-scan:] if t.get('self')]
    prev = [p for p in prev if len(p) >= min_len][-limit:]
    for p in prev:
        for i in range(len(reply) - min_len + 1):
            if reply[i:i + min_len] in p:
                return True
    return False


def _claims_resend(reply):
    """这句话是否在断言"对方发了几次"（她无权知道的事实）。

    事故（2026-10-28）：群友追问「所以到底为啥会说连发两次」时，她答
    「因为刚才那条消息重复发送了两次呀」——把自己先前随口一说的判断
    升级成了事实陈述。真实发送次数只有桥接层能统计，且从不告诉她。
    群约束里已禁止，这里是确定性兜底（prompt 约束在 qwen-flash 上压不干净：
    实测旧约束 9 次采样仍有 2 次议论发送次数）。
    """
    t = reply or ''
    return any(w in t for w in cfg.RESEND_CLAIM_WORDS)


def split_bubbles(text, limit=None):
    """按换行把回复拆成独立消息（她可以自己补一句），最多 limit 条。

    超出上限的行并进最后一条，用换行保留原本的分行，不丢内容。
    """
    limit = max(1, limit or cfg.BRIDGE_MAX_BUBBLES)
    lines = [ln.strip() for ln in (text or '').splitlines() if ln.strip()]
    if not lines:
        return []
    if len(lines) <= limit:
        return lines
    return lines[:limit - 1] + ['\n'.join(lines[limit - 1:])]


def split_reply(text, limit=None):
    """长回复按句切分（QQ 单条有长度上限，切在句末更像人说话）。"""
    limit = limit or cfg.BRIDGE_MAX_MSG_CHARS
    text = (text or '').strip()
    if len(text) <= limit:
        return [text] if text else []
    out, buf = [], ''
    for piece in re.split(r'(?<=[。！？!?\n])', text):
        if not piece:
            continue
        if len(buf) + len(piece) <= limit:
            buf += piece
        else:
            if buf:
                out.append(buf)
            buf = piece
    if buf:
        out.append(buf)
    return out