# -*- coding: utf-8 -*-
"""角色扮演核心：system 组装 / 生成 / 记忆后处理（无 UI、无协议依赖）。

分层：
- **本模块**：这一轮"说什么"——角色卡加载、system 按序组装、模型调用、记忆后处理
- `rp_gui.py`：Gradio 的流式 yield / 重生成 / 清空确认（UI 专属，不含人格逻辑）
- `sim_group.py`：8 人离线模拟，走与本模块完全相同的路径
- QQ 传输层（自写 bridge / AstrBot 插件）：只负责收发消息与调本模块

system 组装顺序（与重构前的 rp_gui 完全一致，群聊块仅在群场景插入）：
    角色卡 → 用户身份 → 今日日常 → 群里最近 → 群里的人 → 关系状态
    → 记忆卡 → 回忆 → 语料 RAG →（熟悉话题展开提示）→ group note

state 布局：
    state/personas/<id>/                    角色运行态根（GUI 单用户数据也在这里）
    state/personas/<id>/users/<uid>/        QQ 侧每用户：memory_card.json / chat_history.json
    state/members/<uid>/member.json         跨角色共用的客观身份卡
"""
import copy
import datetime
import json
import logging
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from rp_common import clean_output, stream_chat, chat_api, retrieval_query
import hw_profile as hw
import memory_card as mem
import member_card
import corpus_rag as rag
import daily_scene
import card_source
import lore_rag
import persona
import rp_config as cfg
import runtime_config as rc
import self_state

ROOT_DIR = os.path.dirname(HERE)
STATE_ROOT = os.path.join(ROOT_DIR, 'state')
USERS_ROOT = os.path.join(STATE_ROOT, 'users')
DEFAULT_STATE_ROOT = STATE_ROOT
DEFAULT_USERS_ROOT = USERS_ROOT

LOG = logging.getLogger('rp_core')

MEMORY_TURNS = 8  # 私聊短期上下文轮数（user+assistant 各算一条 → 16 条消息）

# 群发言决策每条消息都要问一次"她记不记得这事"，每次都读盘+解析没有意义。
# 短期缓存：文件 mtime 变了（任何写入方保存过）或超过 TTL 就重读。
SNIPPETS_TTL = 5.0
_SNIPPETS_CACHE = {}
_SNIPPETS_LOCK = threading.Lock()

DEFAULT_OPTIONS = {
    'temperature': 0.7,
    'top_p': 0.9,
    'repeat_penalty': 1.15,
    'num_predict': 200,
}


# ---------- 路径 / 用户 ----------

_UID_UNSAFE = re.compile(r'[^0-9A-Za-z_-]')


def safe_uid(uid):
    """uid → 可安全拼进路径的字符串。

    uid 来自 onebot 事件的 sender_id，正常是整数；但任何外部输入都不该直接拼路径
    （'../../x' 会逃出 users/ 目录）。非白名单字符一律换成下划线。
    """
    s = str(uid if uid is not None else '').strip()
    return (_UID_UNSAFE.sub('_', s)[:64] or 'unknown')


def user_dir(uid):
    """当前角色下 QQ 侧单用户的 state 目录（uid 为空 → 角色根目录）。"""
    pid = persona.current()
    # 测试/工具可能改写 core 的根目录；显式重定向优先，避免绕开替身路径。
    if USERS_ROOT != DEFAULT_USERS_ROOT:
        return os.path.join(USERS_ROOT, persona.safe_uid(uid)) if uid else STATE_ROOT
    return persona.user_dir(uid, pid)


def state_dir_for(uid):
    return user_dir(uid)


def _scope(uid):
    """记忆 state 作用域：用户目录，或角色根目录（单用户/GUI）。

    调用方已显式进入某个作用域时（GUI/测试的 state_scope），空 uid 不能把它
    顶掉，否则 build_system 会去读真实角色目录。
    """
    if uid:
        return mem.state_scope(user_dir(uid))
    if mem.state_dir() != mem.DEFAULT_STATE_DIR:
        return mem.state_scope(None)
    return mem.state_scope(persona.state_root(persona.current()))


def history_path(uid):
    return os.path.join(user_dir(uid), 'chat_history.json')


# ---------- 模型 ----------

def parse_model_key(key):
    """模型键 → (kind, model_id, think, profile)。

    面板写入的复合键自带后端与档位，不再依赖全局 api_profile：
      local|<ollama tag>                  本地 Ollama
      localapi|<model id>                 自建 OpenAI 兼容服务
      qwen|<档位名>|<model id>            千问 API
    旧注册表 key（qwen-max/qwen3.5/...）继续可用；没带档位的千问键回落全局
    api_profile（profile=None，调用层按它解析）。
    """
    key = str(key or '').strip()
    if '|' in key:
        parts = key.split('|')
        head = parts[0].strip().lower()
        if head == 'local' and len(parts) >= 2:
            return 'local', '|'.join(parts[1:]).strip(), None, None
        if head == 'localapi' and len(parts) >= 2:
            return 'localapi', '|'.join(parts[1:]).strip(), None, None
        if head == 'qwen' and len(parts) >= 3:
            profile = parts[1].strip().lower() or None
            return 'api', '|'.join(parts[2:]).strip(), None, profile
    for m in cfg.MODEL_REGISTRY:
        if m['key'] == key:
            return m['kind'], m['model'], m.get('think'), None
    for m in cfg.MODEL_REGISTRY:
        if m['key'] == cfg.MODEL_DEFAULT:
            return m['kind'], m['model'], m.get('think'), None
    return 'local', cfg.MODEL_REGISTRY[0]['model'], None, None


def compose_model_key(kind, model, profile=None):
    """(kind, model_id, profile) → 面板存储用的复合键。"""
    model = str(model or '').strip()
    if kind == 'local':
        return 'local|' + model
    if kind == 'localapi':
        return 'localapi|' + model
    return 'qwen|%s|%s' % ((profile or cfg.QWEN_PROFILE), model)


def resolve_model(key):
    """模型键 → (kind, model_id, think)。未知 key 回退注册表默认项。"""
    kind, model_id, think, _profile = parse_model_key(key)
    return kind, model_id, think


def model_profile(key):
    """该模型键使用的千问档位；非千问返回 None。"""
    kind, _model_id, _think, profile = parse_model_key(key)
    if kind != 'api':
        return None
    return profile or cfg.QWEN_PROFILE


def _block_thinking(key):
    """对话/判官各自的思考挡位（面板热生效）。"""
    return rc.get(key, cfg.API_THINKING)


def load_card(model_key=None):
    """加载**当前角色**的 system 卡文本（model_key 仅为兼容旧调用）。

    角色由 runtime_config.character 决定，模型只决定由谁来演。
    """
    return _read_card(persona.system_path())


def _read_card(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except OSError as e:
        return cfg.CARD_LOAD_FAIL_FORMAT % (persona.display_name(), e)


def build_options(temp=None, top_p=None, repeat=None, max_tokens=None, expert=False):
    """推理参数（None 用默认值）。

    expert=True（她熟悉的话题）时给更长篇幅——"展开讨论"需要字数空间，
    只靠 prompt 说"多说两句"而 num_predict 卡在 120 是写不出来的。
    """
    if max_tokens:
        np = int(max_tokens)
    elif expert:
        np = cfg.GROUP_EXPERT_NUM_PREDICT
    else:
        np = DEFAULT_OPTIONS['num_predict']
    return {
        'temperature': DEFAULT_OPTIONS['temperature'] if temp is None else temp,
        'top_p': DEFAULT_OPTIONS['top_p'] if top_p is None else top_p,
        'repeat_penalty': DEFAULT_OPTIONS['repeat_penalty'] if repeat is None else repeat,
        'num_predict': np,
        # 显式上下文长度：否则 Ollama 用默认 2K~4K，会从 system 开头静默截断
        'num_ctx': hw.NUM_CTX,
    }


def build_identity(identity_preset, identity_desc, group=False):
    """用户身份块（区分原作角色/自定义）。原作角色线角色卡已定义称呼，无需附加。

    group=True 用群聊版措辞（"现在跟你说话的这个人"）——群里说"对方"会指代不明，
    模型可能把整段画像套到所有群友身上。
    """
    if identity_preset == cfg.IDENTITY_PRESET_ORIGINAL:
        return ''
    desc = (identity_desc or '').strip().rstrip('。')   # 避免和格式里的句号叠成「。。」
    block = cfg.IDENTITY_HEADER_GROUP if group else cfg.IDENTITY_HEADER
    if desc:
        block += cfg.IDENTITY_DESC_FORMAT % desc
    block += cfg.IDENTITY_NO_ORIGINAL_NAMES
    return block


def identity_for(uid, group=False):
    """按 QQ 号取人物卡（未登记返回空串，即按普通群友对待）。

    配置在 rp_config.QQ_IDENTITY_USERS；只对**当前发言人**注入。
    """
    spec = (getattr(cfg, 'QQ_IDENTITY_USERS', None) or {}).get(str(uid or ''))
    if not spec:
        return ''
    return build_identity(spec.get('preset'), spec.get('desc'), group=group)


# ---------- 群成员 ----------

def note_speaker(uid, nickname='', group_id=''):
    """收到某人消息时更新其成员卡（昵称/最近互动/计数）。失败静默。"""
    try:
        return member_card.touch(uid, nickname=nickname, group_id=group_id)
    except Exception:
        return None


def merge_speakers(speakers, mentions=None, limit=None):
    """合并「被 @ 提到的人」与「最近发言者」，去重并裁剪到预算。

    @ 提到的人必须优先注入——否则模型看到"@群友C"却不知道群友C是谁，
    只能从上下文硬猜（8 人场景里这是指代错乱的主要来源）。
    """
    limit = limit or cfg.MEMBER_BRIEF_LIMIT
    out = []
    seen = set()
    for sp in list(mentions or []) + list(speakers or []):
        uid = str((sp or {}).get('uid') or '').strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        out.append({'uid': uid, 'nickname': sp.get('nickname') or ''})
        if len(out) >= limit:
            break
    return out


def mentions_from_text(text, candidates):
    """从一句话里找出被提到的成员（@ 之外的自然称呼也算）。

    群聊里"你怎么老搭理群友A不搭理我"这种**不带 @ 的称呼**很常见；
    只认 @ 会让模型看到名字却不知道是谁 —— 这是张冠李戴的另一个来源。
    candidates: [{'uid','nickname'}]（调用方提供本群已知成员，避免全库扫描）

    单字昵称（"阿""白"）不做子串匹配——否则"阿"会命中几乎每条消息，
    成员块立刻被挤满。这类昵称只在写成 @阿 时才认。

    关键：必须同时识别 `@QQ号` 形式的 @（群里常直接 @ 数字号）。
    qq_bridge 把「@别人」转成 `@昵称` 或 `@QQ号`（CQ 码没带 name 时就是纯数字）。
    只匹配 nickname 会漏掉 `@10000002` 这种 → 被 @ 的人不进成员块 →
    模型不知道"这个人是谁"，只能从上下文里最近的称呼硬猜（把「博士」安错人）。
    """
    text = text or ''
    out = []
    seen = set()
    for c in (candidates or []):
        uid = str((c or {}).get('uid') or '').strip()
        nick = str((c or {}).get('nickname') or '').strip()
        if not uid or not nick or uid in seen:
            continue
        hit = ('@' + nick) in text or ('＠' + nick) in text
        # @QQ号 形式：群里直接 @ 数字号时，文本里是「@<uid>」而非「@昵称」。
        # 这是"张冠李戴"的最大来源——漏了它，被 @ 的人就失去锚点。
        if not hit and uid:
            hit = ('@' + uid) in text or ('＠' + uid) in text
        if not hit and len(nick) >= 2:
            hit = nick in text
        # identity（用户登记的真实名字/身份，如「用户」）：群里有人问「用户是谁」
        # 时，消息里只有身份词、没有昵称/QQ号。必须让身份词也能命中，否则
        # 模型不知道「用户」对应哪个人（2026-10-22 实测踩到）。
        if not hit:
            try:
                ident = str((member_card.load(uid) or {}).get('identity') or '').strip()
            except Exception:
                ident = ''
            if ident and len(ident) >= 2 and ident in text:
                hit = True
        if hit:
            seen.add(uid)
            out.append({'uid': uid, 'nickname': nick})
    return out


def build_member_block(speakers, limit=None, redact=False, others=None,
                       roster_limit=None):
    """成员摘要块：本轮说话的人逐个展开，其余群友按关系程度分层。

    speakers: [{'uid','nickname'}...]，按**最近发言在前**排序（调用方保证）。
    预算：limit 默认 cfg.MEMBER_BRIEF_LIMIT（4）——8 张卡全注入会挤爆上下文、
    冲淡注意力，实测会让回复变糊。
    others: 本群其他已知成员 [{'uid','nickname','card'?}...]，按**关系由近到远**
            排序（group_roster.known_members 保证）。前 roster_limit 人展开成
            成员行，再往后的只并成一行「另外」名字；已出现的 uid 会跳过。
    roster_limit: 展开人数；None 时取角色清单 group.roster_limit，缺省
            cfg.MEMBER_ROSTER_LIMIT。
    redact=True：只输出昵称，不带 QQ 号、本名、标签与印象。
    """
    limit = limit or cfg.MEMBER_BRIEF_LIMIT
    if roster_limit is None:
        try:
            roster_limit = persona.group_roster_limit()
        except Exception:
            roster_limit = 0
    roster_limit = roster_limit or cfg.MEMBER_ROSTER_LIMIT
    seen = set()
    lines = []

    def _line(uid, fallback_nick, card=None):
        """一个成员的完整成员行（称呼/身份/标签按同一套规则注入）。"""
        if redact:
            nick = str(fallback_nick or '').strip()[:20]
            return cfg.MEMBER_LINE_REDACTED_FORMAT.format(nickname=nick) if nick else ''
        m = card if isinstance(card, dict) else member_card.load(uid)
        nick = str(m.get('nickname') or '').strip()
        # 没卡的人 load 出来的昵称就是 uid，必须退回调用方给的名字
        if (not nick or nick == uid) and str(fallback_nick or '').strip():
            nick = str(fallback_nick).strip()[:20]
        if not nick:
            nick = cfg.MEMBER_UNKNOWN_NICK
        # 气泡模型分层注入：
        #   - identity：**无条件注入**。它是用户在 GUI 里手动登记的真实身份/名字
        #     （如「用户」），不是模型抽的软信息。若不注入，模型在群里只认识网名，
        #     用户问「用户是谁」就答不上来（实测：用户 promoted=false 时身份被吞掉）。
        #   - style / relation_note：个人层软画像，晋升（promoted）后才注入，避免
        #     对每个路人展开长篇画像挤爆上下文。
        # call_name（她给他起的称呼）必须保留——它锚定「叫什么」，一旦缺失
        # 模型会临时编一个，编出来的容易安到下一个人头上（实测踩过）。
        parts = [p for p in (m.get('identity'),) if p]
        if m.get('promoted'):
            parts += [p for p in (m.get('style'), m.get('relation_note')) if p]
        # identity 是「这个人登记的真实名字/身份」（如「用户」），不是网名也不是外号。
        # 它必须同时承担**称呼**职能：只写「本名/身份：用户」时，模型会把它当背景资料，
        # 照旧按网名喊人（2026-10-28 实测：一直喊「示例昵称」，那是网名派生的外号）。
        if m.get('identity'):
            desc = ('本名：%s——称呼他就用这个名字，不要用这行开头的网名'
                    % m['identity'])
            if m.get('promoted'):
                extra = '；'.join(p for p in (m.get('style'), m.get('relation_note')) if p)
                if extra:
                    desc += '；%s' % extra
        else:
            desc = '；'.join(parts)
        # 关于他的标签（群里起哄/评价里透露的偏好）——**不要求晋升**也注入。
        # 2026-10-28：relation_note 要 promoted 才注入，而晋升要有 importance>=2 的事件，
        # 于是"这人喜欢猫"这类标签即使记下来也永远看不见。briefs 走这条旁路。
        # 用「听说：」领起，是为了让模型知道这不是他本人讲的，别当成他自我介绍。
        bl = member_card.brief_line(m)
        if bl:
            desc = ('%s；听说：%s' % (desc, bl)) if desc else '听说：%s' % bl
        old = m.get('alias')
        if old and old != nick:
            nick = '%s（曾用名 %s）' % (nick, old)
        # 称呼挂在昵称上（tag），不塞 desc——否则会变成「群友A：群友A（你叫他…）」
        tag = member_card.call_tag(m)
        if desc:
            return cfg.MEMBER_LINE_FORMAT.format(
                nickname=nick, uid=uid, tag=tag, desc=desc)
        return cfg.MEMBER_LINE_PLAIN_FORMAT.format(
            nickname=nick, uid=uid, tag=tag)

    for sp in (speakers or []):
        uid = str(sp.get('uid') or '').strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        line = _line(uid, sp.get('nickname'))
        if not line:
            continue
        lines.append(line)
        if len(lines) >= limit:
            break

    # 其余群友：关系近的前 roster_limit 人各占一行，再往后只留名字。
    # 「另外」只留给人脉尾部，不再把所有没发言的人压成一份花名册。
    tail = []
    detailed = 0
    for sp in (others or []):
        uid = str((sp or {}).get('uid') or '').strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        card = sp.get('card') if isinstance(sp, dict) else None
        if detailed < roster_limit:
            line = _line(uid, sp.get('nickname'), card)
            if not line:
                continue
            lines.append(line)
            detailed += 1
            continue
        if redact:
            name = str(sp.get('nickname') or '').strip()[:20]
            item = name
        else:
            m = card if isinstance(card, dict) else member_card.load(uid)
            name = str(m.get('nickname') or '').strip()
            if not name or name == uid:
                name = str(sp.get('nickname') or '').strip()
            item = '%s（%s）' % (name[:20], uid) if name else ''
        if item:
            tail.append(item)
        if len(tail) >= cfg.MEMBER_OTHERS_LIMIT:
            break
    if tail:
        lines.append(cfg.MEMBER_OTHERS_LINE.format(names='、'.join(tail)))
    if not lines:
        return ''
    return (cfg.MEMBER_HEADER_REDACTED if redact else cfg.MEMBER_HEADER) + '\n' + '\n'.join(lines)


def clip_text(text, limit=None):
    """单条消息进上下文前的截断（长粘贴会顶爆 24 轮上下文预算）。"""
    limit = limit or cfg.GROUP_MSG_MAX_CHARS
    t = (text or '').strip()
    return t if len(t) <= limit else t[:limit] + '…'


def message_time_label(ts):
    """收到的消息的时间标签：[11-02 00:08]。

    时间必须跟着消息走，而不是在 system 里注入一行「今天几号」：
    模型需要判断的是「这句话是哪天说的」，跨天/跨零点时尤其明显。
    ts 无法解析时返回空串，不硬编时间。
    """
    if not ts:
        return ''
    try:
        dt = datetime.datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError):
        return ''
    return '[%s]' % dt.strftime('%m-%d %H:%M')


def format_group_turns(turns, limit=None, private=False, max_chars=None):
    """群聊上下文 → messages。非"自己"的发言统一 role=user，内容带时间与称呼前缀。

    turns: [{'nickname','text','ts','self'(bool)}...] 按时间正序。
    private: 私聊只有一个人，不加「昵称：」前缀——没有归属歧义，前缀只是噪声。
    max_chars: 单条消息截断字数（本群覆盖；None=用全局默认）。
    """
    limit = limit or cfg.GROUP_TURN_LIMIT
    out = []
    for t in (turns or [])[-limit:]:
        text = clip_text(t.get('text'), max_chars)
        if not text:
            continue
        if t.get('self'):
            # 只放她真正说过的话。以前会追加「（对 昵称）」标注收件人，但模型
            # 会把标注当成发言格式照抄进正文；收件人改由 system 的【对话记录】陈述，
            # 不往 assistant 内容里写非台词文本。
            out.append({'role': 'assistant', 'content': text})
            continue
        if private:
            content = text
        else:
            nick = (t.get('nickname') or cfg.MEMBER_UNKNOWN_NICK).strip()
            content = cfg.SPEAKER_PREFIX_FORMAT.format(nickname=nick, text=text)
        stamp = message_time_label(t.get('ts'))
        if stamp:
            content = stamp + ' ' + content
        out.append({'role': 'user', 'content': content})
    return out


# ---------- system 组装 ----------

def build_system(message, *, model_key=None, uid=None, card_text=None, identity=None,
                 speakers=None, mentions=None, group_members=None, group_scene='',
                 group_note=False, expert=None, include_daily=True, include_rag=True,
                 block_log=None, prev_reply='', tool_block='', speaker='', redact=None,
                 bubble_note=False, last_to='', last_to_ts=None, member_limit=None):
    """组装本轮 system。uid 决定读哪份记忆（None=GUI 的 state/）。

    mentions: 本条消息里被 @ 的人 [{'uid','nickname'}...]——优先级高于"最近发言者"。
    group_members: 本群已知成员 [{'uid','nickname'}...]（名册/成员卡）。只把没进
                   成员块的人压成一行名字，防止没在本轮发言的群友被当成陌生人。
    identity: 用户身份块文本。None（默认）= 按 uid 查 rp_config.QQ_IDENTITY_USERS 自动注入；
              传 '' 表示明确不要（如冷场主动开口时没有"当前发言人"）。
    expert: 是否是她熟悉的话题（None=群场景下按 GATE_EXPERT_WORDS 自动判断）。
            为真时追加「可以展开」提示——决策层负责"要不要开口"，这里负责"开口后别只说半句"。
    prev_reply: 她上一句回复。检索 query = 上一句 + 这句（`retrieval_query`）——
                「那次」这类指代单独去查是噪声，带上她上一句才查得到。
    tool_block: 工具层产出的【外部信息】块（router 命中时由 bridge 传入）。
    speaker: 本轮说话人昵称（群聊传 msg.nick）。非空时在成员块前写一行「本轮说话的人」，
             防止模型把上一轮聊过的人名沿用到他头上。
    last_to / last_to_ts: 她上一句的收件人与时间。只陈述事实（上一条对谁说的），
             不塞进 assistant 轮，避免模型照抄成发言格式。
    block_log: 传入 list 时按顺序记录各注入块名与字符数（调参/断言用）。
    redact: 是否对用户画像/记忆脱敏（None=按 model_key 的后端类型自动判断）。
    """
    if expert is None:
        expert = bool(group_note) and is_expert_topic(message)
    if redact is None:
        redact = cfg.should_redact(resolve_model(model_key)[0])
    if redact:
        # 显式传 '' 仍表示"不要身份块"；其余一律换成不含任何画像的通用块。
        if identity != '':
            identity = cfg.IDENTITY_REDACTED_GROUP if group_note else cfg.IDENTITY_REDACTED
    elif identity is None:
        identity = identity_for(uid, group=bool(group_note))
    with _scope(uid):
        return _build_system_inner(
            message, model_key=model_key, card_text=card_text, identity=identity,
            speakers=merge_speakers(speakers, mentions),
            group_members=group_members, group_scene=group_scene,
            group_note=group_note, expert=expert, include_daily=include_daily,
            include_rag=include_rag, block_log=block_log, prev_reply=prev_reply,
            tool_block=tool_block, speaker=speaker, uid=uid, redact=redact,
            bubble_note=bubble_note, last_to=last_to, last_to_ts=last_to_ts,
            member_limit=member_limit)


def is_expert_topic(text):
    """这句是否落在她真正熟悉的领域（词表来自当前角色清单）。"""
    text = text or ''
    return any(w in text for w in persona.gate().get('expert_words') or ())


def _build_system_inner(message, *, model_key, card_text, identity, speakers,
                        group_members, group_scene, group_note, expert, include_daily,
                        include_rag, block_log, prev_reply='', tool_block='',
                        speaker='', uid=None, redact=False, bubble_note=False,
                        last_to='', last_to_ts=None, member_limit=None):
    def add(name, text):
        if not text:
            return ''
        if block_log is not None:
            block_log.append((name, len(text)))
        return '\n\n' + text

    system = card_text if card_text is not None else load_card(model_key)
    system += add('identity', identity)

    if include_daily:
        # 不再单独注入「今天是几号」：时间由每条收到消息的 [MM-DD HH:MM] 标签携带，
        # system 里只保留环境氛围，避免日期与消息时间各说一套。
        daily = daily_scene.get_cached()
        if daily:
            outfit = daily_scene.get_outfit()
            if outfit:
                daily += '\n' + card_source.load()['outfit']['line'].format(outfit=outfit)
            system += add('daily', cfg.DAILY_HEADER + '\n' + daily + '\n' + cfg.DAILY_BG_NOTE)

    # 群级场景里含成员风格画像；脱敏时整块不注入（氛围可从对话本身看出来）
    if group_scene and not redact:
        system += add('group_scene', cfg.GROUP_SCENE_HEADER + '\n' + group_scene)
    if group_note and speaker:
        note = (cfg.CURRENT_SPEAKER_NOTE_REDACTED if redact
                else cfg.CURRENT_SPEAKER_NOTE)
        system += add('current_speaker',
                      note.format(nickname=speaker, uid='' if redact else (uid or '')))
    system += add('members', build_member_block(speakers, redact=redact,
                                                others=group_members,
                                                limit=member_limit))

    mem_data = mem.load()
    card = mem_data['card']
    # 关系分档是互动状态的总结，不含身份信息，脱敏时保留（否则语气会突然变生）
    system += add('relation', mem.relation_block(card, group=bool(group_note)))
    # 用户事实/印象/反思属于画像；脱敏时只保留角色自己的自述（self_disclosure）。
    card_text_summary = mem.format_card(
        card, superseded=mem.superseded_map(mem_data), include_user=not redact)
    system += add('card', cfg.CARD_HEADER + '\n' + card_text_summary if card_text_summary else '')
    # 她自己的一天：日程 +（私聊才有的）日记与好奇清单。不依赖用户画像，脱敏时照常注入。
    try:
        system += add('self_state', self_state.block(uid, private=not group_note))
    except Exception:
        LOG.exception('今天的安排注入失败（跳过）')
    # 检索 query 带上她上一句：记忆与语料共用同一份（指代词才查得到东西）
    q = retrieval_query(prev_reply, message)
    if not redact:
        system += add('recall', mem.recall_block(mem.retrieve(mem_data, q)))

    if include_rag and persona.capabilities().get('corpus'):
        system += add('rag', rag.format_rag(rag.search(q, cfg.RAG_TOP_N)))

    # 外部检索结果：放在 RAG 之后、行为约束之前，保持"资料→提示→约束"的权重顺序
    system += add('tool', tool_block)

    # 角色卡原作条目：她自己的设定，不是用户数据，脱敏时照常注入
    system += add('lore', lore_rag.block(message, prev_reply=prev_reply, query=q))

    # 熟悉话题的展开提示：放在群聊约束之前（约束仍最后，权重最高）
    if expert:
        system += add('expert_hint', cfg.GROUP_EXPERT_HINT)

    # 换行会被桥接层拆成下一条消息（只对 QQ 这一路生效；GUI 不拆）
    if bubble_note:
        system += add('bubble_note', cfg.BUBBLE_NOTE)
    if group_note:
        # 事实块在约束之前：群规始终是 system 的最后一块（权重最高的位置）。
        note = cfg.GROUP_TRANSCRIPT_NOTE
        if last_to:
            stamp = message_time_label(last_to_ts)
            note += '\n' + cfg.LAST_ADDRESSEE_LINE.format(
                when=('（%s）' % stamp.strip('[]') if stamp else ''),
                nickname=last_to)
        system += add('transcript_note', note)
        system += add('group_note', cfg.GROUP_NOTE)
    return system


def build_messages(system, *, message=None, nickname='', turns=None, limit=None,
                   message_ts=None, dedup_tail=True, private=False, max_chars=None):
    """[system] + 群聊上下文 + 当前这条用户消息。空消息不产生空 user 轮。

    message_ts: 当前这条消息的发送时间；有值时与历史消息一样加 [MM-DD HH:MM] 前缀。
    limit / max_chars: 本群覆盖的历史条数与单条字数上限（None=全局默认）。
    dedup_tail: 若历史末尾已经是同一条，就不再追加。调用方可能忘了把"当前句"从
                turns 里剔除（群聊就是先 add_incoming 再 submit），而同一句话在
                messages 里连着出现两遍，模型会读成"对方连发两次"——2026-10-28
                实测每句被 @ 的话都会换来一句「你连发两次」。
                这里只是兜底，正解在 qq_bridge._turns_for_reply（按 mid 精确剔除，
                因为排队时 turns 末尾可能是更晚的消息）。
    """
    msgs = [{'role': 'system', 'content': system}]
    if turns:
        msgs += format_group_turns(turns, limit, private=private,
                                   max_chars=max_chars)
    text = clip_text(message, max_chars) if message is not None else ''
    if text:
        nick = '' if private else (nickname or '').strip()
        if nick:
            text = cfg.SPEAKER_PREFIX_FORMAT.format(nickname=nick, text=text)
        stamp = message_time_label(message_ts)
        if stamp:
            text = stamp + ' ' + text
        dup_tail = (dedup_tail and msgs and msgs[-1].get('role') == 'user'
                    and (msgs[-1].get('content') or '') == text)
        if not dup_tail:
            msgs.append({'role': 'user', 'content': text})
    return msgs


# ---------- 生成 ----------

def generate_stream(messages, *, model_key=None, options=None, should_stop=None):
    """流式生成，逐块 yield 累积文本（API 模式一次给完整文本）。

    should_stop: 无参回调，返回 True 即停止。本地流式在块之间真停；
    API 非流式请求发出去就停不下来，只能在返回后丢弃（调用方负责记 cancel_late）。
    """
    kind, model_id, think = resolve_model(model_key)
    opts = options or dict(DEFAULT_OPTIONS)
    if kind in ('api', 'localapi'):
        text = chat_api(messages, kind, model_id,
                        temperature=opts.get('temperature', 0.7),
                        max_tokens=opts.get('num_predict', 120),
                        thinking=_block_thinking('gen_thinking'),
                        profile=model_profile(model_key))
        if should_stop is not None and should_stop():
            return
        yield text
        return
    for text in stream_chat(model_id, messages, options=opts, think=think):
        if should_stop is not None and should_stop():
            return
        yield text


def generate(messages, *, model_key=None, options=None, group=False, should_stop=None):
    """非流式生成完整回复（已过 clean_output）。

    group=True（群聊）时额外剥掉（轻笑）这类短动作括注——群聊只要纯台词，
    提示词已约束，这里是硬兜底；私聊保留动作描写。
    should_stop 见 generate_stream：被取消时返回的是**已生成的部分**，调用方自行决定丢弃。
    """
    text = ''
    for text in generate_stream(messages, model_key=model_key, options=options,
                                should_stop=should_stop):
        pass
    names = persona.aliases()
    return clean_output(text, strip_action=group,
                        keep_terms=lore_rag.term_pattern(),
                        drop_prefixes=card_source.header_prefixes(),
                        self_prefixes=tuple(n + sep for n in names for sep in ('：', ':')))


def last_self_text(turns):
    """从群聊 turns 里取她最后一句（检索 query 的上文）。

    turns: [{'nickname','text','self'(bool)}...] 按时间正序。
    """
    for t in reversed(turns or []):
        if t.get('self') and (t.get('text') or '').strip():
            return t['text']
    return ''


def last_self_to(turns):
    """她最后一句是说给谁的：{'to': 昵称, 'ts': 时间}；没记录返回空 dict。

    只看最近一次发言：主动开口没有对象，就不再往前翻——旧收件人写进 system
    比不写更误导。
    """
    for t in reversed(turns or []):
        if t.get('self'):
            to = str(t.get('to') or '').strip()
            return {'to': to, 'ts': t.get('ts') or 0} if to else {}
    return {}


# ---------- 记忆后处理 ----------

_GENERIC_CALLS = ('这位', '那位', '各位', '大家', '诸位', '你们', '我们')

# 身份/敬语后缀：这些是「从对方身份或画像里抠出来的泛称」，不是她给这个人起的
# 专属外号。存进 call_name 会导致**多人共用同一个称呼**——实测出现过两个群友
# 都被叫「博士」（一个昵称里带"博士"，一个纯属张冠李戴），人称彻底错乱。
# 所以这类词一律不存，让她直接用昵称。
_GENERIC_SUFFIXES = ('博士', '老师', '先生', '小姐', '女士', '君', '酱', '同学', '前辈',
                     '学长', '学姐', '老板', '大人', '殿下', '师傅', '师父', '老爷', '夫人')


def _clean_call_name(raw, nickname):
    """规整抽取结果：去掉引号/句读，过滤 NONE、代词、泛称、身份敬称、等于昵称的情况。"""
    if not raw:
        return ''
    t = str(raw).strip().splitlines()[0].strip()
    t = t.strip('「」『』“”"\'`。，,；;：:！!？? 　')
    if not (2 <= len(t) <= 8):
        return ''
    if t.upper() in ('NONE', 'NULL', 'N/A') or t in ('无', '没有', '你', '您', '他', '她', '对方'):
        return ''
    if '没有' in t or '不需要' in t:
        return ''
    # 泛称不是「她给某个人的称呼」，存下来会污染成员卡（实测把「这位先生」存过）
    if t.startswith(_GENERIC_CALLS):
        return ''
    if nickname and t == str(nickname):
        return ''      # 只是叫了昵称，不算起外号，不必存
    # 纯身份/敬语泛称：博士/老师/先生/君/酱…… 这类词多人共用，存下来就串人。
    # 只过滤「光秃秃的身份词」本身；「奇怪先生」这类带形容前缀的真外号保留
    # （真外号的前缀是形容性的，不是身份）。
    if t in _GENERIC_SUFFIXES:
        return ''
    return t


def extract_call_name(reply, nickname, *, model_key=None):
    """从她的回复里抽出「她给这个人起的称呼」。没有就返回 ''。

    为什么要抽：群聊里她给 A 起了「奇怪先生」，A 一滑出成员块，这个称呼就
    失去归属，下一轮她会顺手安给 B（实测踩过）。存进 member.json 后，
    每次注入成员块都带上「你叫他X」，称呼就有了锚。
    成本控制：只有出现 CALL_NAME_HINTS 线索才花一次调用（大多回复都是「你」）。
    """
    reply = (reply or '').strip()
    if not reply or not any(h in reply for h in cfg.CALL_NAME_HINTS):
        return ''
    kind, model_id, think = resolve_model(model_key)
    prompt = cfg.CALL_NAME_PROMPT.format(nickname=nickname or '这个人', reply=reply[:200])
    messages = [{'role': 'user', 'content': prompt}]
    try:
        if kind in ('api', 'localapi'):
            out = chat_api(messages, kind, model_id, temperature=0, max_tokens=16,
                           profile=model_profile(model_key))
        else:
            out = ''
            for out in stream_chat(model_id, messages,
                                   options={'temperature': 0, 'num_predict': 16},
                                   think=False):
                pass
    except Exception:
        return ''
    return _clean_call_name(out, nickname)


# ---------- 气泡模型晋升（群友：群聊气泡 → 人物气泡） ----------

def _promote_precheck(uid):
    """晋升判定的硬性预筛（不调模型）：返回 (是否够格进判官, 诊断 dict)。

    只有「可能够格」的人才值得花一次判官调用。信号全是可观测、按 uid 隔离的：
    - 互动轮数 >= PROMOTE_MIN_INTERACTIONS
    - 记忆卡 importance>=2 的事件数 >= PROMOTE_MIN_IMPORTANT_EVENTS
    - 她给这人起过外号（call_name 非空）→ 强信号，直接够格
    """
    with _scope(uid):
        m = mem.load()
    card = m.get('card') or {}
    n = int(m.get('interaction_count') or 0)
    episodes = [e for e in (m.get('episodes') or []) if not e.get('inactive')]
    important = sum(1 for e in episodes if int(e.get('importance', 1)) >= 2)
    has_facts = bool(card.get('facts') or card.get('impressions'))
    cm = member_card.load(uid)
    has_call = bool((cm.get('call_name') or '').strip())
    eligible = (n >= cfg.PROMOTE_MIN_INTERACTIONS
                and important >= cfg.PROMOTE_MIN_IMPORTANT_EVENTS) or has_call
    diag = {
        'interactions': n,
        'important_events': important,
        'has_call': has_call,
        'has_facts': has_facts,
    }
    return eligible, diag


def promote_judge(uid, *, judge_model=None):
    """用判官模型判断某群友是否已「印象深刻、值得单独建人物画像」。

    输入严格遵循「风格抽取输入铁律」：喂给判官的「该人发言片段」来自
    memory_snippets(uid)（按 uid 隔离的个人记忆），绝不从群聊混流里猜归属。
    返回 (bool 是否晋升, why 理由)。判官失败返回 (False, 错误说明)，不晋升（保守）。
    """
    with _scope(uid):
        m = mem.load()
    card = m.get('card') or {}
    n = int(m.get('interaction_count') or 0)
    episodes = [e for e in (m.get('episodes') or []) if not e.get('inactive')]
    important = sum(1 for e in episodes if int(e.get('importance', 1)) >= 2)
    has_facts = bool(card.get('facts') or card.get('impressions'))
    cm = member_card.load(uid)
    nickname = cm.get('nickname') or str(uid)
    has_call = bool((cm.get('call_name') or '').strip())
    kind, model_id, _ = resolve_model(judge_model)
    snips = memory_snippets(uid, limit=6)
    if cfg.should_redact(kind):
        # 发言片段就是用户记忆原文；脱敏时只保留统计信号，由判官按轮数/重要事件判断。
        snip_text = '（已脱敏：仅依据上面的统计信号判断）'
    else:
        snip_text = '\n'.join('- %s' % s for s in snips) if snips else '（无）'
    prompt = cfg.PROMOTE_PROMPT.format(
        nickname=nickname, n=n, m=important,
        has_call='是' if has_call else '否',
        has_facts='有' if has_facts else '无',
        snippets=snip_text, name=persona.direct_name())
    messages = [{'role': 'user', 'content': prompt}]
    try:
        if kind in ('api', 'localapi'):
            out = chat_api(messages, kind, model_id, temperature=0, max_tokens=64,
                           thinking=_block_thinking('judge_thinking'),
                           profile=model_profile(judge_model))
        else:
            out = ''
            for out in stream_chat(model_id, messages,
                                   options={'temperature': 0, 'num_predict': 64},
                                   think=False):
                pass
    except Exception as e:
        return False, '判官调用失败：%s' % e
    # 解析 JSON：判官可能输出带 ```json 包裹或杂音，宽松提取 promote 字段
    s = (out or '').strip()
    promote = None
    m_json = re.search(r'\{.*\}', s, re.S)
    if m_json:
        try:
            obj = json.loads(m_json.group(0))
            promote = bool(obj.get('promote'))
        except (ValueError, TypeError):
            promote = None
    if promote is None:
        # 兜底：文本里出现 true/false
        if 'true' in s.lower():
            promote = True
        elif 'false' in s.lower():
            promote = False
    if promote is None:
        return False, '判官输出不可解析：%s' % s[:80]
    return promote, (s[:80] or '')


def maybe_promote(uid, *, judge_model=None):
    """晋升判定入口：预筛 → 判官 → 写 member.promoted。

    在 post_turn 归纳后调用。已晋升的幂等跳过；预筛不达标的跳过（不花调用）。
    返回 dict（checked/promoted/why/skipped_reason）。失败不抛异常。
    """
    uid = str(uid) if uid else None
    if not uid:
        return {'checked': False, 'promoted': False, 'why': '无 uid'}
    cm = member_card.load(uid)
    if cm.get('promoted'):
        return {'checked': False, 'promoted': True, 'why': '已晋升（幂等）'}
    try:
        eligible, diag = _promote_precheck(uid)
        if not eligible:
            return {'checked': False, 'promoted': False,
                    'skipped_reason': '预筛不达标 %s' % diag}
        promoted, why = promote_judge(uid, judge_model=judge_model)
        if promoted:
            member_card.promote(uid)
        return {'checked': True, 'promoted': promoted, 'why': why}
    except Exception as e:
        return {'checked': False, 'promoted': False, 'why': '异常：%s' % e}


def _note_about(res, uid, speaker=''):
    """把「A 说 B」的标签记到 B 的成员卡，返回目标 uid（没写则 None）。

    只写 member.briefs，**不写对方的记忆卡**：记忆卡的 facts 是归纳产物、带取代链
    （supersedes/src），跨 uid 写会破坏那条链的一致性——而 briefs 本来就是"别人对他的
    印象"，语义天然允许来源不是本人。

    about_uid 已在 memory_card._match_about 里校验过（必须命中 @ 名单），
    这里再拦一次"记到说话人自己头上"和空标签。
    """
    ep = (res or {}).get('episode') or {}
    target = str(ep.get('about_uid') or '').strip()
    tag = (ep.get('about_tag') or '').strip()
    if not target or not tag or target == str(uid or '').strip():
        return None
    try:
        if member_card.add_brief(target, tag, source=speaker):
            return target
    except Exception:
        return None
    return None


def post_turn(uid, user_msg, reply, *, model_key=None, channel=None, think=None,
              data=None, learn_alias=False, speaker='', recent=None, ats=None,
              is_group=False):
    """本轮回复后的记忆更新：计数 → 话题边界闭合 → 遗忘 → 每5轮重归纳 → 落盘/备份。

    在 uid 的 state 作用域内执行（持锁，多用户串行写盘不交叉）。
    data: 可传入已加载的记忆（调用方做过回滚快照时用）；None 则自行 load。
    learn_alias: 群聊场景开——从回复里抽「她给他起的称呼」并写进成员卡。
    speaker: 本句说话人昵称（群聊传 msg.nick）。空=回落「你」（不是「用户」——
             那是模型泛称，会原样漏进注入文本，见 memory_card._round_speaker）。
    recent: 最近几条别人的群聊 [{'uid','nickname','text'}...]，只作语境
            （见 memory_card._norm_recent，旧 [[昵称, 文本]] 仍兼容）。
    ats:    本句 @ 到的其他人 [{'uid','nickname'}...]——归属依据（见 _norm_ats）。
    is_group: 本轮来自群聊。私聊与群聊的话题缓冲分开结算，不并进同一条事件。
    返回 dict（action/rounds/episode/inducted/values/delta/interaction_count）。
    """
    kind, model_id, think_reg = resolve_model(model_key)
    channel = channel or kind
    if think is None:
        think = think_reg
    with _scope(uid):
        m = data if data is not None else mem.load()
        mem.bump_interaction(m)
        # 用今日日常给事件场景定锚：不给锚点时，边界模型会从记忆卡里搬旧地点，
        # 把当天的新场景也记成上一次的地点（2026-10-29 咖啡店残留事故）。
        res = mem.buffer_round(m, user_msg, reply, model=model_id, think=think,
                               channel=channel, speaker=speaker, recent=recent, ats=ats,
                               scene_hint=daily_scene.get_cached(), is_group=is_group)
        mem.forget(m)
        before = {k: m['card'][k] for k in ('affection', 'trust', 'familiarity')}
        before_rel = m['card'].get('relationship')
        inducted = mem.maybe_induct(m, model=model_id, think=think, channel=channel,
                                    force=bool(res.get('episode') or res.get('flushed')))
        after = {k: m['card'][k] for k in ('affection', 'trust', 'familiarity')}
        after_rel = m['card'].get('relationship')
        try:
            rank_up = (mem.RELATIONSHIPS.index(after_rel)
                       > mem.RELATIONSHIPS.index(before_rel))
        except ValueError:
            rank_up = False
        mem.save(m)
        invalidate_snippets(uid)
        ep = res.get('episode')
        flushed = res.get('flushed')
        info = {
            'uid': str(uid) if uid else None,
            'round': m['interaction_count'],
            'action': res.get('action'),
            'span_rounds': res.get('rounds'),
            'buffer_len': len(m['pending']),
            'llm_failed': res.get('llm_failed', False),
            'episode': (ep.get('summary') if ep else None),
            'importance': (ep.get('importance') if ep else None),
            'flushed': (flushed.get('summary') if flushed else None),
            'span_head': res.get('span_head'),
            'values': after,
            'relationship_before': before_rel,
            'relationship_after': after_rel,
            'rank_up': rank_up,
            'inducted': inducted,
            'delta': ({k: after[k] - before[k] for k in before} if inducted else None),
        }
        mem.debug_log(info)
        try:
            mem.auto_backup(m)
        except Exception:
            pass
        # 称呼学习：放在最后，失败不影响记忆；已有称呼就不再覆盖（免得来回变）
        if learn_alias and uid and reply:
            try:
                card = member_card.load(uid)
                # 登记了本名的人不学外号：抽出来的多半是网名派生（「示例昵称」），
                # 存下来只会持续干扰称呼（2026-10-28）。
                if not (card.get('call_name') or '').strip() \
                        and not (card.get('identity') or '').strip():
                    call = extract_call_name(reply, card.get('nickname') or '',
                                             model_key=model_key)
                    if call:
                        # 这个名字已经名花有主 → 拒绝。
                        # 实测 2026-10-28：她一边喊「原作角色君」一边喊「群友」，
                        # 抽取器把「群友」记成给原作角色的称呼，此后一直叫他群友，
                        # 两个人共用同一个名字，人称彻底串了。
                        owner = member_card.name_owner(call, exclude_uid=uid)
                        if owner:
                            info['call_name_rejected'] = {'name': call, 'owner': owner}
                        else:
                            member_card.update(uid, call_name=call)
                            info['call_name'] = call
            except Exception:
                pass
        # 归属写回：这一段讲的是某个被 @ 的群友（不是本轮说话人）→ 标签记到他头上。
        # 放在记忆存盘之后：万一这里出问题，说话人自己的记忆已经落好了。
        about_uid = _note_about(res, uid, speaker)
        if about_uid:
            info['about_uid'] = about_uid
        return info


def clear_user(uid):
    """清空某用户记忆：先归档到 backups/（不删除），再开新记忆 + 空对话记录。"""
    with _scope(uid):
        archived = None
        try:
            archived = mem.backup()
        except Exception:
            pass
        reset_history(uid)
        mem.save(mem._empty())
        invalidate_snippets(uid)
        mem.debug_log({'event': 'memory_reset', 'uid': str(uid) if uid else None})
        return archived


# ---------- 对话记录（per-user） ----------

def load_history(uid):
    try:
        with open(history_path(uid), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def append_history(uid, user_text, assistant_text):
    """原子追加一轮原始对话。"""
    try:
        hist = load_history(uid)
        hist.append({'role': 'user', 'content': user_text})
        hist.append({'role': 'assistant', 'content': assistant_text})
        p = history_path(uid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except (OSError, ValueError):
        pass


def reset_history(uid):
    try:
        p = history_path(uid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump([], f)
    except OSError:
        pass


def snapshot(uid=None):
    """当前记忆的深拷贝快照（重生成回滚基线）。"""
    with _scope(uid):
        return copy.deepcopy(mem.load())


def invalidate_snippets(uid=None):
    """丢掉某人（uid 为空 = 全部）的记忆摘要缓存；记忆被改写后调用。"""
    key_uid = str(uid or '')
    with _SNIPPETS_LOCK:
        for k in list(_SNIPPETS_CACHE):
            if uid is None or k[0] == key_uid:
                _SNIPPETS_CACHE.pop(k, None)


def memory_snippets(uid, limit=12):
    """她对某人的记忆摘要（供群发言决策判断"这是不是聊过的事"）。

    取记忆卡三类条目 + 最近事件摘要/原话；不含具体数值，也不含他人内容。
    5 秒内且文件未变则直接复用，避免每条群消息都读盘解析。
    """
    key = (str(uid or ''), int(limit), persona.current())
    now = time.time()
    with _scope(uid):
        try:
            mtime = os.path.getmtime(mem._cur_path())
        except OSError:
            mtime = 0.0
        with _SNIPPETS_LOCK:
            hit = _SNIPPETS_CACHE.get(key)
            if hit and now - hit[0] < SNIPPETS_TTL and hit[1] == mtime:
                return list(hit[2])
        m = mem.load()
        out = _build_snippets(m, limit)
        with _SNIPPETS_LOCK:
            _SNIPPETS_CACHE[key] = (now, mtime, list(out))
        return out


def _build_snippets(m, limit):
    card = m.get('card') or {}
    # 已取代的旧事实不进决策上下文——否则判官会拿「住大阪」去判「搬家」相关的话。
    # 用 item_expired 而非简单查集合：归并式条目（"现居东京，曾住大阪"）引用了旧事件
    # 但同时吸收了新事件，不能算过期。
    sup = mem.superseded_map(m)
    out = []
    for key in ('facts', 'impressions', 'self_disclosure'):
        items = card.get(key) or []
        if key == 'facts':      # 同 format_card：取代只作用于事实类，不误杀性格特质
            items = [it for it in items if not mem.item_expired(it, sup)]
        for it in items[:4]:
            t = (it.get('text') or '').strip()
            if t:
                out.append(t)
    dead = mem.superseded_ep_ids(m)
    episodes = [e for e in (m.get('episodes') or [])
                if not e.get('inactive') and e.get('id') not in dead]
    for ep in episodes[-6:]:
        s = (ep.get('summary') or '').strip()
        if s:
            out.append(s)
        u = (ep.get('user_said') or '').strip()
        if u:
            out.append(u[:60])
    return out[:limit]


def mem_label(uid=None):
    """记忆标签（GUI 顶部显示 / 群内 /relation 指令）。"""
    with _scope(uid):
        m = mem.load()
    c = m['card']
    return cfg.MEM_LABEL_FORMAT % (c['affection'], c['trust'], c['familiarity'],
                                   c['relationship'],
                                   len([e for e in m['episodes'] if not e.get('inactive')]),
                                   m['interaction_count'])
