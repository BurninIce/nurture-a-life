# -*- coding: utf-8 -*-
"""角色（persona）注册表：资产路径、运行态目录与能力位。

边界（2026-11-06 定）：
    - 角色资产只读：card.json / system_en.txt / stats.json
    - 切换角色只改 runtime_config.character 指针，不修改任何资产文件
    - 模型 key 与角色无关：选哪个模型不影响「她是谁」
    - 缺资产的指令明确失效，绝不回退到别的角色资产

布局：角色资产在 prompts/personas/<id>/，运行态在 state/personas/<id>/。
Character 已迁入同一布局，不再有 legacy 路径分支。
"""
import json
import logging
import os
import re
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROMPTS_ROOT = os.path.join(ROOT, 'prompts')
PERSONAS_ROOT = os.path.join(PROMPTS_ROOT, 'personas')
STATE_ROOT = os.path.join(ROOT, 'state')
PERSONAS_STATE_ROOT = os.path.join(STATE_ROOT, 'personas')

DEFAULT_ID = 'character'
LEGACY_IDS = ()
_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')

_LOG = logging.getLogger('persona')
_LOCK = threading.RLock()
_MANIFEST_CACHE = {}      # path -> (mtime, dict)
_CTX = threading.local()


# ---------- 线程内角色上下文 ----------
# 群 worker 在任务开始时进入角色上下文；切换角色不会让在途任务读到新角色的
# 路径/规则。没有上下文时回退到 runtime_config.character。

def context(pid):
    class _Ctx(object):
        def __enter__(self):
            self.prev = getattr(_CTX, 'pid', None)
            _CTX.pid = pid
            return pid

        def __exit__(self, exc_type, exc, tb):
            _CTX.pid = self.prev
            return False
    return _Ctx()


def current():
    pid = getattr(_CTX, 'pid', None)
    return pid if pid and valid_id(pid) else active_id()


# ---------- 角色清单 ----------

def valid_id(pid):
    return bool(pid and _ID_RE.match(str(pid)))


def _manifest_path(pid):
    return os.path.join(PERSONAS_ROOT, pid, 'persona.json')


def list_ids():
    """可用角色 id：personas/ 下带 persona.json 的目录。"""
    ids = list(LEGACY_IDS)
    try:
        for name in sorted(os.listdir(PERSONAS_ROOT)):
            if not valid_id(name) or name in ids:
                continue
            if os.path.isfile(_manifest_path(name)):
                ids.append(name)
    except OSError:
        pass
    return ids


def _read_manifest(pid):
    path = _manifest_path(pid)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _LOCK:
        hit = _MANIFEST_CACHE.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        _LOG.warning('角色清单读取失败：%s', path)
        return {}
    if not isinstance(data, dict):
        return {}
    with _LOCK:
        _MANIFEST_CACHE[path] = (mtime, data)
    return data


def manifest(pid=None):
    """角色清单（缺失时返回只含 id 的最小 dict）。"""
    pid = pid or current()
    data = dict(_read_manifest(pid))
    data.setdefault('id', pid)
    return data


def forget(pid):
    """丢掉某角色的清单缓存（覆盖导入后立刻读到新文件）。"""
    with _LOCK:
        _MANIFEST_CACHE.pop(_manifest_path(pid), None)


def exists(pid):
    return bool(pid and valid_id(pid)
                and (pid in LEGACY_IDS or os.path.isfile(_manifest_path(pid))))


def resolve_id(text):
    """按 id / 展示名 / 别名解析角色（大小写不敏感）；找不到返回 ''。"""
    key = str(text or '').strip().lower()
    if not key:
        return ''
    for pid in list_ids():
        m = manifest(pid)
        names = {pid.lower(), str(m.get('label') or ''),
                 str(m.get('display_name') or '')}
        names.update(str(x) for x in (m.get('aliases') or []))
        if key in {n.strip().lower() for n in names if n.strip()}:
            return pid
    return ''


def active_id():
    """runtime_config 里的角色指针；非法/不存在则回退 Character（不改盘）。"""
    try:
        import runtime_config as rc
        pid = rc.get('character')
    except Exception:
        pid = None
    if pid and exists(pid):
        return pid
    if pid and pid != DEFAULT_ID:
        _LOG.warning('角色指针 %r 不存在，回退 %s', pid, DEFAULT_ID)
    return DEFAULT_ID


def activate(pid):
    """切换角色：只校验并写指针，不修改任何资产/记忆。返回 (ok, message)。"""
    ok, reason = validate(pid, strict=True)
    if not ok:
        return False, reason
    try:
        import runtime_config as rc
        rc.set_value('character', pid)
    except Exception as e:
        return False, '写入角色指针失败：%s' % e
    return True, display_name(pid)


# ---------- 展示信息与发言门 ----------

def display_name(pid=None):
    pid = pid or current()
    m = manifest(pid)
    name = str(m.get('display_name') or m.get('label') or '').strip()
    if name:
        return name
    if pid == DEFAULT_ID:
        return '苏禾'
    return pid


def aliases(pid=None):
    pid = pid or current()
    m = manifest(pid)
    out = [str(x).strip() for x in (m.get('aliases') or []) if str(x).strip()]
    if display_name(pid) not in out:
        out.insert(0, display_name(pid))
    return out


def _cfg_defaults():
    try:
        import rp_config as cfg
        return cfg
    except Exception:
        return None


def gate(pid=None):
    """发言门用词：direct_names / interest_words / expert_words。

    清单里没写的项回退到 rp_config（Character 的现有值），保证老角色零变化；
    显式写成空数组表示「这一类词就是没有」，不再回退。
    """
    pid = pid or current()
    m = manifest(pid)
    g = m.get('gate') if isinstance(m.get('gate'), dict) else {}
    cfg = _cfg_defaults()

    def _pick(key, attr, default):
        if key in g:
            val = g.get(key)
            if isinstance(val, (list, tuple)):
                return tuple(str(x) for x in val if str(x).strip())
            if val is None:
                return ()
        if cfg is not None and hasattr(cfg, attr):
            return tuple(getattr(cfg, attr))
        return tuple(default)

    return {
        'direct_names': _pick('direct_names', 'GATE_DIRECT_NAMES',
                              ('苏禾', '阿禾')),
        'interest_words': _pick('interest_words', 'GATE_INTEREST_WORDS', ()),
        'expert_words': _pick('expert_words', 'GATE_EXPERT_WORDS', ()),
    }


# 角色级发言决策参数：写进 persona.json 的 gate.<key>，只影响该角色。
# 包含 L1 权重/门槛与频率冷却；逻辑层读 gate_tuning()，
# 导入时缺项由 gate_defaults() 补齐。
GATE_TUNING_RANGES = {
    'consider_at': (0.0, 2.0),
    'reply_at': (0.0, 2.0),
    'w_question': (0.0, 1.0),
    'w_topic': (0.0, 1.0),
    'w_interest': (0.0, 1.0),
    'w_emotion': (0.0, 1.0),
    'w_neglect': (0.0, 1.0),
    'w_continuation': (0.0, 1.0),
    'w_memory': (0.0, 1.0),
    'w_random': (0.0, 1.0),
    'memory_sim_at': (0.0, 1.0),
    'group_cooldown': (0.0, 600.0),
    'user_cooldown': (0.0, 3600.0),
    'max_per_min': (1.0, 60.0),
    'expert_cooldown_factor': (0.1, 3.0),
}
GATE_TUNING_INTS = ('group_cooldown', 'user_cooldown', 'max_per_min')
# 缺项时取哪个 rp_config 常量作为默认值
_GATE_TUNING_CFG = {
    'consider_at': 'GATE_CONSIDER_AT',
    'reply_at': 'GATE_REPLY_AT',
    'w_question': 'GATE_W_QUESTION',
    'w_topic': 'GATE_W_TOPIC',
    'w_interest': 'GATE_W_INTEREST',
    'w_emotion': 'GATE_W_EMOTION',
    'w_neglect': 'GATE_W_NEGLECT',
    'w_continuation': 'GATE_W_CONTINUATION',
    'w_memory': 'GATE_W_MEMORY',
    'w_random': 'GATE_W_RANDOM',
    'memory_sim_at': 'GATE_MEMORY_SIM_AT',
    'group_cooldown': 'GATE_GROUP_COOLDOWN',
    'user_cooldown': 'GATE_USER_COOLDOWN',
    'max_per_min': 'GATE_MAX_PER_MIN',
    'expert_cooldown_factor': 'GATE_EXPERT_COOLDOWN_FACTOR',
}


def _clamp_tuning(key, value):
    lo, hi = GATE_TUNING_RANGES[key]
    val = max(lo, min(hi, float(value)))
    return int(round(val)) if key in GATE_TUNING_INTS else val


def gate_defaults(keys=None):
    """发言决策参数的默认值（来自 rp_config，已按范围夹紧）。

    不传 keys 返回全部支持项；导入时只补 GATE_DECISION_KEYS。
    """
    cfg = _cfg_defaults()
    out = {}
    for key in (keys or GATE_TUNING_RANGES):
        attr = _GATE_TUNING_CFG.get(key)
        val = getattr(cfg, attr, None) if (cfg is not None and attr) else None
        if val is None:
            continue
        try:
            out[key] = _clamp_tuning(key, val)
        except (TypeError, ValueError):
            continue
    return out


def gate_tuning(pid=None):
    """角色级发言门覆盖值；非法/缺省项直接忽略，越界值夹到安全范围。"""
    pid = pid or current()
    m = manifest(pid)
    g = m.get('gate') if isinstance(m.get('gate'), dict) else {}
    out = {}
    for key in GATE_TUNING_RANGES:
        if key not in g:
            continue
        try:
            out[key] = _clamp_tuning(key, g[key])
        except (TypeError, ValueError):
            continue
    return out


def group_roster_limit(pid=None):
    """成员块按关系展开的人数上限（清单 group.roster_limit）。

    擅长处理人际关系的角色可以写大一些；0/缺失表示用 rp_config 默认值。
    """
    m = manifest(pid)
    g = m.get('group') if isinstance(m.get('group'), dict) else {}
    try:
        val = int(g.get('roster_limit'))
    except (TypeError, ValueError):
        return 0
    return val if val > 0 else 0


def bubble_limit(pid=None):
    """一次回复最多几条消息（清单 bubble_limit，默认 2，上限 10）。

    1 = 只发一条（换行仍保留在同一条里）；超过 10 按 10 处理。
    """
    try:
        val = int(manifest(pid).get('bubble_limit') or 2)
    except (TypeError, ValueError):
        return 2
    return max(1, min(10, val))


def qq_profile(pid=None):
    """可选的 QQ 昵称/头像同步配置。返回 (spec, reason)；未启用返回 (None, '')。

    严格模式：启用了但昵称/头像缺失就不返回 spec，避免只改一半。
    """
    pid = pid or current()
    m = manifest(pid)
    p = m.get('qq_profile') if isinstance(m.get('qq_profile'), dict) else {}
    if not p.get('enabled'):
        return None, ''
    nickname = str(p.get('nickname') or display_name(pid)).strip()
    if not nickname:
        return None, '昵称缺失'
    avatar = _abs(p.get('avatar'))
    if avatar and not os.path.isfile(avatar):
        return None, '头像文件缺失：%s' % avatar
    return {'nickname': nickname, 'avatar': avatar}, ''


def direct_name(pid=None):
    """群里点名叫她的首选短名（@ 解析与直达判定共用）。"""
    names = gate(pid).get('direct_names') or ()
    return str(names[0]) if names else display_name(pid)


_PROMPT_CONST = {
    'daily': 'DAILY_GEN_PROMPT',
    'private_proactive': 'GATE_PRIVATE_PROACTIVE_PROMPT',
    'gate_judge': 'GATE_JUDGE_PROMPT',
    'gate_proactive': 'GATE_PROACTIVE_PROMPT',
}


def prompt(key, pid=None, **kw):
    """角色专属 prompt；清单里没有则回退 rp_config 的常量（Character 老路径）。"""
    pid = pid or current()
    m = manifest(pid)
    prompts = m.get('prompts') if isinstance(m.get('prompts'), dict) else {}
    text = prompts.get(key)
    if not text:
        cfg = _cfg_defaults()
        attr = _PROMPT_CONST.get(key, key.upper())
        text = getattr(cfg, attr, '') if cfg is not None else ''
    # 回退的全局 prompt 用 {name} 表示角色名；调用方忘了传也不会格式化失败。
    kw.setdefault('name', direct_name(pid))
    try:
        return str(text).format(**kw) if text else ''
    except (KeyError, IndexError, ValueError):
        _LOG.warning('角色 %s 的 prompt %s 格式化失败', pid, key)
        return str(text or '')


# ---------- 资产路径 ----------

def _abs(path):
    path = str(path or '').strip()
    if not path:
        return ''
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def _default_asset(pid, kind):
    names = {'card': 'card.json', 'system': 'system_en.txt',
             'stats': 'stats.json', 'tts': 'tts.json', 'cg': 'cg.json'}
    return os.path.join(PERSONAS_ROOT, pid, names.get(kind, kind + '.json'))


def asset_path(kind, pid=None):
    pid = pid or current()
    m = manifest(pid)
    assets = m.get('assets') if isinstance(m.get('assets'), dict) else {}
    return _abs(assets.get(kind)) or _default_asset(pid, kind)


def card_json_path(pid=None):
    return asset_path('card', pid)


def system_path(pid=None):
    return asset_path('system', pid)


def stats_path(pid=None):
    return asset_path('stats', pid)


def tts_path(pid=None):
    return asset_path('tts', pid)


def cg_path(pid=None):
    return asset_path('cg', pid)


# ---------- 运行态路径 ----------

def state_root(pid=None):
    pid = pid or current()
    return os.path.join(PERSONAS_STATE_ROOT, pid)


def safe_uid(uid):
    s = str(uid if uid is not None else '').strip()
    return (re.sub(r'[^0-9A-Za-z_-]', '_', s)[:64] or 'unknown')


def user_dir(uid, pid=None):
    """QQ 用户目录：始终落在当前角色的运行态根目录下。"""
    pid = pid or current()
    if not uid:
        return state_root(pid)
    return os.path.join(state_root(pid), 'users', safe_uid(uid))


def daily_scene_path(pid=None):
    return os.path.join(state_root(pid), 'daily_scene.json')


def self_state_path(pid=None):
    return os.path.join(state_root(pid), 'self_state.json')


def private_topic_path(pid=None):
    return os.path.join(state_root(pid), 'private_topic.json')


def tts_root(pid=None):
    return os.path.join(state_root(pid), 'tts')


def cg_root(pid=None):
    return os.path.join(state_root(pid), 'cg')


def lore_index_dir(pid=None):
    return os.path.join(state_root(pid), 'lore_index')


def novel_index_dir(pid=None):
    pid = pid or current()
    m = manifest(pid)
    assets = m.get('assets') if isinstance(m.get('assets'), dict) else {}
    path = _abs(assets.get('novel'))
    if path:
        return path
    return os.path.join(state_root(pid), 'novel_index')


def corpus_spec(pid=None):
    """语料资产：{samples, cache, header}；没有则 None（不回退别人的语料）。"""
    pid = pid or current()
    m = manifest(pid)
    assets = m.get('assets') if isinstance(m.get('assets'), dict) else {}
    spec = assets.get('corpus')
    if isinstance(spec, dict) and spec.get('samples'):
        cfg = _cfg_defaults()
        return {
            'samples': _abs(spec.get('samples')),
            'cache': _abs(spec.get('cache')) or os.path.join(
                state_root(pid), 'corpus_embeddings.json'),
            'header': str(spec.get('header') or (getattr(cfg, 'RAG_HEADER', '') if cfg else '')),
        }
    if pid == DEFAULT_ID:
        cfg = _cfg_defaults()
        return {
            'samples': os.path.join(ROOT, 'corpus', 'samples_clean.jsonl'),
            'cache': os.path.join(state_root(pid), 'corpus_embeddings.json'),
            'header': getattr(cfg, 'RAG_HEADER', '') if cfg else '',
        }
    return None


def capabilities(pid=None):
    """当前角色可用的能力位；缺资产一律 False，不回退。"""
    pid = pid or current()
    caps = {'tts': False, 'cg': False, 'corpus': False, 'novel': False,
            'lore': False}
    caps['tts'] = _enabled_asset(tts_path(pid))
    caps['cg'] = _enabled_asset(cg_path(pid))
    spec = corpus_spec(pid)
    caps['corpus'] = bool(spec and os.path.isfile(spec.get('samples') or ''))
    try:
        import novel_rag
        caps['novel'] = bool(novel_rag.available(pid))
    except Exception:
        nd = novel_index_dir(pid)
        caps['novel'] = all(os.path.exists(os.path.join(nd, n))
                            for n in ('chunks.jsonl', 'embeddings.npy', 'meta.json'))
    try:
        data = _load_json(card_json_path(pid))
        caps['lore'] = bool((data.get('lore') or {}).get('entries'))
    except Exception:
        pass
    return caps


def _load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _enabled_asset(path):
    if not path or not os.path.isfile(path):
        return False
    try:
        return _load_json(path).get('enabled', True) is not False
    except (OSError, ValueError):
        return False


def cg_asset_problems(pid=None):
    """检查角色的 cg.json 是否能被 CG 模块解析；没有该文件不算问题。"""
    pid = pid or current()
    if not os.path.isfile(cg_path(pid)):
        return []
    try:
        import cg_profile
        cg_profile.load(card_json_path(pid))
    except Exception as e:
        return ['CG 配置不可用：%s' % str(e)[:160]]
    return []


# ---------- 激活校验 ----------

def validate(pid, strict=True):
    """返回 (ok, reason)。strict=True 时要求卡/系统提示/数值规则都齐全。"""
    if not valid_id(pid):
        return False, '角色 id 非法：%r' % (pid,)
    if not exists(pid):
        return False, '角色目录不存在：%s' % _manifest_path(pid)
    for kind, label in (('card', '角色卡'), ('system', 'system 提示')):
        path = asset_path(kind, pid)
        if not os.path.isfile(path):
            return False, '%s缺失：%s' % (label, path)
    card = None
    try:
        card = _load_json(card_json_path(pid))
    except (OSError, ValueError) as e:
        return False, '角色卡无法读取：%s' % e
    for key in ('static', 'relationship', 'lore'):
        if not isinstance(card.get(key), dict):
            return False, '角色卡缺少 %s 段' % key
    if not strict:
        return True, ''
    path = stats_path(pid)
    if not os.path.isfile(path):
        return False, '数值规则缺失：%s' % path
    try:
        cfg = _load_json(path)
    except (OSError, ValueError) as e:
        return False, '数值规则无法读取：%s' % e
    chars = cfg.get('characters') if isinstance(cfg, dict) else None
    if not isinstance(chars, dict) or pid not in chars:
        return False, '数值规则里没有角色 %s（禁止借用其他角色规则）' % pid
    if cfg.get('active') != pid:
        return False, '数值规则 active=%r，与角色 %s 不一致' % (cfg.get('active'), pid)
    return True, ''


