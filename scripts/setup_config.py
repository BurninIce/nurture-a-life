# -*- coding: utf-8 -*-
"""首次启动绑定信息（state/setup.json）。

代码里的默认身份是开发机上的账号；刚拿到项目的人不能沿用，所以由
scripts/quickstart.py 询问一次并写入这里。启动链按下面的字段读取：

    self_qq      bot 自己登录的 QQ（NapCat 扫码的那个号）
    admin_qq     管理员 QQ（/status、/character 等指令的放行者）
    group_id     只守的群；留空 = 不限制（加入哪个群就服务哪个群）
    napcat_home  NapCat Shell 目录（含 NapCatWinBootMain.exe）
    ollama_home  Ollama 目录（可选，只用云端 API 时可留空）

    model_provider   对话/判官使用的 OpenAI 兼容服务商（见 MODEL_PRESETS；空=不配置）
    model_base_url   端点地址（选预设时预填，可改）
    model_gen        对话模型 ID
    model_judge      判官模型 ID（留空 = 跟对话用同一个）
    model_key_mode   file=明文写进项目根 qwen.key；env=写入用户环境变量
    model_key_env    环境变量名（选预设时预填）

Key 本身**不写进 setup.json**：file 模式落到 qwen.key，env 模式写进
HKCU\\Environment，两者都已被 .gitignore 覆盖。向导之外的读取方只认
环境变量或 key 文件，不解析 setup.json 里的密钥。

优先级：环境变量（NAL_*） > setup.json > 代码默认值。
没有 setup.json 时所有读取方退回原默认值，老安装不受影响。
测试用 NAL_SETUP 指向临时文件（NAL_HOME 同理）。
"""
import json
import os
import re
import tempfile
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.environ.get('NAL_HOME') or os.path.dirname(HERE))

QQ_RE = re.compile(r'^[1-9]\d{4,11}$')
KEY_ENV_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
MODEL_KEY_MODE_FILE = 'file'
MODEL_KEY_MODE_ENV = 'env'
KEY_MODES = (MODEL_KEY_MODE_FILE, MODEL_KEY_MODE_ENV)
MODEL_KEY_FILE = 'qwen.key'      # 与 rp_config.QWEN_API_KEY_FILE 同一个文件

# 国内常见的 OpenAI 兼容端点。模型 ID 只是预填值，用户可改；
# dashscope=True 的端点认 enable_thinking / reasoning_effort / enable_search，
# 其余只发标准 OpenAI 字段（多发的厂商参数会 400）。
MODEL_PRESETS = (
    {'id': 'dashscope', 'label': '阿里云百炼（千问）',
     'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
     'key_env': 'DASHSCOPE_API_KEY',
     'gen_model': 'qwen-plus', 'judge_model': 'qwen-max', 'dashscope': True},
    {'id': 'deepseek', 'label': 'DeepSeek 开放平台',
     'base_url': 'https://api.deepseek.com/v1',
     'key_env': 'DEEPSEEK_API_KEY',
     'gen_model': 'deepseek-chat', 'judge_model': 'deepseek-chat'},
    {'id': 'siliconflow', 'label': '硅基流动 SiliconFlow',
     'base_url': 'https://api.siliconflow.cn/v1',
     'key_env': 'SILICONFLOW_API_KEY',
     'gen_model': 'deepseek-ai/DeepSeek-V3',
     'judge_model': 'deepseek-ai/DeepSeek-V3'},
    {'id': 'moonshot', 'label': '月之暗面 Kimi',
     'base_url': 'https://api.moonshot.cn/v1',
     'key_env': 'MOONSHOT_API_KEY',
     'gen_model': 'moonshot-v1-8k', 'judge_model': 'moonshot-v1-8k'},
    {'id': 'zhipu', 'label': '智谱 GLM',
     'base_url': 'https://open.bigmodel.cn/api/paas/v4',
     'key_env': 'ZHIPU_API_KEY',
     'gen_model': 'glm-4.6', 'judge_model': 'glm-4.6'},
    {'id': 'custom', 'label': '自定义（OpenAI 兼容）',
     'base_url': '', 'key_env': 'CUSTOM_API_KEY',
     'gen_model': '', 'judge_model': ''},
)
PRESET_BY_ID = {p['id']: p for p in MODEL_PRESETS}

MODEL_FIELDS = ('model_provider', 'model_base_url', 'model_gen', 'model_judge',
                'model_key_mode', 'model_key_env')
FIELDS = ('self_qq', 'admin_qq', 'group_id', 'napcat_home', 'ollama_home') \
    + MODEL_FIELDS + ('base_url', 'model')


def setup_path():
    override = (os.environ.get('NAL_SETUP') or '').strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(ROOT, 'state', 'setup.json')


def normalize(data):
    """只留已知字段并去空白；缺字段补空字符串。"""
    out = {}
    for key in FIELDS:
        val = data.get(key, '') if isinstance(data, dict) else ''
        out[key] = '' if val is None else str(val).strip()
    return out


def load(path=None):
    """读 setup.json；不存在或损坏时返回 {}（启动链不因它中断）。"""
    try:
        with open(path or setup_path(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return normalize(data) if isinstance(data, dict) else {}


def exists(path=None):
    return os.path.exists(path or setup_path())


def presets_payload():
    """给向导/面板用的预设清单（不含任何密钥）。"""
    return [dict(p) for p in MODEL_PRESETS]


def model_spec(data):
    """setup → 规范化后的模型端点规格；没配置或配置不完整返回 None。

    key_env 缺省取预设；key_mode 缺省 file；judge 缺省跟随 gen。
    只做组装，不做校验（validate 负责报错）。
    """
    d = data if isinstance(data, dict) else normalize(data)
    provider = str(d.get('model_provider') or '').strip().lower()
    preset = PRESET_BY_ID.get(provider) or {}
    base_url = str(d.get('model_base_url') or preset.get('base_url') or '').strip()
    gen = str(d.get('model_gen') or preset.get('gen_model') or '').strip()
    judge = str(d.get('model_judge') or '').strip() or gen
    key_env = str(d.get('model_key_env') or preset.get('key_env') or '').strip()
    key_mode = str(d.get('model_key_mode') or MODEL_KEY_MODE_FILE).strip().lower()
    if not (provider and base_url and gen and key_env):
        return None
    return {
        'provider': provider,
        'label': preset.get('label') or '自定义端点',
        'base_url': base_url,
        'gen_model': gen,
        'judge_model': judge,
        'key_env': key_env,
        'key_mode': key_mode,
        'dashscope': bool(preset.get('dashscope')),
    }


def key_error(key):
    """Key 的格式检查（空串 = 没填，合法：可以稍后补）。"""
    key = str(key or '')
    if not key:
        return ''
    if len(key) > 512:
        return 'Key 过长（上限 512 字符）'
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key):
        return 'Key 不能包含空白或控制字符'
    return ''


def _url_ok(url):
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ('http', 'https') and bool(p.netloc)


def endpoint_error(url):
    """OpenAI 兼容端点地址的校验提示；空串 = 合法。"""
    url = str(url or '').strip()
    if not url:
        return '需要端点地址（OpenAI 兼容的 /v1 地址）'
    if not _url_ok(url):
        return '端点需要是 http:// 或 https:// 地址'
    return ''


def validate(data):
    """[(字段, 提示), ...]；空列表表示可以保存。"""
    d = normalize(data)
    errors = []
    if not QQ_RE.match(d['self_qq']):
        errors.append(('self_qq', '机器人 QQ 需要 5-12 位数字（NapCat 要登录的那个号）'))
    if not QQ_RE.match(d['admin_qq']):
        errors.append(('admin_qq', '管理员 QQ 需要 5-12 位数字（建议填你自己的号）'))
    elif d['admin_qq'] == d['self_qq']:
        errors.append(('admin_qq',
                       '管理员不能是机器人自己：bot 会忽略自己发的消息，'
                       '届时没人能下管理指令'))
    if d['group_id'] and not QQ_RE.match(d['group_id']):
        errors.append(('group_id', '群号需要 5-12 位数字；不指定群就留空'))
    for key, label in (('napcat_home', 'NapCat 目录'),
                       ('ollama_home', 'Ollama 目录')):
        if d[key] and not os.path.isdir(d[key]):
            errors.append((key, '%s不存在：%s' % (label, d[key])))
    errors.extend(_validate_model(d))
    return errors


def _validate_model(d):
    provider = d['model_provider'].strip().lower()
    if not provider:
        if any(d[k] for k in ('model_base_url', 'model_gen', 'model_judge')):
            return [('model_provider', '先选择服务商，再填端点与模型')]
        return []
    if provider not in PRESET_BY_ID:
        return [('model_provider', '未知服务商：%s' % provider)]
    preset = PRESET_BY_ID[provider]
    errors = []
    base_url = d['model_base_url'] or preset.get('base_url') or ''
    err = endpoint_error(base_url)
    if err:
        errors.append(('model_base_url', err))
    if not (d['model_gen'] or preset.get('gen_model')):
        errors.append(('model_gen', '需要对话模型的 ID'))
    if d['model_key_mode'] and d['model_key_mode'].lower() not in KEY_MODES:
        errors.append(('model_key_mode', 'Key 保存方式只能是 file 或 env'))
    key_env = d['model_key_env'] or preset.get('key_env') or ''
    if not KEY_ENV_RE.match(key_env):
        errors.append(('model_key_env',
                       '环境变量名只能用字母/数字/下划线，且不能以数字开头'))
    return errors


def user_env(name):
    """读当前用户的持久环境变量（新开的控制台不会继承 setx 的结果）。"""
    name = str(name or '').strip()
    if not name:
        return ''
    if os.name != 'nt':
        return os.environ.get(name, '')
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as k:
            val, _kind = winreg.QueryValueEx(k, name)
            return str(val or '').strip()
    except (OSError, ImportError, ValueError):
        return ''


def apply_env(target=None):
    """env 模式的模型 Key 补进当前进程环境，返回变量名（没有可用值返回 ''）。

    每个读 rp_config 的进程（桥接/面板/守护）启动时都会经过这里：
    setx 只影响之后新开的控制台，start.bat 那一份环境是旧的。
    """
    setup = load()
    name = setup.get('model_key_env') or ''
    if (setup.get('model_key_mode') or '').lower() != MODEL_KEY_MODE_ENV or not name:
        return ''
    env = os.environ if target is None else target
    if env.get(name):
        return name
    val = user_env(name)
    if val:
        env[name] = val
    return name if val else ''


def save(data, path=None):
    """校验后原子写入（同目录临时文件 + os.replace），返回落盘路径。"""
    p = path or setup_path()
    d = normalize(data)
    errors = validate(d)
    if errors:
        raise ValueError('；'.join(msg for _, msg in errors))
    parent = os.path.dirname(p) or '.'
    os.makedirs(parent, exist_ok=True)
    try:
        with open(p, encoding='utf-8') as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    payload = dict(d)
    # 兼容字段：老版本 rp_config 只认 base_url / model，向导写下的模型规格同时落到这两项
    spec = model_spec(d)
    if spec:
        payload['base_url'] = spec.get('base_url') or ''
        payload['model'] = spec.get('gen_model') or ''
    payload['created_at'] = (old.get('created_at') if isinstance(old, dict)
                             else '') or now
    payload['updated_at'] = now
    fd, tmp = tempfile.mkstemp(prefix='.setup-', suffix='.json', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p
