# -*- coding: utf-8 -*-
"""按群单独覆盖群聊开关与参数（发言门除外）。

背景（用户需求 2026-11-11）：群聊开关和主动/上下文参数原先都是全局一份，
新群加入后只能跟着全局走，想给某个群更少的历史条数就只能改全局。
本模块把「某个群单独改了哪些项」存进 `state/groups/<gid>/settings.json`：

    {
      "enabled": true,
      "group_cooldown": 90,
      "group_turn_limit": 16
    }

只有**显式改过**的键才落盘；没写的键继续跟随角色卡调参、运行时配置与 rp_config。
读取顺序（从具体到一般）：

    本群覆盖 > 角色卡 gate 调参 > runtime_config > rp_config 常量

发言门的打分权重与冷却（consider_at、w_*、group_cooldown 等）**不进本模块**：
用户 2026-11-11 明确它们归角色卡、所有群共用，面板在配置页统一编辑。

存储只做存取与校验，读的时候由调用方按上述顺序回退；这样面板能同时展示
「本群值」和「继承值」，不必把全局值复制进每个群。
"""
import json
import os
import threading

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROOT = os.path.join(ROOT_DIR, 'state', 'groups')
_ROOT = DEFAULT_ROOT

_LOCK = threading.RLock()
_MISSING = object()

# key     → 参数名（与 group_gate / runtime_config 中的键一致，避免来回映射）
# type    → bool / int / float
# group   → 面板分组
# cfg     → 全局回退的 rp_config 常量（没有则用 default）
GROUP_SCHEMA = [
    # —— 开关 ——
    {'key': 'enabled', 'type': 'bool', 'group': '开关', 'default': True,
     'label': '本群接管',
     'desc': '关闭后本群不接话、不记活跃、不主动；管理员仍可用 /group on 叫回来'},
    {'key': 'proactive_enabled', 'type': 'bool', 'group': '开关', 'default': True,
     'label': '主动开话题', 'desc': '群冷场时她主动起话头'},
    {'key': 'tools_enabled', 'type': 'bool', 'group': '开关', 'default': False,
     'cfg': 'TOOLS_ENABLED',
     'label': '工具总开关',
     'desc': '允许她在这个群按需调用外部检索工具（默认关）'},
    {'key': 'tool_router_group_at', 'type': 'bool', 'group': '开关', 'default': False,
     'cfg': 'TOOL_ROUTER_GROUP_AT',
     'label': '群聊旁听也检索',
     'desc': '关闭时这个群里只有 @ 她才检索（推荐）'},

    # 发言门（consider_at / w_* / group_cooldown 等 15 项）不在这里：
    # 它们归角色卡 gate 调参，所有群共用，面板在配置页统一编辑。

    # —— 主动开话题 ——
    {'key': 'gate_idle_sec', 'type': 'int', 'group': '主动开话题', 'min': 60, 'max': 7200,
     'step': 60, 'unit': '秒', 'cfg': 'GATE_IDLE_SEC',
     'label': '静默阈值', 'desc': '群内静默超过多少秒才允许主动'},
    {'key': 'gate_proactive_min_gap', 'type': 'int', 'group': '主动开话题',
     'min': 60, 'max': 21600, 'step': 60, 'unit': '秒', 'cfg': 'GATE_PROACTIVE_MIN_GAP',
     'label': '主动最小间隔', 'desc': '两次主动之间最小间隔'},
    {'key': 'gate_proactive_daily_max', 'type': 'int', 'group': '主动开话题',
     'min': 0, 'max': 50, 'step': 1, 'unit': '次/天', 'cfg': 'GATE_PROACTIVE_DAILY_MAX',
     'label': '每日主动上限', 'desc': '本群每天主动次数上限'},

    # —— 上下文预算 ——
    {'key': 'group_turn_limit', 'type': 'int', 'group': '上下文预算', 'min': 4, 'max': 60,
     'step': 1, 'unit': '条', 'cfg': 'GROUP_TURN_LIMIT',
     'label': '群聊历史条数', 'desc': '传给模型的群聊历史条数'},
    {'key': 'group_msg_max_chars', 'type': 'int', 'group': '上下文预算', 'min': 50,
     'max': 1000, 'step': 10, 'unit': '字', 'cfg': 'GROUP_MSG_MAX_CHARS',
     'label': '单条消息上限', 'desc': '单条群消息进上下文的最大字数'},
    {'key': 'member_brief_limit', 'type': 'int', 'group': '上下文预算', 'min': 1, 'max': 8,
     'step': 1, 'unit': '人', 'cfg': 'MEMBER_BRIEF_LIMIT',
     'label': '成员摘要上限', 'desc': '本轮发言人之外，还展开几个人的摘要'},
]

_BY_KEY = {s['key']: s for s in GROUP_SCHEMA}


def set_root(path=None):
    """切换设置根目录（离线模拟/测试隔离用）。"""
    global _ROOT
    _ROOT = path or DEFAULT_ROOT
    return _ROOT


def _gid(gid):
    gid = str(gid or '').strip()
    return gid if gid.isdigit() else ''


def group_dir(gid):
    return os.path.join(_ROOT, _gid(gid) or '_invalid')


def path_for(gid):
    return os.path.join(group_dir(gid), 'settings.json')


def schema():
    """字段定义表（深拷贝，调用方可随意改）。"""
    return [dict(s) for s in GROUP_SCHEMA]


def load(gid):
    """读某群的覆盖值；只保留 schema 认识且校验通过的键。"""
    gid = _gid(gid)
    if not gid:
        return {}
    try:
        with open(path_for(gid), encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, val in raw.items():
        spec = _BY_KEY.get(key)
        if not spec:
            continue
        cv = coerce(spec, val)
        if cv is not None:
            out[key] = cv
    return out


def get(gid, key):
    """某群的覆盖值；没改过返回 None（调用方继续回退）。"""
    if key not in _BY_KEY:
        return None
    return load(gid).get(key)


def overrides(gid):
    return load(gid)


def enabled(gid, legacy_off=None):
    """本群是否接管：有显式覆盖用覆盖值，否则回退旧的 group_disabled 名单。"""
    val = get(gid, 'enabled')
    if val is not None:
        return bool(val)
    off = [str(g) for g in (legacy_off or [])]
    return str(gid) not in off


def set_enabled(gid, on):
    return set_many(gid, {'enabled': bool(on)})


def set_many(gid, kv):
    """写入若干覆盖值；未知键或非法值直接报错（不静默吞掉拼写错误）。"""
    gid = _gid(gid)
    if not gid:
        raise ValueError('群号必须是数字')
    if not isinstance(kv, dict) or not kv:
        raise ValueError('没有要保存的参数')
    data = load(gid)
    for key, val in kv.items():
        spec = _BY_KEY.get(key)
        if not spec:
            raise ValueError('未知参数：%s' % key)
        cv = coerce(spec, val)
        if cv is None:
            raise ValueError('参数 %s 取值不合法：%r' % (key, val))
        data[key] = cv
    _save(gid, data)
    return dict(data)


def unset(gid, keys=None):
    """清除覆盖：keys 为空或含 'all' 时全部清除，否则只清列出的键。"""
    gid = _gid(gid)
    if not gid:
        raise ValueError('群号必须是数字')
    data = load(gid)
    if keys is None:
        keys = ['all']
    if isinstance(keys, str):
        keys = [keys]
    keys = [str(k).strip() for k in (keys or []) if str(k).strip()]
    if not keys or 'all' in keys:
        data = {}
    else:
        for key in keys:
            if key not in _BY_KEY:
                raise ValueError('未知参数：%s' % key)
            data.pop(key, None)
    _save(gid, data)
    return dict(data)


def copy(src, dst):
    """把 src 的覆盖值整套复制到 dst（替换而不是合并）。"""
    src, dst = _gid(src), _gid(dst)
    if not src or not dst:
        raise ValueError('群号必须是数字')
    if src == dst:
        raise ValueError('不能从本群复制到自己')
    data = load(src)
    _save(dst, data)
    return dict(data)


def global_value(key):
    """某参数当前的全局值：runtime_config > rp_config 常量。"""
    spec = _BY_KEY.get(key)
    if not spec:
        return None
    try:
        import runtime_config as rc
        val = rc.get(key, _MISSING)
        if val is not _MISSING:
            return val
    except Exception:
        pass
    if spec.get('cfg'):
        try:
            import rp_config as cfg
            return getattr(cfg, spec['cfg'], spec.get('default'))
        except Exception:
            pass
    return spec.get('default')


def defaults(legacy_off=None):
    """全部参数的全局值；enabled 按旧关闭名单解释。"""
    out = {s['key']: global_value(s['key']) for s in GROUP_SCHEMA}
    out['enabled'] = enabled('__none__', legacy_off)
    return out


def effective(gid, key, legacy_off=None):
    """本群值（有覆盖）或全局值。"""
    val = get(gid, key)
    if val is not None:
        return val
    if key == 'enabled':
        return enabled(gid, legacy_off)
    return global_value(key)


def effective_all(gid, legacy_off=None):
    gid = _gid(gid)
    defaults_ = defaults(legacy_off)
    out = dict(defaults_)
    if gid:
        out.update(load(gid))
    if gid:
        out['enabled'] = enabled(gid, legacy_off)
    return out


def limits(gid):
    """上下文预算三项的生效值（供 rp_core / qq_bridge 使用）。"""
    return {
        'turn_limit': effective(gid, 'group_turn_limit'),
        'msg_max_chars': effective(gid, 'group_msg_max_chars'),
        'member_brief_limit': effective(gid, 'member_brief_limit'),
    }


def known_groups():
    """已知群号：配置里的默认群 + 已有群状态目录（去重排序）。"""
    out = set()
    try:
        import rp_config as cfg
        for gid in (getattr(cfg, 'QQ_GROUPS', None) or []):
            gid = str(gid).strip()
            if gid.isdigit():
                out.add(gid)
    except Exception:
        pass
    try:
        for name in os.listdir(_ROOT):
            if name.isdigit():
                out.add(name)
    except OSError:
        pass
    return sorted(out)


def summary(gid):
    gid = _gid(gid)
    data = load(gid)
    return {'gid': gid, 'enabled': enabled(gid), 'overrides': data,
            'count': len(data)}


def coerce(spec, val):
    """按 schema 做类型转换/范围夹紧；非法返回 None。"""
    t = spec.get('type')
    try:
        if t == 'bool':
            if isinstance(val, bool):
                return val
            if isinstance(val, (int, float)):
                return val != 0
            if isinstance(val, str):
                sv = val.strip().lower()
                if sv in ('true', '1', 'yes', 'on', 'y'):
                    return True
                if sv in ('false', '0', 'no', 'off', 'n', ''):
                    return False
            return None
        if t == 'int':
            return int(_clamp(int(float(val)), spec))
        if t == 'float':
            return round(_clamp(float(val), spec), 4)
    except (TypeError, ValueError):
        return None
    return None


def _clamp(n, spec):
    lo, hi = spec.get('min'), spec.get('max')
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def _save(gid, data):
    path = path_for(gid)
    with _LOCK:
        if not data:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
