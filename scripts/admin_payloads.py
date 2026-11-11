# -*- coding: utf-8 -*-
"""管理面板的只读数据组装（status / 记忆概要 / 成员 / 群氛围 / 角色卡 / 模型目录 / TTS / CG）。

admin_server 只保留 HTTP 路由与写操作；这里不碰请求对象、不开端口，
所有函数都是只读的，单测可以直接调用。唯一的外部注入是 `set_bridge()`
（面板读取实时统计用；独立进程运行时为 None）。
"""
import json
import os
import socket
import time

import card_source
import heartbeat
import paths
import hw_profile as hw
import persona
import runtime_config as rc

BRIDGE = None

_PORT_CACHE = {}       # {(host, port): (result, ts)}
_PORT_CACHE_TTL = 3.0  # 秒：探测结果缓存，避免每 10s 轮询都重探慢端口

_TTS_CACHE = {'ts': 0.0, 'data': None}
_TTS_CACHE_TTL = 5.0   # 秒：TTS 健康检查较重（离线要等超时），短缓存

CG_SIZES = ((768, 512), (512, 768), (768, 768), (1024, 576), (576, 1024))


def set_bridge(bridge):
    global BRIDGE
    BRIDGE = bridge


def _port_open(host, port, timeout=0.15):
    """TCP 端口是否可达（探测 NapCat/Ollama），带短缓存。

    缓存 3 秒：避免前端每 10 秒轮询时，对未监听的端口每次都等满
    timeout（未监听端口是 TimeoutError 而非快速拒绝，0.5s 会明显拖慢面板）。
    """
    key = (host, port)
    now = time.time()
    hit = _PORT_CACHE.get(key)
    if hit and now - hit[1] < _PORT_CACHE_TTL:
        return hit[0]
    try:
        with socket.create_connection((host, port), timeout=timeout):
            result = True
    except (OSError, socket.timeout):
        result = False
    _PORT_CACHE[key] = (result, now)
    return result


def _napcat_online():
    return _port_open('127.0.0.1', 6700)  # OneBot 反向 WS 端口


def _ollama_online():
    return _port_open('127.0.0.1', paths.PORT_OLLAMA)


def _user_state_dir(uid):
    """面板操作的记忆目录：跟随当前角色。"""
    return persona.user_dir(uid)


def _mem_readable(uid):
    """只读面板：没有记忆文件就别进 state_scope（它会顺手建目录）。"""
    return os.path.isfile(os.path.join(_user_state_dir(uid), 'memory_card.json'))


def _mem_summary(uid):
    """某用户记忆卡概要；失败返回空。"""
    out = {}
    if not _mem_readable(uid):
        return out
    try:
        import memory_card as mem
        with mem.state_scope(_user_state_dir(uid)):
            d = mem.load()
        c = d.get('card', {})
        out = {
            'uid': uid,
            'affection': c.get('affection'),
            'trust': c.get('trust'),
            'familiarity': c.get('familiarity'),
            'relationship': c.get('relationship'),
            'events': len([e for e in d.get('episodes', []) if not e.get('inactive')]),
            'interactions': d.get('interaction_count'),
            'pending': len(d.get('pending', []) or []),
        }
    except Exception:
        pass
    return out


def _mem_detail(uid):
    """某用户完整记忆卡明细：事件库 + 归纳产物 + 话题缓冲。"""
    if not _mem_readable(uid):
        return {}
    try:
        import memory_card as mem
        with mem.state_scope(_user_state_dir(uid)):
            d = mem.load()
        c = d.get('card', {})
        eps = [e for e in d.get('episodes', []) if not e.get('inactive')]
        eps_sorted = sorted(eps, key=lambda e: e.get('time') or '', reverse=True)
        ep_by_id = {e.get('id'): (e.get('summary') or '') for e in (d.get('episodes') or [])}
        inductions = []
        for it in (d.get('inductions') or [])[-30:]:
            if not isinstance(it, dict):
                continue
            ids = it.get('episode_ids') or []
            inductions.append({
                'ts': it.get('ts') or '',
                'round': it.get('round'),
                'before': it.get('before') or {},
                'after': it.get('after') or {},
                'delta': it.get('delta') or {},
                'episodes': [{'id': i, 'summary': ep_by_id.get(i, '')} for i in ids],
            })
        return {
            'uid': str(uid),
            'interaction_count': d.get('interaction_count'),
            'last_induction_round': d.get('last_induction_round'),
            'card': {
                'affection': c.get('affection'),
                'trust': c.get('trust'),
                'familiarity': c.get('familiarity'),
                'relationship': c.get('relationship'),
                'impressions': c.get('impressions') or [],
                'facts': c.get('facts') or [],
                'self_disclosure': c.get('self_disclosure') or [],
                'reflections': c.get('reflections') or [],
            },
            'episodes': [{
                'id': e.get('id'),
                'summary': e.get('summary') or '',
                'scene': e.get('scene') or '',
                'emotion': e.get('emotion') or '',
                'importance': e.get('importance'),
                'tags': e.get('tags') or [],
                'user_said': e.get('user_said') or '',
                'character_said': e.get('character_said') or '',
                'speaker': e.get('speaker') or '',
                'channel': e.get('channel') or '',
                'favorite': bool(e.get('favorite')),
                'time': e.get('time') or '',
                'start_time': e.get('start_time') or '',
            } for e in eps_sorted],
            'inductions': inductions,
            'pending': d.get('pending') or [],
            'deletions': [{
                'ts': rec.get('ts') or '',
                'reason': rec.get('reason') or '',
                'episodes': [{
                    'id': e.get('id'),
                    'summary': e.get('summary') or '',
                    'time': e.get('time') or '',
                    'channel': e.get('channel') or '',
                } for e in (rec.get('episodes') or []) if isinstance(e, dict)],
            } for rec in (d.get('deletions') or [])[-20:] if isinstance(rec, dict)],
        }
    except Exception:
        return {}


def _personas_payload():
    """可选角色清单：严格校验 + 能力位，供面板下拉与状态展示。"""
    out = []
    for pid in persona.list_ids():
        ok, reason = persona.validate(pid, strict=True)
        out.append({
            'id': pid,
            'name': persona.display_name(pid),
            'ok': ok,
            'reason': reason,
            'capabilities': persona.capabilities(pid),
            'active': pid == persona.active_id(),
        })
    return out


# 角色卡面板：发言决策字段的展示名与分组（范围/类型仍以 persona.py 为准）
_GATE_FIELDS = {
    'consider_at': ('终审门槛', '打分到此值才交给模型终审', '门槛', 0.01, ''),
    'reply_at': ('兜底回复阈值', '终审模型不可用时，打分到此值直接回', '门槛', 0.01, ''),
    'w_question': ('疑问句权重', '有人可能想听她说', '权重', 0.05, ''),
    'w_topic': ('话题相关权重', '', '权重', 0.05, ''),
    'w_interest': ('兴趣词权重', '', '权重', 0.05, ''),
    'w_emotion': ('情绪强度权重', '感叹、长文本、情绪词', '权重', 0.05, ''),
    'w_neglect': ('被冷落权重', '她有一阵没说话', '权重', 0.05, ''),
    'w_continuation': ('延续权重', '她刚说过话、话题还在延续', '权重', 0.05, ''),
    'w_memory': ('记忆命中权重', '', '权重', 0.05, ''),
    'w_random': ('随机权重', '拟人随机，避免行为可预测', '权重', 0.05, ''),
    'memory_sim_at': ('记忆相似度门槛', '低于此相似度不算“聊过”', '记忆', 0.01, ''),
    'group_cooldown': ('同群发言间隔', '0 = 不限制', '冷却', 1, '秒'),
    'user_cooldown': ('同人发言间隔', '0 = 不限制', '冷却', 1, '秒'),
    'max_per_min': ('每分钟发言上限', '防止刷屏，@ 也受此保护', '冷却', 1, '条/分'),
    'expert_cooldown_factor': ('熟悉话题冷却系数',
                               '她本行话题时同群间隔的乘子，越小越愿意接', '冷却', 0.1, '×'),
}


def _gate_schema():
    out = []
    for key, (lo, hi) in persona.GATE_TUNING_RANGES.items():
        label, desc, group, step, unit = _GATE_FIELDS.get(
            key, (key, '', '其他', 0.05, ''))
        out.append({
            'key': key,
            'label': label,
            'desc': desc,
            'group': group,
            'type': 'int' if key in persona.GATE_TUNING_INTS else 'float',
            'min': lo,
            'max': hi,
            'step': step,
            'unit': unit,
        })
    return out


def _persona_detail(pid=None):
    """角色卡编辑页的数据：清单、卡原文、发言决策字段表与一致性状态。

    manifest.gate 给的是**生效值**：卡里写了用卡里的，没写的按 rp_config 的默认值
    补齐。否则卡里省略了门槛与权重的角色（仓库里的示例卡就是）在面板上是一整列空白，
    看不出她此刻用的是 0.30 还是 0.72。保存时这些值一并写回卡里。
    """
    pid = (pid or '').strip().lower() or persona.active_id()
    if not persona.valid_id(pid) or not persona.exists(pid):
        raise ValueError('角色不存在：%s' % (pid or '(空)'))
    ok, reason = persona.validate(pid, strict=True)
    try:
        card = card_source.load(pid=pid)
    except (OSError, ValueError) as e:
        card = {}
        ok, reason = False, 'card.json 读取失败：%s' % e
    manifest = dict(persona.manifest(pid))
    gate = dict(persona.gate_defaults())
    gate.update(manifest.get('gate') or {})
    manifest['gate'] = gate
    if not manifest.get('bubble_limit'):
        manifest['bubble_limit'] = persona.bubble_limit(pid)
    return {
        'id': pid,
        'name': persona.display_name(pid),
        'valid': ok,
        'reason': reason,
        'active': pid == persona.active_id(),
        'manifest': manifest,
        'card': card,
        'gate_schema': _gate_schema(),
        'stale': card_source.check(pid=pid),
    }


def _schema_payload():
    """字段定义表 + 当前值，供前端自动渲染。

    hidden 字段（角色指针、模型/思考）不进通用表单：角色在右上角切换，
    模型由前端两块独立模型卡渲染。
    """
    try:
        vals = rc.all()
    except Exception:
        vals = {}
    schema = []
    for spec in rc.SCHEMA:
        item = dict(spec)
        if item.get('hidden'):
            continue
        item.pop('hidden', None)
        schema.append(item)
    import rp_config as cfg
    return {
        'schema': schema,
        'values': vals,
        'personas': _personas_payload(),
        # 模型卡里的「端点档位」下拉据此渲染
        'profiles': [{'id': pid, 'label': (spec or {}).get('label', pid)}
                     for pid, spec in cfg.QWEN_PROFILES.items()],
        'model_blocks': {
            'gen': _model_block('gen_model', 'gen_thinking', '对话模型',
                                '决定她说什么；本地模型最省，API 模型可开思考'),
            'judge': _model_block('judge_model', 'judge_thinking', '判官模型',
                                  '终审、主动开口判定、晋升与记忆归纳'),
        },
        'thinking': _thinking_meta(),
        'catalog_limit': rc.get('model_catalog_limit', 10),
    }


def _model_block(key, thinking_key, title, desc):
    """模型卡的当前状态：提供商/端点档位/模型 ID/思考挡位。"""
    import rp_core as core
    raw = rc.get(key, '')
    kind, model_id, _think, profile = core.parse_model_key(raw)
    provider = {'local': 'local', 'localapi': 'localapi', 'api': 'qwen'}.get(kind, 'local')
    prof = profile or rc.get('api_profile', 'dashscope')
    return {
        'key': key,
        'thinking_key': thinking_key,
        'title': title,
        'desc': desc,
        'providers': _providers(provider, prof),
        'provider': provider,
        'profile': prof,
        'model': model_id,
        'value': raw,
        'thinking': rc.get(thinking_key, 'off'),
    }


def _api_key_ready(env_name, file_name):
    """key 到位没有：环境变量或项目根的 key 文件，任一即可。"""
    return bool((os.environ.get(env_name) or '').strip() or _read_key_file(file_name))


def _providers(current=None, profile=None):
    """下拉里只放本机真配好了的提供商，顺序照 _CATALOG_LABELS 的书写顺序。

    没 key 的云端档、没填过端点的自建兼容服务都不该占一格。但已经存进
    runtime_config 的那个提供商即使此刻缺 key 也要留在下拉里——否则面板会
    默认选中第一项，用户点一下「应用模型」就把云端模型覆盖成本地模型 ID。
    """
    import rp_config as cfg
    spec = cfg.QWEN_PROFILES.get((profile or cfg.QWEN_PROFILE).strip().lower())
    ready = {
        'local': True,                                  # 随 Ollama 一起启动
        'localapi': cfg.LOCAL_API_CONFIGURED,            # 向导或环境变量给过端点
        'qwen': bool(spec) and _api_key_ready(spec['key_env'], cfg.QWEN_API_KEY_FILE),
    }
    if current in _CATALOG_LABELS:
        ready[current] = True
    return [{'id': p, 'label': _CATALOG_LABELS[p]}
            for p in _CATALOG_LABELS if ready.get(p)]


def _thinking_meta():
    from rp_config import API_THINKING_LEVELS, API_THINKING_LABELS
    return {'levels': list(API_THINKING_LEVELS), 'labels': dict(API_THINKING_LABELS)}


# ---------- 模型目录（面板下拉用，带条数上限） ----------

_CATALOG_TTL = 60.0
_CATALOG_CACHE = {}     # {(provider, profile): (ids, error, ts)}
# 书写顺序 = 面板提供商下拉的顺序
_CATALOG_LABELS = {'local': '本地 Ollama', 'localapi': '自建兼容端点', 'qwen': '云端 API'}
# 这些明显不是对话模型，列出来只会挤掉真正可用的选项
_NON_CHAT_HINTS = ('embedding', 'tts', 'asr', 'audio', 'rerank', 'ocr',
                   'stable-diffusion', 'image', 'video')


def _catalog_fallback(provider, profile=None):
    """拉不到清单时的兜底：注册表里已知的该提供商模型。"""
    import rp_config as cfg
    if provider == 'local':
        return [m['model'] for m in cfg.MODEL_REGISTRY if m.get('kind') == 'local']
    if provider == 'localapi':
        return [cfg.LOCAL_API_MODEL]
    return [m['model'] for m in cfg.MODEL_REGISTRY if m.get('kind') == 'api'] \
        + [cfg.DAILY_MODEL]


def _http_json(url, headers=None, timeout=8):
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _fetch_catalog(provider, profile=None):
    """从对应后端拉完整模型清单；返回 (ids, error)。"""
    import rp_config as cfg
    if provider == 'local':
        data = _http_json('http://127.0.0.1:%d/api/tags' % paths.PORT_OLLAMA, timeout=3)
        return [m.get('name') for m in (data.get('models') or []) if m.get('name')], ''

    if provider == 'localapi':
        key = (os.environ.get(cfg.LOCAL_API_KEY_ENV, '')
               or _read_key_file(cfg.LOCAL_API_KEY_FILE))
        data = _http_json(cfg.LOCAL_API_BASE_URL.rstrip('/') + '/models',
                          {'Authorization': 'Bearer ' + key} if key else None,
                          timeout=10)
    else:
        prof = (profile or cfg.QWEN_PROFILE).strip().lower()
        spec = cfg.QWEN_PROFILES.get(prof)
        if not spec:
            raise RuntimeError('未知端点档位：%s' % profile)
        key = (os.environ.get(spec['key_env'], '')
               or _read_key_file(cfg.QWEN_API_KEY_FILE))
        if not key:
            raise RuntimeError('缺少 %s 的 API key' % spec['label'])
        data = _http_json(spec['base_url'].rstrip('/') + '/models',
                          {'Authorization': 'Bearer ' + key}, timeout=10)
    rows = data.get('data') if isinstance(data, dict) else None
    if rows is None and isinstance(data, dict):
        rows = data.get('models')
    ids = []
    for row in rows or []:
        mid = row.get('id') or row.get('name') if isinstance(row, dict) else row
        if mid:
            ids.append(str(mid))
    return ids, ''


def _read_key_file(name):
    from rp_common import read_key_file
    return read_key_file(name)


def _short_error(e):
    """把网络异常压成一句人话。

    面板的提示行要让人知道下一步怎么做，所以只留原因类别；把
    「<urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>」
    原样抄进面板既难读，也把 Windows 的措辞混进了产品文案。
    """
    text = str(e) or e.__class__.__name__
    low = text.lower()
    if '401' in low or 'unauthorized' in low or 'invalid api key' in low \
            or 'invalid_api_key' in low:
        return '鉴权失败，检查 key'
    if '403' in low or 'forbidden' in low:
        return '这个 key 没有列模型的权限'
    if 'timed out' in low or 'timeout' in low:
        return '连接超时'
    if ('10061' in low or 'connection refused' in low
            or 'actively refused' in low or 'max retries' in low
            or 'name or service not known' in low
            or 'getaddrinfo failed' in low):
        return '连不上端点'
    if '404' in low:
        return '端点没有 /models'
    return text[:60]


def _catalog_ids(provider, profile):
    """带缓存的全量清单（失败回退注册表，并记录错误）。"""
    now = time.time()
    key = (provider, (profile or '').strip().lower())
    hit = _CATALOG_CACHE.get(key)
    if hit and now - hit[2] < _CATALOG_TTL:
        return hit[0], hit[1]
    try:
        ids, err = _fetch_catalog(provider, profile)
    except Exception as e:
        ids, err = [], _short_error(e)
    if not ids:
        ids = _catalog_fallback(provider, profile)
    _CATALOG_CACHE[key] = (ids, err, now)
    return ids, err


def _is_chat_model(mid):
    low = mid.lower()
    return not any(h in low for h in _NON_CHAT_HINTS)


def model_catalog(provider, profile=None, limit=None, current=None):
    """面板模型下拉：按上限截断，超限时给出「去提供者查看」提示。

    current: 当前已选模型 ID；即使不在前 N 个也保留在列表里（否则下拉显示为空）。
    """
    provider = (provider or 'local').strip().lower()
    if provider not in _CATALOG_LABELS:
        raise ValueError('未知提供商：%s' % provider)
    try:
        limit = int(limit if limit is not None else rc.get('model_catalog_limit', 10))
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 100))
    raw_ids, err = _catalog_ids(provider, profile)
    seen = set()
    ids = []
    for mid in raw_ids:
        if mid in seen or not _is_chat_model(mid):
            continue
        seen.add(mid)
        ids.append(mid)
    # 当前模型置顶，其余让千问系/常用模型靠前（清单常有几百条，前 10 个要能选）
    if current and current in seen:
        ids.remove(current)
        ids.insert(0, current)
    liked = [m for m in ids if m.lower().startswith('qwen')]
    rest = [m for m in ids if not m.lower().startswith('qwen')]
    ordered = liked + rest
    shown = ordered[:limit]
    truncated = len(ordered) > limit
    label = _CATALOG_LABELS[provider]
    if err:
        hint = '拉取清单失败（%s），可直接填写模型 ID' % err
    elif truncated:
        hint = ('只列出前 %d 个（共 %d 个）；其余请到%s查看，可直接填写模型 ID'
                % (limit, len(ordered), label))
    else:
        hint = ''
    return {
        'provider': provider,
        'label': label,
        'profile': (profile or '').strip().lower(),
        'models': shown,
        'total': len(ordered),
        'limit': limit,
        'truncated': truncated,
        'error': err,
        'hint': hint,
    }


def _first_identity_uid():
    """主用户（用户）uid：优先 QQ_IDENTITY_USERS 第一个。"""
    import rp_config as cfg
    return next(iter(getattr(cfg, 'QQ_IDENTITY_USERS', {})), None)


def _members_payload():
    """全部群成员卡（member.json）列表，附各自好感（记忆卡）。"""
    out = []
    try:
        import member_card
        for m in member_card.all_members():
            uid = m.get('uid')
            row = {
                'uid': uid,
                'nickname': m.get('nickname') or '',
                'alias': m.get('alias') or '',
                'call_name': m.get('call_name') or '',
                'identity': m.get('identity') or '',
                'gender': m.get('gender') or '',
                'style': m.get('style') or '',
                'relation_note': m.get('relation_note') or '',
                'promoted': bool(m.get('promoted')),
                'promoted_at': m.get('promoted_at') or '',
                'last_seen': m.get('last_seen') or '',
                'msg_count': m.get('msg_count') or 0,
            }
            mc = _mem_summary(uid)
            if mc:
                row['affection'] = mc.get('affection')
                row['trust'] = mc.get('trust')
                row['familiarity'] = mc.get('familiarity')
                row['relationship'] = mc.get('relationship')
                row['events'] = mc.get('events')
                row['interactions'] = mc.get('interactions')
            out.append(row)
    except Exception:
        pass
    return out


def _group_payload(gid=None):
    """群氛围 + 最近事件 + 成员风格画像（group_scene）。"""
    import rp_config as cfg
    gid = str(gid or '').strip() or (getattr(cfg, 'QQ_GROUPS', None) or [None])[0]
    if not gid:
        return {}
    try:
        import group_scene
        data = group_scene.load(gid)
        members = []
        for uid, m in (data.get('members') or {}).items():
            members.append({
                'uid': uid,
                'nickname': m.get('nickname') or uid,
                'style': m.get('style') or '',
                'last_seen': m.get('last_seen') or '',
            })
        return {
            'gid': str(gid),
            'scene': data.get('scene') or '',
            'vibe': data.get('vibe') or '',
            'updated': data.get('updated') or '',
            'recent_events': [{
                'summary': e.get('summary') or '',
                'importance': e.get('importance'),
                'time': e.get('time') or '',
            } for e in (data.get('recent_events') or [])[-8:]],
            'members': members,
        }
    except Exception:
        return {}


def _groups_payload():
    """面板群列表：已知群 + 是否接管 + 单独覆盖项数。"""
    import group_settings
    off = rc.get('group_disabled') or []
    rows = []
    for gid in group_settings.known_groups():
        data = group_settings.overrides(gid)
        rows.append({
            'gid': gid,
            'enabled': group_settings.enabled(gid, legacy_off=off),
            'count': len(data),
            'overrides': data,
        })
    return {'groups': rows}


def _group_settings_payload(gid):
    """某个群的参数页：schema + 本群覆盖 + 生效值 + 全局回退值。"""
    import group_settings
    gid = str(gid or '').strip()
    if not gid.isdigit():
        raise ValueError('群号必须是数字')
    off = rc.get('group_disabled') or []
    return {
        'gid': gid,
        'enabled': group_settings.enabled(gid, legacy_off=off),
        'overrides': group_settings.overrides(gid),
        'values': group_settings.effective_all(gid, legacy_off=off),
        'global': group_settings.defaults(legacy_off=off),
        'schema': group_settings.schema(),
    }


def _models_payload():
    """模型清单（供前端下拉框）。label 的唯一来源是 rp_config.MODEL_REGISTRY。"""
    import rp_config as cfg
    return [{'key': m['key'], 'label': m['label']} for m in cfg.MODEL_REGISTRY]


def _api_payload():
    """当前千问 API 档位与 key 配对情况（只给前缀，不暴露 key）。"""
    import rp_config as cfg
    from rp_common import read_key_file
    prof = cfg.QWEN_PROFILES.get(cfg.QWEN_PROFILE) or {}
    key = (os.environ.get(cfg.QWEN_API_KEY_ENV, '')
           or read_key_file(cfg.QWEN_API_KEY_FILE))
    want = cfg.QWEN_KEY_PREFIX.get(cfg.QWEN_PROFILE)
    return {
        'profile': cfg.QWEN_PROFILE,
        'label': prof.get('label', cfg.QWEN_PROFILE),
        'base_url': cfg.QWEN_BASE_URL,
        'key_env': cfg.QWEN_API_KEY_ENV,
        'key_prefix': (key[:6] + '…') if key else 'MISSING',
        'key_ok': bool(key) and (not want or key.startswith(want)),
    }


def _stats_payload():
    """qq_bridge 实时运行统计；独立进程运行时为空。"""
    if BRIDGE is None:
        return None
    try:
        st = BRIDGE.stats
        return {
            'events': st.get('events', 0),
            'replies': st.get('replies', 0),
            'ignored': st.get('ignored', 0),
            'dup': st.get('dup', 0),
            'private': st.get('private', 0),
            'errors': st.get('errors', 0),
            'memory_errors': st.get('memory_errors', 0),
            'model': BRIDGE.model,
            'judge_model': BRIDGE.judge_model,
            'self_id': BRIDGE.self_id,
            'groups': {gid: {'queue': gs.queue.qsize(), 'dropped': gs.dropped}
                       for gid, gs in (BRIDGE.groups or {}).items()},
        }
    except Exception:
        return None


def _supervisor_payload():
    """守护进程与桥接心跳：守护自己可能活着但桥接已停，两个都要分开看。"""
    now = time.time()
    sup = heartbeat.read(heartbeat.SUPERVISOR_PATH)
    hb = heartbeat.read(heartbeat.BRIDGE_PATH)
    sup_age = heartbeat.age(sup, now)
    hb_age = heartbeat.age(hb, now)
    try:
        last_restart = float((sup or {}).get('last_restart_at') or 0)
    except (TypeError, ValueError):
        last_restart = 0.0
    return {
        'enabled': sup is not None,
        'alive': sup_age is not None and sup_age <= 35,
        'state': (sup or {}).get('state') or 'unknown',
        'detail': (sup or {}).get('detail') or '',
        'restarts': (sup or {}).get('restarts', 0),
        'supervisor_age': sup_age,
        'heartbeat_age': hb_age,
        'last_restart_age': (now - last_restart) if last_restart else None,
        'bridge_pid': (hb or {}).get('pid'),
        'bridge_uptime': (hb or {}).get('uptime_sec'),
        'napcat_connected': (hb or {}).get('napcat_connected'),
    }


def status_payload():
    return {
        'napcat_online': _napcat_online(),
        'ollama_online': _ollama_online(),
        'hw': {
            'vram_gb': hw.VRAM_GB,
            'source': hw.SOURCE,
            'num_ctx': hw.NUM_CTX,
            'concurrency': hw.CONCURRENCY,
            'embed_model': hw.EMBED_MODEL,
        },
        'model': {
            'gen': rc.get('gen_model', ''),
            'judge': rc.get('judge_model', ''),
        },
        'character': {
            'id': persona.active_id(),
            'name': persona.display_name(),
            'capabilities': persona.capabilities(),
        },
        'api': _api_payload(),
        'mem': _mem_summary(_first_identity_uid()) if _first_identity_uid() else {},
        'stats': _stats_payload(),
        'supervisor': _supervisor_payload(),
    }


def _tts_url():
    import rp_config as cfg
    return cfg.TTS_URL_DEFAULT


def _tts_health():
    """TTS 服务健康信息（缓存 5 秒；面板 10s 轮询不会把离线超时叠加）。"""
    now = time.time()
    if _TTS_CACHE['data'] is not None and now - _TTS_CACHE['ts'] < _TTS_CACHE_TTL:
        return _TTS_CACHE['data']
    import tts_client
    url = _tts_url()
    ref = os.environ.get('NAL_TTS_REF_AUDIO') or ''
    h = tts_client.health(url, timeout=3)
    if h:
        data = {'online': True, 'url': url,
                'ref_audio': h.get('ref_audio') or ref}
    else:
        data = {'online': False, 'url': url, 'ref_audio': ref}
    _TTS_CACHE.update(ts=now, data=data)
    return data


def _cg_dir(uid):
    import cg_gen
    return os.path.join(cg_gen.cg_root(), uid)


def _cg_name_ok(name):
    return (name.endswith('.png') and '..' not in name
            and all(c.isalnum() or c in '._-' for c in name))


def _cg_status():
    import comfy_client
    info = comfy_client.describe()
    h = comfy_client.health()
    return {'url': info['url'],
            'workflow': info['workflow'],
            'connected': bool(h.get('ok')),
            'gpu': h.get('gpu'),
            'vram_free_mb': h.get('vram_free_mb'),
            'error': h.get('error'),
            'sizes': [{'w': w, 'h': h} for w, h in CG_SIZES]}


def _cg_prompt(uid, stage=None):
    try:
        import cg_gen
        pid = persona.active_id()
        return cg_gen.preview(uid, stage=stage,
                              card=persona.card_json_path(pid), pid=pid)
    except Exception as e:
        return {'error': str(e)[:200]}


def _cg_images(uid, limit=12):
    folder = _cg_dir(uid)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    rows = []
    for name in names:
        if not name.endswith('.png'):
            continue
        try:
            with open(os.path.join(folder, os.path.splitext(name)[0] + '.json'),
                      encoding='utf-8') as f:
                meta = json.load(f)
            stat = os.stat(os.path.join(folder, name))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict) or meta.get('engine') != 'comfyui':
            continue
        rows.append({'name': name, 'size': stat.st_size,
                     'mtime': int(stat.st_mtime),
                     'stage': meta.get('stage'),
                     'seed': meta.get('seed'),
                     'width': meta.get('width'),
                     'height': meta.get('height'),
                     'elapsed_ms': meta.get('elapsed_ms'),
                     'prompt': (meta.get('prompt') or '')[:400]})
    rows.sort(key=lambda r: r['mtime'], reverse=True)
    return rows[:limit]
