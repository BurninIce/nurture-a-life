# -*- coding: utf-8 -*-
"""群成员卡（真人群友的客观信息，半静态）。

设计边界（2026-10-20 定）：
- 8 人是**真人**，所以本卡只记**客观可知**的信息：昵称/别称/自述身份/说话风格/关系备注/标签。
- **不写任何人格设定**——角色对每个人的差异化反应来自她自己的记忆（memory_card 事件库），
  而不是给真人套一份 RP 设定。这既是产品主张（故事为锚），也是隐私边界。
- 存储：客观信息存 state/members/<uid>/member.json（跨角色共用）；
  角色专属的称呼/关系备注存 state/personas/<id>/users/<uid>/member.json。
  Character 已迁入同一布局，不再读整张旧卡。

注入预算：只注入"当前发言人 + 最近几位发言者"的摘要，不整群注入（见 rp_core.build_member_block）。
"""
import datetime
import json
import logging
import os
import re

import logutil
import persona

_LOG = logging.getLogger('member_card')

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_USERS_ROOT = os.path.join(ROOT_DIR, 'state', 'members')
_USERS_ROOT = DEFAULT_USERS_ROOT

_UID_UNSAFE = re.compile(r'[^0-9A-Za-z_-]')

FIELDS = ('uid', 'nickname', 'alias', 'call_name', 'identity', 'gender', 'style',
          'relation_note', 'briefs', 'tags', 'first_seen', 'last_seen', 'msg_count',
          'promoted', 'promoted_at')
# 只有「她怎么看这个人」的部分随角色走；昵称/身份/性别/风格等客观信息共用。
OVERLAY_FIELDS = ('call_name', 'relation_note', 'briefs', 'promoted', 'promoted_at')

MAX_BRIEF_LEN = 90  # 单成员摘要长度上限（容纳「你叫他 X」，仍远低于注入预算）
BRIEF_MAX_ITEMS = 4   # 每人最多记几条「关于他的标签」
BRIEF_TEXT_CHARS = 16  # 单条标签字符上限（「喜欢猫」这种长度）

# 运行态文件结构版本。缺 version 视为 0（旧文件）；迁移只在 load 内存里执行，
# 下次 save 才落盘。只允许补字段/规整结构，不改昵称、称呼、备注等 RP 可见内容。
SCHEMA_VERSION = 1
_MIGRATIONS = {}  # {目标版本: fn(data)}，幂等


def users_root():
    return _USERS_ROOT


def set_users_root(path=None):
    """切换成员卡根目录（离线模拟/测试隔离用）。返回切换后的根目录。"""
    global _USERS_ROOT
    _USERS_ROOT = path or DEFAULT_USERS_ROOT
    return _USERS_ROOT


def _safe_uid(uid):
    s = str(uid if uid is not None else '').strip()
    return _UID_UNSAFE.sub('_', s)[:64] or 'unknown'


def shared_path(uid):
    """跨角色共用的客观身份卡（昵称/身份/风格/标签）。"""
    return os.path.join(_USERS_ROOT, _safe_uid(uid), 'member.json')


def user_dir(uid, pid=None):
    """uid → 角色专属覆盖卡目录（state/personas/<id>/users/<uid>）。"""
    pid = pid or persona.current()
    return persona.user_dir(uid, pid)


def path_for(uid, pid=None):
    return os.path.join(user_dir(uid, pid), 'member.json')


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M')


def _empty(uid, nickname=''):
    return {
        'schema_version': SCHEMA_VERSION,
        'uid': str(uid),
        'nickname': nickname or str(uid),
        'alias': '',        # 曾用名（改昵称时自动记录）
        'call_name': '',    # 她给这个人起的称呼（如「奇怪先生」），用来防止叫到别人头上
        'identity': '',
        # 人物卡性别（male/female/other）；留空表示未登记。
        'gender': '',
        'style': '',
        'relation_note': '',
        # 关于他的一句话标签，如 [{'text': '喜欢猫', 'source': '示例角色', 'at': '10-28'}]。
        # 与 relation_note 的区别：briefs **不要求晋升**就注入——群友的"关键标签"
        # 本来就应该一眼看见，而 relation_note 是给已晋升的熟人用的长画像。
        'briefs': [],
        'tags': [],
        'first_seen': _now(),
        'last_seen': _now(),
        'msg_count': 0,
        'promoted': False,   # 是否已从「群聊气泡」晋升为「人物气泡」（独立画像）
        'promoted_at': '',   # 晋升时间（YYYY-MM-DD HH:MM）
    }


def _read(path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except OSError as e:
        _LOG.warning('成员卡读取失败（按空卡处理）%s：%s', path, e)
        return {}
    except ValueError:
        _LOG.error('成员卡 JSON 损坏（按空卡处理，下次 save 会覆盖该文件）：%s', path)
        return {}
    if not isinstance(data, dict):
        _LOG.warning('成员卡结构非法（按空卡处理）：%s', path)
        return {}
    return data


def _merge(base, data, fields):
    base.update({k: v for k, v in data.items() if k in fields})


def _schema_of(data):
    try:
        return int(data.get('schema_version') or 0)
    except (TypeError, ValueError):
        return 0


def migrate(data):
    """就地升级到 SCHEMA_VERSION（幂等；更高版本原样返回，不降级）。"""
    v = _schema_of(data)
    if v > SCHEMA_VERSION:
        return data
    for target in range(v + 1, SCHEMA_VERSION + 1):
        fn = _MIGRATIONS.get(target)
        if fn is not None:
            fn(data)
        data['schema_version'] = target
    return data


_SHARED_FIELDS = tuple(k for k in FIELDS if k not in OVERLAY_FIELDS)


def load(uid, pid=None):
    """读成员卡；缺失/损坏返回按 uid 初始化的空卡（不写盘）。

    共用身份与角色专属称呼/关系备注合并，互不覆盖。
    """
    pid = pid or persona.current()
    shared = migrate(_read(shared_path(uid)))
    overlay = migrate(_read(path_for(uid, pid)))
    base = _empty(uid)
    _merge(base, shared, _SHARED_FIELDS)
    _merge(base, overlay, OVERLAY_FIELDS)
    base['uid'] = str(uid)
    base['schema_version'] = max(_schema_of(shared), _schema_of(overlay),
                                 SCHEMA_VERSION)
    if not isinstance(base.get('tags'), list):
        base['tags'] = []
    if not isinstance(base.get('briefs'), list):
        base['briefs'] = []
    return base


def _write(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def save(uid, data, pid=None):
    """原子写：共用身份写共享区，称呼/关系备注写角色覆盖卡。"""
    pid = pid or persona.current()
    data = migrate(dict(data))
    shared = migrate(_read(shared_path(uid)))
    shared.update({k: data.get(k) for k in _SHARED_FIELDS if k in data})
    shared['uid'] = str(uid)
    ver = max(_schema_of(shared), _schema_of(data), SCHEMA_VERSION)
    shared['schema_version'] = ver
    _write(shared_path(uid), shared)
    overlay = {'uid': str(uid), 'schema_version': ver}
    overlay.update({k: data.get(k) for k in OVERLAY_FIELDS if k in data})
    return _write(path_for(uid, pid), overlay)


def touch(uid, nickname='', group_id=''):
    """每收到一条该用户的消息调用一次：刷新昵称/最近互动/计数。

    昵称变更时旧昵称进 alias（保留曾用名，便于模型理解"改名的还是同一个人"）。
    """
    m = load(uid)
    nick = (nickname or '').strip()
    if nick and nick != m['nickname']:
        if m['nickname'] and m['nickname'] != str(uid):
            m['alias'] = m['nickname'][:20]
            m['tags'] = (m.get('tags') or [])[:]
        m['nickname'] = nick[:20]
    m['last_seen'] = _now()
    m['msg_count'] = int(m.get('msg_count') or 0) + 1
    if group_id:
        tag = 'G%s' % group_id
        if tag not in m['tags']:
            m['tags'] = (m['tags'] + [tag])[:8]
    try:
        save(uid, m)
    except OSError as e:
        logutil.throttled(_LOG, 'touch_save', logging.WARNING,
                          '成员卡写入失败（uid=%s）：%s', uid, e)
    return m


def add_brief(uid, text, source=''):
    """给某人记一条「关于他的标签」（如「喜欢猫」）。返回是否新增。

    这是 **A 说 B** 的信息：source 记说话人昵称，供日后追来源；注入时刻意不逐条带来源
    （「喜欢猫（示例角色说）」太占预算），统一以「听说：」领起，模型能分辨这不是他本人讲的。
    按文本去重；超限丢最旧（新的更贴现状）。
    不负责作用域/锁——跨 uid 写入由 rp_core 包 scope。
    """
    if not uid:
        return False
    t = (text or '').strip()[:BRIEF_TEXT_CHARS]
    if not t:
        return False
    m = load(uid)
    briefs = [b for b in (m.get('briefs') or []) if isinstance(b, dict)]
    if any((b.get('text') or '') == t for b in briefs):
        return False
    briefs.append({'text': t, 'source': (source or '').strip()[:20],
                   'at': datetime.datetime.now().strftime('%m-%d')})
    m['briefs'] = briefs[-BRIEF_MAX_ITEMS:]
    save(uid, m)
    return True


def brief_line(m, max_chars=MAX_BRIEF_LEN):
    """成员卡 → 「喜欢猫、爱爬山」。没有则空串。"""
    items = [b for b in (m.get('briefs') or [])
             if isinstance(b, dict) and (b.get('text') or '').strip()]
    if not items:
        return ''
    return '、'.join(b['text'].strip() for b in items)[:max_chars]


def update(uid, **kw):
    """人工/脚本补充信息（identity / gender / style / relation_note / alias / call_name / tags）。"""
    m = load(uid)
    for k, v in kw.items():
        if k in ('identity', 'style', 'relation_note', 'alias', 'call_name') and isinstance(v, str):
            m[k] = v.strip()[:60]
        elif k == 'gender' and isinstance(v, str):
            g = v.strip().lower()
            m[k] = g if g in ('male', 'female', 'other') else ''
        elif k == 'tags' and isinstance(v, (list, tuple)):
            m['tags'] = [str(t)[:12] for t in v][:8]
    save(uid, m)
    return m


_NAME_FIELDS = ('nickname', 'alias', 'call_name', 'identity')
_NICK_TAIL = re.compile(r'[（(\[【].*?[）)\]】]|\.\.\.|…')
# 挂在名字后面的敬称/身份后缀：同名再叫一次时常被加上（群友 → 群友君）。
# 按长度从大到小匹配，保证「先生」不会被「生」抢先剥掉。
_CALL_SUFFIXES = ('先生', '小姐', '女士', '博士', '老师', '同学', '前辈', '学长',
                  '学姐', '老板', '大人', '殿下', '师傅', '师父', '老爷', '夫人',
                  '君', '酱')
# 这些词多人可共用，不能因为某人的昵称以它结尾就当成「某人的名字片段」。
_GENERIC_NAMES = frozenset((
    '博士', '老师', '先生', '小姐', '女士', '同学', '前辈', '学长', '学姐',
    '老板', '大人', '殿下', '师傅', '师父', '老爷', '夫人', '君', '酱',
    '这位', '那位', '各位', '大家', '你们', '我们',
))


def _base_nick(nick):
    """昵称去掉括注/省略号尾巴：「肌霸群友（复活特蕾西娅中…）」→「肌霸群友」。"""
    return _NICK_TAIL.sub('', (nick or '')).strip()


def _call_stem(name):
    """去掉称呼后面的敬称后缀：「群友君」→「群友」。剩余不足两字则不剥。"""
    t = (name or '').strip()
    changed = True
    while changed and len(t) > 2:
        changed = False
        for suf in _CALL_SUFFIXES:
            if t.endswith(suf) and len(t) > len(suf) + 1:
                t = t[:-len(suf)].strip()
                changed = True
                break
    return t


def _name_keys(name):
    """一个名字的全部等价形式（原名 / 去括注 / 去敬称后缀）。"""
    t = (name or '').strip()
    if not t:
        return set()
    base = _base_nick(t)
    return {x for x in (t, base, _call_stem(t), _call_stem(base)) if x}


def name_owner(name, exclude_uid=''):
    """这个名字已经是谁的了？返回占用者 uid；没人用则 ''。

    用途：拦住「她给 A 起的称呼落到 B 头上」。实测 2026-10-28：
    角色在同一条回复里既喊「原作角色君」又喊「群友」，抽取器把「群友」
    当成给原作角色的称呼写进 call_name —— 而「群友」本来就是 10000002 的称呼，
    于是注入时两个人共用一个名字，人称彻底串了（她此后一直把原作角色叫成群友）。

    比对四个称呼字段，并同时比对「去括注」「去敬称后缀」两种变体：
    2026-11-01 又踩到一次——原作角色被存下 call_name「群友君」，全等比较
    （「群友」≠「群友君」）没拦住，注入后角色一直把原作角色当群友。

    另加「昵称片段」判定：中文群名片常把描述放在名字前面
    （肌霸群友 / 蓝莓小saki / 示例角色），只取其中一截当称呼很容易串人。
    因此「群友」「saki」「原作角色」这类是别人昵称后缀的片段、以及「肌霸」
    这种前缀片段，都算名花有主。
    不做任意子串匹配：短名字当子串极易误伤（「霜」在「用户」里）。
    """
    target = _name_keys(name)
    if not target:
        return ''
    ex = str(exclude_uid or '').strip()
    pid = persona.current()
    roots = (_USERS_ROOT, os.path.join(persona.state_root(pid), 'users'))
    dirs = {d for root in roots for d in _list_dirs(root)}
    for d in dirs:
        if ex and d == ex:
            continue
        try:
            m = load(d, pid)
        except Exception:
            continue
        if not m:
            continue
        for k in _NAME_FIELDS:
            v = m.get(k)
            v = v.strip() if isinstance(v, str) else ''
            if not v:
                continue
            if _name_keys(v) & target:
                return str(m.get('uid') or d)
            fulls = {v, _base_nick(v)}
            for cand in target:
                if len(cand) < 2 or cand in _GENERIC_NAMES:
                    continue
                if any(len(full) > len(cand)
                       and (full.endswith(cand) or full.startswith(cand))
                       for full in fulls if full):
                    return str(m.get('uid') or d)
    return ''


def promote(uid):
    """标记某群友已晋升为「人物气泡」（独立画像）。
    幂等：已晋升则直接返回，不重复写盘。晋升单向（降级暂不做）。
    """
    m = load(uid)
    if m.get('promoted'):
        return m
    m['promoted'] = True
    m['promoted_at'] = _now()
    save(uid, m)
    return m


def call_tag(member):
    """她给这个人起的称呼，做成昵称后缀；没有或等于昵称则返回 ''。

    单独成一个函数，是为了让注入端能把「叫什么」挂在昵称上，
    而不是塞进 desc —— 塞进去会变成「群友A：群友A（你叫他…）」的重复。

    2026-10-28：登记了 identity（本名）的人**不再加这个标签**。
    「你叫他「示例昵称」」是从网名派生的，权重比「本名：用户」高，
    结果整轮都在喊网名。本名优先，称呼交给 desc 里那句话说死。
    """
    if not member:
        return ''
    if (member.get('identity') or '').strip():
        return ''
    call = (member.get('call_name') or '').strip()
    if not call or call in (member.get('nickname'), member.get('alias')):
        return ''
    # 泛称多人共用，不能当专属称呼注入（2026-10-22：群友和原作角色都被叫「博士」）。
    if call in _GENERIC_NAMES:
        return ''
    # 存量脏数据的最后一道拦截：称呼若已属于别人，宁可不注入，
    # 也不能让两个人共用一个名字（2026-11-01：群友君 → 原作角色）。
    if name_owner(call, exclude_uid=str(member.get('uid') or '')):
        return ''
    return '｜你叫他「%s」' % call


def brief(member, max_len=MAX_BRIEF_LEN):
    """成员摘要一行：昵称（她对他的称呼；身份；风格；关系备注）。空字段自动省略。

    call_name 比 desc 优先保留：它直接决定「他叫什么」，一旦缺失模型就会
    自己临时编一个，编出来的很容易安到下一个人头上（实测踩过）。
    截断从尾部切，所以把 call_name 放在 desc 之前。
    """
    if not member:
        return ''
    nick = member.get('nickname') or member.get('uid') or ''
    alias = member.get('alias')
    if alias and alias != nick:
        nick = '%s（曾用名 %s）' % (nick, alias)
    call = (member.get('call_name') or '').strip()
    # 有本名时同 call_tag：不让网名派生的外号抢在本名前面（2026-10-28）。
    if call and not (member.get('identity') or '').strip() \
            and call not in (member.get('nickname'), alias) \
            and call not in _GENERIC_NAMES \
            and not name_owner(call, exclude_uid=str(member.get('uid') or '')):
        nick = '%s（你叫他「%s」）' % (nick, call)
    parts = [p for p in (member.get('identity'), member.get('style'),
                         member.get('relation_note')) if p]
    desc = '；'.join(parts)
    text = '%s：%s' % (nick, desc) if desc else nick
    return text[:max_len]


def all_members():
    """列出全部成员卡（调试/面板用）。只看真正有卡的目录：空目录不是成员。"""
    pid = persona.current()
    keys = set()
    for root in (_USERS_ROOT, os.path.join(persona.state_root(pid), 'users')):
        keys.update(d for d in _list_dirs(root)
                    if os.path.isfile(os.path.join(root, d, 'member.json')))
    return [load(uid, pid) for uid in sorted(keys)]


def _list_dirs(root):
    try:
        return os.listdir(root)
    except OSError:
        return []
