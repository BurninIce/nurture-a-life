# -*- coding: utf-8 -*-
"""三项数值规则（好感/信任/熟悉度）——角色可替换的配置层。

规则唯一事实源：prompts/personas/<id>/stats.json（经 persona.stats_path 解析）
    active                     当前角色 id
    characters.<id>.stats      三项数值各自「是什么、什么会涨、什么会跌、单次上限」
    characters.<id>.aversions  人设明确反感的行为，命中后按固定值扣分
    characters.<id>.relationship  关系档位与数值门槛

代码只负责执行配置：限幅、档位约束、反感结算。换角色卡时改 JSON（或新增角色
条目并切 active）即可，不需要改 memory_card / rp_config 里的任何角色的偏好。
规则文件与角色卡同目录，切角色即切规则。

降级策略：配置缺失或损坏时用内置 _FALLBACK（只有三项上限，无角色反感），
并记一条 warning；绝不让归纳流程因为配置问题直接崩掉。
"""
import copy
import json
import logging
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RULES_PATH = os.path.join(ROOT, 'prompts', 'personas', 'character', 'stats.json')

STAT_KEYS = ('affection', 'trust', 'familiarity')
DEFAULT_LEVELS = ['陌生', '相识', '熟络', '亲密', '倾心']

_LOG = logging.getLogger('stat_rules')
_LOCK = threading.RLock()
_CACHE = {}       # pid -> {'cfg','mtime','pinned','path'}
_PATHS = {}       # pid -> 外部指定路径（测试/迁移用）


# 配置不可用时的最低保障。正常部署不会走到这里——
# 数值以角色目录的 stats.json 为准，这里只保证归纳还能运行。
_FALLBACK = {
    'name': '当前角色',
    'stats': {
        'affection': {
            'label': '好感',
            'meaning': '她愿不愿意靠近你。',
            'up': ['愉快的相处：小步'],
            'down': ['明确排斥、厌烦：1~3'],
            'not_affect': [],
            'gain_caps': {'陌生': 4, '相识': 8, '熟络': 10, '亲密': 12, '倾心': 15},
            'gain_caps_big': {'陌生': 6, '相识': 12, '熟络': 25, '亲密': 30, '倾心': 32},
            'down_cap': 3,
        },
        'trust': {
            'label': '信任',
            'meaning': '她敢不敢把真实的自己交给你。',
            'up': ['说到做到、交付真实信息：小步'],
            'down': ['撒谎、利用她：1~2'],
            'not_affect': [],
            'gain_caps': {'陌生': 4, '相识': 6, '熟络': 8, '亲密': 10, '倾心': 12},
            'gain_caps_big': {'陌生': 4, '相识': 10, '熟络': 20, '亲密': 25, '倾心': 28},
            'down_cap': 2,
        },
        'familiarity': {
            'label': '熟悉度',
            'meaning': '你们一起经历过多少。',
            'up': ['聊得久、一起做过事：小步'],
            'down': [],
            'not_affect': ['熟悉度永不下降'],
            'gain_caps': {'陌生': 3, '相识': 6, '熟络': 8, '亲密': 10, '倾心': 12},
            'gain_caps_big': {'陌生': 3, '相识': 8, '熟络': 20, '亲密': 22, '倾心': 24},
            'down_cap': 0,
        },
    },
    'aversions': [],
    'relationship': {
        'levels': DEFAULT_LEVELS,
        'floor': [
            {'level': '倾心', 'affection': 85, 'trust': 75, 'familiarity': 80},
            {'level': '亲密', 'affection': 70, 'trust': 60, 'familiarity': 65},
            {'level': '熟络', 'affection': 40, 'trust': 30, 'familiarity': 40},
            {'level': '相识', 'affection': 20, 'trust': 20, 'familiarity': 0},
        ],
    },
}
_FALLBACK_CFG = {'version': 0, 'active': '_default',
                 'characters': {'_default': _FALLBACK}}


def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _validate(cfg):
    """校验并归一化配置；不合法直接抛 ValueError（由 load 兜底）。"""
    if not isinstance(cfg, dict):
        raise ValueError('顶层不是对象')
    chars = cfg.get('characters')
    if not isinstance(chars, dict) or not chars:
        raise ValueError('缺少 characters')
    for cid, ch in chars.items():
        if not isinstance(ch, dict):
            raise ValueError('角色 %s 不是对象' % cid)
        stats = ch.get('stats')
        if not isinstance(stats, dict):
            raise ValueError('角色 %s 缺少 stats' % cid)
        for key in STAT_KEYS:
            s = stats.get(key)
            if not isinstance(s, dict):
                raise ValueError('角色 %s 缺少数值 %s' % (cid, key))
            for table in ('gain_caps', 'gain_caps_big'):
                if not isinstance(s.get(table), dict):
                    raise ValueError('%s.%s 缺少 %s' % (cid, key, table))
            if _as_int(s.get('down_cap'), -1) < 0:
                raise ValueError('%s.%s.down_cap 必须是非负整数' % (cid, key))
        aversions = ch.get('aversions') or []
        if not isinstance(aversions, list):
            raise ValueError('角色 %s.aversions 不是数组' % cid)
        seen = set()
        for av in aversions:
            if not isinstance(av, dict) or not str(av.get('id') or '').strip():
                raise ValueError('角色 %s 的反感条目缺少 id' % cid)
            aid = str(av['id']).strip()
            if aid in seen:
                raise ValueError('角色 %s 的反感 id 重复：%s' % (cid, aid))
            seen.add(aid)
            if not isinstance(av.get('deltas'), dict):
                raise ValueError('角色 %s 的反感 %s 缺少 deltas' % (cid, aid))
        rel = ch.get('relationship') or {}
        if rel:
            lv = rel.get('levels')
            if lv is not None and list(lv) != DEFAULT_LEVELS:
                raise ValueError('角色 %s.relationship.levels 必须保持系统档位 %s'
                                 % (cid, '/'.join(DEFAULT_LEVELS)))
            if not isinstance(rel.get('floor'), list):
                raise ValueError('角色 %s.relationship.floor 不是数组' % cid)
            for row in rel.get('floor') or []:
                if not isinstance(row, dict) or row.get('level') not in DEFAULT_LEVELS:
                    raise ValueError('角色 %s.relationship.floor 档位非法：%s'
                                     % (cid, row))
    if cfg.get('active') not in chars:
        raise ValueError('active=%r 不在 characters 中（禁止回退其他角色）'
                         % (cfg.get('active'),))
    return cfg


def _pid(pid=None):
    """当前角色 id；导入期拿不到上下文时返回 None（只用内置降级规则）。"""
    if pid:
        return pid
    try:
        import persona
        return persona.current()
    except Exception:
        return None


def _rule_path(pid):
    if pid in _PATHS:
        return _PATHS[pid]
    try:
        import persona
        return persona.stats_path(pid)
    except Exception:
        return RULES_PATH


def load(path=None, pid=None):
    """读入并激活某个角色的数值配置；失败则回落中性 _FALLBACK。

    pid 为空时取线程内当前角色；文件缺失/损坏只影响该角色，绝不回退到
    其他角色的规则（宁可少涨/不扣，也不串人设）。
    """
    pid = _pid(pid)
    path = path or _rule_path(pid)
    with _LOCK:
        if path != _rule_path(pid):
            _PATHS[pid] = path
        try:
            with open(path, encoding='utf-8') as f:
                raw = json.load(f)
            cfg = _validate(raw)
            ent = {'cfg': cfg, 'mtime': os.path.getmtime(path),
                   'pinned': False, 'path': path}
        except (OSError, ValueError) as e:
            _LOG.warning('角色 %s 的数值规则不可用（%s）：%s，暂用中性兜底',
                         pid, path, e)
            cfg = copy.deepcopy(_FALLBACK_CFG)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = None
            ent = {'cfg': cfg, 'mtime': mtime, 'pinned': False, 'path': path}
        _CACHE[pid] = ent
        return cfg


def _ensure(pid=None):
    """按角色缓存 + 文件 mtime 热加载；set_config 注入的值不读盘。"""
    pid = _pid(pid)
    with _LOCK:
        ent = _CACHE.get(pid)
        path = _rule_path(pid)
        if ent is None:
            return load(path, pid=pid)
        if ent.get('pinned'):
            return ent['cfg']
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        if mtime != ent.get('mtime') or path != ent.get('path'):
            return load(path, pid=pid)
        return ent['cfg']


def config(pid=None):
    return _ensure(pid)


def reload(pid=None):
    pid = _pid(pid)
    return load(_rule_path(pid), pid=pid)


def reset():
    """丢弃全部角色的缓存，下次访问重新读盘。"""
    with _LOCK:
        _CACHE.clear()
        _PATHS.clear()


def set_config(cfg, pid=None):
    """测试/热注入用：给当前角色直接激活一份配置，不再读盘。"""
    pid = _pid(pid)
    with _LOCK:
        data = _validate(copy.deepcopy(cfg))
        _CACHE[pid] = {'cfg': data, 'mtime': None, 'pinned': True,
                       'path': _rule_path(pid)}
        return data


def _character(pid=None):
    cfg = _ensure(pid)
    chars = cfg.get('characters') or {}
    ch = chars.get(cfg.get('active'))
    if not isinstance(ch, dict):
        _LOG.warning('角色 %s 的数值规则缺失，使用中性兜底（不回退其他角色）',
                     cfg.get('active'))
        return _FALLBACK
    return ch


def name():
    return str(_character().get('name') or '')


def levels():
    rel = _character().get('relationship') or {}
    return list(rel.get('levels') or DEFAULT_LEVELS)


def stat(key):
    return (_character().get('stats') or {}).get(key) or {}


def gain_cap(key, relationship, big=False):
    """本次归纳该数值允许的最大涨幅（按归纳前的档位取表）。"""
    s = stat(key)
    table = s.get('gain_caps_big' if big else 'gain_caps') or {}
    # 档位脏数据时先退回「相识」（与旧行为一致），再不行取最低档，宁可少涨
    for lv in (relationship, '相识'):
        if lv in table:
            return max(0, _as_int(table.get(lv), 0))
    for lv in levels():
        if lv in table:
            return max(0, _as_int(table.get(lv), 0))
    return 0


def down_cap(key):
    """本次归纳该数值允许的最大跌幅；familiarity 配置为 0（永不下降）。"""
    return max(0, _as_int(stat(key).get('down_cap'), 0))


def aversions():
    return [a for a in (_character().get('aversions') or []) if isinstance(a, dict)]


def aversion_hits(raw, valid_episode_ids=None):
    """校验模型输出的反感命中，返回 [{'id': str, 'episode_ids': [int]}]。

    - 只认配置里存在的 id；未知 id 丢弃
    - 同一个 id 的多条记录合并 episode_ids（去重保序）
    - 给了 valid_episode_ids 时，引用了不存在事件的编号会被丢掉
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    valid = {str(a.get('id')) for a in aversions()}
    valid_eps = None
    if valid_episode_ids is not None:
        valid_eps = {_as_int(x, -1) for x in valid_episode_ids}
    merged = {}
    order = []
    for item in raw:
        if isinstance(item, dict):
            aid = str(item.get('id') or '').strip()
            eps_raw = item.get('episode_ids') or item.get('source_episode_ids') or []
        else:
            aid, eps_raw = str(item or '').strip(), []
        if not isinstance(eps_raw, (list, tuple)):
            eps_raw = [eps_raw]
        if aid not in valid:
            continue
        if aid not in merged:
            merged[aid] = {'id': aid, 'episode_ids': []}
            order.append(aid)
        eps = merged[aid]['episode_ids']
        for x in eps_raw:
            n = _as_int(x, -1)
            if n < 0 or (valid_eps is not None and n not in valid_eps):
                continue
            if n not in eps:
                eps.append(n)
    return [merged[aid] for aid in order]


def aversion_deltas(hits):
    """把命中的反感折算成三项扣分。

    接受 aversion_hits() 的输出或纯 id 列表。同一窗口里命中多条时**不叠加**：
    每项取最负的那条。一次归纳只结算一次反感，否则「取笑伤痛 + 怜悯」
    会在同一个窗口里把好感直接扣穿。
    """
    by_id = {str(a.get('id')): a for a in aversions()}
    out = {}
    for item in hits or []:
        aid = item.get('id') if isinstance(item, dict) else item
        av = by_id.get(str(aid))
        if not av:
            continue
        for key, val in (av.get('deltas') or {}).items():
            if key not in STAT_KEYS:
                continue
            val = _as_int(val, 0)
            if val < 0:
                out[key] = min(out.get(key, 0), val)
    return out


def relation_floor():
    """关系档位门槛：[(档位, 好感, 信任, 熟悉), ...]，按配置顺序（高→低）。"""
    rel = _character().get('relationship') or {}
    floor = rel.get('floor') or _FALLBACK['relationship']['floor']
    out = []
    for row in floor:
        if not isinstance(row, dict):
            continue
        lv = str(row.get('level') or '').strip()
        if not lv:
            continue
        out.append((lv, _as_int(row.get('affection'), 0),
                    _as_int(row.get('trust'), 0),
                    _as_int(row.get('familiarity'), 0)))
    return out


def _cap_text(table):
    return ' / '.join('%s %d' % (lv, _as_int((table or {}).get(lv), 0))
                      for lv in levels() if lv in (table or {}))


def _aversion_line(av):
    deltas = av.get('deltas') or {}
    bits = []
    for key in STAT_KEYS:
        v = _as_int(deltas.get(key), 0)
        if v:
            bits.append('%s %+d' % (stat(key).get('label') or key, v))
    return '%s｜%s：%s → %s' % (av.get('id'), av.get('label') or '',
                               av.get('trigger') or '', '、'.join(bits) or '不扣分')


def prompt_block():
    """生成归纳 prompt 里的数值规则段（由配置驱动，换角色只改 JSON）。"""
    ch = _character()
    lines = ['三项数值各管一件事，按事件分别判断（不要三项一起动）。',
             '只结算标了【本次新事件】的事件；【历史·已结算】不要重复加减。',
             '判断基准是上方【关系现状】的当前数值：只在此基础上加减，不要另起炉灶给新数字。',
             '没有真正推进关系的事件，三项照抄当前值，不要为了"有变化"而调整。']
    for key in STAT_KEYS:
        s = stat(key)
        label = s.get('label') or key
        lines.append('')
        lines.append('【%s %s】%s' % (label, key, s.get('meaning') or ''))
        for title, field in (('会涨', 'up'), ('会跌', 'down')):
            items = [str(x) for x in (s.get(field) or []) if str(x).strip()]
            if items:
                lines.append('  %s：%s。' % (title, '；'.join(
                    '%d) %s' % (i + 1, x) for i, x in enumerate(items))))
        items = [str(x) for x in (s.get('not_affect') or []) if str(x).strip()]
        if items:
            lines.append('  不算：%s。' % '；'.join(items))
        up = _cap_text(s.get('gain_caps'))
        up_big = _cap_text(s.get('gain_caps_big'))
        down = down_cap(key)
        if key == 'familiarity' or down == 0:
            lines.append('  单次上限：涨（%s；重大事件 %s）；**永不下降**。' % (up, up_big))
        else:
            lines.append('  单次上限：涨（%s；重大事件 %s）；跌最多 %d。' % (up, up_big, down))
    avs = aversions()
    if avs:
        lines.append('')
        lines.append('**%s明确反感的行为（优先级最高，按配置固定扣分）**：'
                     % (ch.get('name') or ''))
        for av in avs:
            lines.append('- ' + _aversion_line(av))
        lines.append('命中任意条目时，在 aversion_hits 里输出 {"id": 反感id, "episode_ids": [发生该行为的事件编号]}；'
                     'episode_ids 必须来自上方事件列表，同一条行为只报一次。'
                     '系统会按上表自动扣分，**数值字段里不要再重复扣**；'
                     '之前归纳已结算过的旧事件不要再报。没有命中给空数组，拿不准就不算。')
    lines.append('')
    lines.append('关系档位只能被三项数值抬上去，不能越过数值；'
                 '单次反感不降 relationship 档位，只有数值已撑不住原档位时才退，且一次最多退一档。')
    lines.append('relationship 必须和数值自洽：数值只够「相识」时，不许因为对方表白了就判「熟络」；'
                 '反过来，数值已经够高时也不必刻意压着不给升档。')
    return '\n'.join(lines)
