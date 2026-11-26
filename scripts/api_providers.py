# -*- coding: utf-8 -*-
"""面板可增删的自定义 API 供应商（state/providers.json）。

存储只记端点信息，Key 不写进 JSON：
    file 模式 → state/provider_keys/<id>.key（已 gitignore）
    env  模式 → 当前用户的持久环境变量（复用首次启动向导的写法）

预设、端点/Key 校验、Key 写盘全部复用 setup_config / quickstart，
新增的供应商以 qwen|<id>|<模型> 的复合键接入现有调用链（见 rp_config）。

安全约束：
- 至少保留一个自定义供应商（含首次启动向导配置的 custom 档），不能删空。
- 正在被对话/判官/辅助调用使用的供应商不允许删除，避免运行期 404。
"""
import json
import os
import re
import tempfile
import time

import paths
import setup_config

# 测试可整体替换（与 paths 解耦，便于落在临时目录）
ROOT = paths.ROOT
STATE_DIR = paths.STATE_DIR

SETUP_PROVIDER_ID = 'custom'
# 这些 id 已被内置后端/计费档占用，用户供应商不能再取
RESERVED_IDS = ('local', 'lab', 'qwen', 'payg', 'tokenplan', 'custom')
ID_RE = re.compile(r'^[a-z][a-z0-9_-]{1,23}$')
MAX_PROVIDERS = 20


class ApiProviderError(ValueError):
    """供应商增删校验失败（面板直接把 message 显示给用户）。"""


def providers_path():
    return os.path.join(STATE_DIR, 'providers.json')


def key_dir():
    return os.path.join(STATE_DIR, 'provider_keys')


def key_path(pid):
    return os.path.join(key_dir(), pid + '.key')


def _key_file_name(pid):
    """rp_common.read_key_file 接受的路径：优先项目内相对路径。"""
    path = key_path(pid)
    try:
        rel = os.path.relpath(path, ROOT)
    except ValueError:          # 跨盘符
        return path
    return path if rel.startswith('..') else rel


def _setup_spec():
    try:
        return setup_config.model_spec(setup_config.load())
    except Exception:
        return None


def _setup_record():
    """首次启动向导配置的 custom 档：只读展示，不在这里删除。"""
    spec = _setup_spec()
    if not spec:
        return None
    return {
        'id': SETUP_PROVIDER_ID,
        'label': spec['label'],
        'base_url': spec['base_url'],
        'key_env': spec['key_env'],
        'dashscope': bool(spec.get('dashscope')),
        'probe_model': spec['gen_model'],
        'source': 'setup',
        'deletable': False,
    }


def _norm(raw):
    """丢掉损坏/字段不全的行，保证运行期不会拿到半个供应商。"""
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get('id') or '').strip().lower()
    if not ID_RE.match(pid) or pid in RESERVED_IDS:
        return None
    base_url = str(raw.get('base_url') or '').strip()
    if not setup_config._url_ok(base_url):
        return None
    label = str(raw.get('label') or pid).strip()[:40]
    env = str(raw.get('key_env') or '').strip()
    return {
        'id': pid,
        'label': label or pid,
        'base_url': base_url,
        'key_env': env if setup_config.KEY_ENV_RE.match(env) else '',
        'dashscope': bool(raw.get('dashscope')),
        'probe_model': str(raw.get('probe_model') or '').strip(),
        'source': 'user',
        'created_at': str(raw.get('created_at') or ''),
    }


def _load_raw():
    try:
        with open(providers_path(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    rows = data.get('providers') if isinstance(data, dict) else data
    out, seen = [], set()
    for raw in rows if isinstance(rows, list) else []:
        rec = _norm(raw)
        if rec and rec['id'] not in seen:
            seen.add(rec['id'])
            out.append(rec)
    return out


def _save(rows):
    os.makedirs(STATE_DIR, exist_ok=True)
    payload = {'providers': rows}
    fd, tmp = tempfile.mkstemp(prefix='.providers-', suffix='.json',
                               dir=STATE_DIR or '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, providers_path())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _env_present(name):
    name = str(name or '').strip()
    if not name:
        return False
    if os.environ.get(name):
        return True
    try:
        return bool(setup_config.user_env(name))
    except Exception:
        return False


def _record_has_key(rec):
    if rec.get('source') == 'setup':
        return _env_present(rec.get('key_env')) or os.path.exists(
            os.path.join(ROOT, setup_config.MODEL_KEY_FILE))
    return _env_present(rec.get('key_env')) or os.path.exists(key_path(rec['id']))


def _public(rec):
    out = dict(rec)
    out['has_key'] = _record_has_key(rec)
    out.setdefault('deletable', True)
    return out


def _norm_url(url):
    return str(url or '').strip().rstrip('/').lower()


def _read_key_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def _key_values(rec):
    """只用于重复检测：该供应商可能生效的 Key（环境变量与 Key 文件各算一份）。"""
    vals = set()
    env = str(rec.get('key_env') or '').strip()
    if env:
        try:
            vals.add(str(setup_config.user_env(env) or '').strip())
        except Exception:
            pass
        vals.add(str(os.environ.get(env) or '').strip())
    if rec.get('source') == 'setup':
        path = os.path.join(ROOT, setup_config.MODEL_KEY_FILE)
    else:
        path = key_path(rec['id'])
    vals.add(_read_key_file(path))
    vals.discard('')
    return vals


def _numbered_label(label, existing):
    labels = {str(r.get('label') or '') for r in existing}
    if label not in labels:
        return label
    for i in range(2, 100):
        cand = ('%s %d' % (label[:36], i)).strip()
        if cand not in labels:
            return cand
    raise ApiProviderError('同名供应商太多，请换个名称')


def providers():
    """面板列表：向导 custom（若有）+ 用户新增的供应商。"""
    out = []
    setup = _setup_record()
    if setup:
        out.append(_public(setup))
    out.extend(_public(r) for r in _load_raw())
    return out


def custom_count():
    return len(_load_raw()) + (1 if _setup_record() else 0)


def payload():
    return {
        'providers': providers(),
        'presets': setup_config.presets_payload(),
        'minimum': 1,
    }


def profile_specs():
    """并入 rp_config.QWEN_PROFILES 的运行期规格（不含密钥本身）。"""
    out = {}
    for rec in _load_raw():
        out[rec['id']] = {
            'label': rec['label'],
            'key_env': rec['key_env'] or ('NAL_%s_KEY'
                                          % rec['id'].upper().replace('-', '_')),
            'base_url': rec['base_url'],
            'dashscope': bool(rec['dashscope']),
            'probe_model': rec['probe_model'],
            'key_file': _key_file_name(rec['id']),
            'source': 'user',
        }
    return out


def _slug(text):
    s = re.sub(r'[^a-z0-9]+', '-', str(text or '').lower()).strip('-')
    return s[:24].strip('-')


def _unique_id(base, rows):
    base = _slug(base) or 'api'
    if not ID_RE.match(base):
        base = 'api'
    used = {r['id'] for r in rows} | set(RESERVED_IDS)
    if base not in used:
        return base
    for i in range(2, 100):
        cand = '%s-%d' % (base[:20], i)
        if cand not in used:
            return cand
    raise ApiProviderError('供应商太多，无法分配新标识')


def _write_key(pid, key_env, key, mode):
    """写 Key，返回需要回滚的文件路径（env 模式返回 None）。"""
    key = str(key or '').strip()
    if not key:
        return None
    if mode == setup_config.MODEL_KEY_MODE_ENV:
        import quickstart
        quickstart.save_env_var(key_env, key)
        return None
    path = key_path(pid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(key + '\n')
    os.replace(tmp, path)
    return path


def add(data):
    """新增一个供应商；返回新增记录（不含 Key）。

    data: {preset?, label?, base_url?, model?, key?, key_mode?, key_env?}
    """
    data = data if isinstance(data, dict) else {}
    rows = _load_raw()
    if len(rows) >= MAX_PROVIDERS:
        raise ApiProviderError('最多 %d 个自定义供应商' % MAX_PROVIDERS)

    preset_id = str(data.get('preset') or '').strip().lower()
    preset = setup_config.PRESET_BY_ID.get(preset_id) or {}
    label = str(data.get('label') or preset.get('label') or '').strip()
    if not label:
        raise ApiProviderError('请填写供应商名称')
    label = label[:40]
    base_url = str(data.get('base_url') or preset.get('base_url') or '').strip()
    err = setup_config.endpoint_error(base_url)
    if err:
        raise ApiProviderError(err)
    model = str(data.get('model') or preset.get('gen_model') or '').strip()

    seed = preset_id if preset_id and preset_id != 'custom' else label
    pid = _unique_id(data.get('id') or seed, rows)

    env_given = bool(str(data.get('key_env') or '').strip())
    key_env = str(data.get('key_env') or preset.get('key_env') or '').strip()
    if not key_env:
        key_env = 'NAL_%s_KEY' % pid.upper().replace('-', '_')
    if not setup_config.KEY_ENV_RE.match(key_env):
        raise ApiProviderError('环境变量名只能用字母/数字/下划线，且不能以数字开头')

    mode = str(data.get('key_mode') or setup_config.MODEL_KEY_MODE_FILE).lower()
    if mode not in setup_config.KEY_MODES:
        raise ApiProviderError('Key 保存方式只能是 file 或 env')
    key = str(data.get('key') or '').strip()
    key_err = setup_config.key_error(key)
    if key_err:
        raise ApiProviderError(key_err)

    # 同端点的重复供应商：Key 或全局变量名撞了就拒；都不同才允许，名字加序号区分。
    existing = providers()
    dups = [r for r in existing
            if _norm_url(r.get('base_url')) == _norm_url(base_url)]
    key_same = bool(key) and any(key in _key_values(r) for r in dups)
    env_same = any(str(r.get('key_env') or '') == key_env for r in dups)
    if key_same or env_same:
        if key_same and env_same:
            raise ApiProviderError(
                '已有相同的 API 配置：端点、Key 与全局变量名都相同，不要重复添加')
        if key_same:
            raise ApiProviderError(
                '已有相同的 API 配置：端点与 Key 相同，不要重复添加')
        raise ApiProviderError(
            '已有相同的 API 配置：端点与全局变量名相同，换个环境变量名再添加')

    if any(r.get('key_env') == key_env for r in existing):
        if env_given:
            owner = next(r for r in existing if r.get('key_env') == key_env)
            raise ApiProviderError('全局变量名已被「%s」使用，换一个再添加'
                                   % owner.get('label'))
        key_env = 'NAL_%s_KEY' % pid.upper().replace('-', '_')
    label = _numbered_label(label, existing)

    record = {
        'id': pid,
        'label': label,
        'base_url': base_url,
        'key_env': key_env,
        'dashscope': bool(preset.get('dashscope')),
        'probe_model': model,
        'source': 'user',
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    written = None
    try:
        if key:
            written = _write_key(pid, key_env, key, mode)
        rows.append(record)
        _save(rows)
    except Exception:
        if written:
            try:
                os.unlink(written)
            except OSError:
                pass
        raise
    return _public(record)


def _in_use_ids():
    """当前对话与所有模型通道正在使用的供应商 id。"""
    try:
        import rp_core
        import rp_common
        import runtime_config as rc
    except Exception:
        return set()
    used = set()
    targets = [('gen_model', lambda: rc.get('gen_model', ''))]
    targets += [(cid, lambda cid=cid: rp_common.resolve_info(cid)[2])
                for cid in getattr(rp_common, 'CHANNEL_BY_ID', {})]
    for _label, resolve in targets:
        try:
            profile = rp_core.parse_model_key(resolve() or '')[3]
        except Exception:
            profile = None
        if profile:
            used.add(str(profile).strip().lower())
    prof = str(rc.get('api_profile', '') or '').strip().lower()
    if prof:
        used.add(prof)
    return used


def remove(pid):
    """删除一个用户供应商；违反最少保留/占用约束时抛 ApiProviderError。"""
    pid = str(pid or '').strip().lower()
    if pid == SETUP_PROVIDER_ID:
        raise ApiProviderError('向导配置的供应商不能在这里删除')
    rows = _load_raw()
    rec = next((r for r in rows if r['id'] == pid), None)
    if not rec:
        raise ApiProviderError('供应商不存在：%s' % (pid or '(空)'))
    if custom_count() <= 1:
        raise ApiProviderError('至少保留一个自定义供应商')
    if pid in _in_use_ids():
        raise ApiProviderError('该供应商正在使用，先切换对话/判官/辅助调用模型再删除')
    rows = [r for r in rows if r['id'] != pid]
    _save(rows)
    try:
        os.unlink(key_path(pid))
    except OSError:
        pass
    return _public(rec)
