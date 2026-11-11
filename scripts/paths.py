# -*- coding: utf-8 -*-
"""运行环境路径的唯一来源。

项目的运行期文件都在项目目录里（`.venv`、`state/`、`logs/`、`local_api.key`），
外部程序（Ollama、NapCat）先按环境变量、setup.json、项目内的 `tools/` 找，
再退到 PATH 与各盘常见安装位置，所以装在哪都能启动：

    NAL_HOME          项目根目录（默认由本文件位置推得）
    NAL_VENV_PY       bot 用的 Python 解释器（默认 <ROOT>/.venv/Scripts/python.exe）
    NAL_OLLAMA_HOME   Ollama 安装目录（默认 <ROOT>/tools/Ollama，其次按位置探测）
    NAL_NAPCAT_HOME   NapCat Shell 目录（默认 <ROOT>/tools/NapCat/Shell，其次探测）

环境变量与 setup.json 里的路径可以写相对值（`tools/Ollama`），按项目根目录展开。
端口与身份同样可覆盖：NAL_PORT_<环节>、NAL_SELF_ID / GROUP_ID / ADMIN_ID。

launcher 在拉起子进程前调用 `export_env()`，把解析结果写回环境变量，
`.bat` 直接复用 `%NAL_*%`，避免 bat 和 Python 各存一份路径。
"""
import json
import os
import re
import shutil

# ---- 项目根目录 ----
HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ROOT = os.path.dirname(HERE)
ROOT = os.path.abspath(os.environ.get('NAL_HOME') or _DEFAULT_ROOT)

SCRIPTS_DIR = os.path.join(ROOT, 'scripts')
STATE_DIR = os.path.join(ROOT, 'state')
ADMIN_UI_DIR = os.path.join(ROOT, 'admin_ui')


def _load_setup():
    """state/setup.json —— 首次运行的 `scripts/setup.py` 写下的绑定。

    读不到、读坏、不是对象，一律返回 {}：所有读取方退回下面的默认值，
    老安装行为和以前完全一致。
    """
    try:
        with open(os.path.join(STATE_DIR, 'setup.json'), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


SETUP = _load_setup()


def _pick(env_name, setup_key, default):
    """环境变量 > setup.json > 代码默认值（空字符串算没设）。"""
    val = (os.environ.get(env_name) or '').strip()
    if val:
        return val
    val = str(SETUP.get(setup_key) or '').strip()
    return val or default


def abs_path(value):
    """把相对路径按项目根目录展开。

    环境变量和 setup.json 里写 `tools/Ollama` 这种相对路径时按 ROOT 解析，
    这样整套目录搬走之后配置不用跟着改，也避免把某一台机器的盘符写进配置。
    """
    path = str(value or '').strip()
    if not path:
        return ''
    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(path):
        path = os.path.join(ROOT, path)
    return os.path.normpath(path)


def _pick_path(env_name, setup_key):
    """外部程序目录的显式配置：环境变量 > setup.json，都没有时返回空串。"""
    for value in (os.environ.get(env_name), SETUP.get(setup_key)):
        path = abs_path(value)
        if path:
            return path
    return ''

# ---- 解释器 ----
VENV_PY = os.environ.get('NAL_VENV_PY') or os.path.join(
    ROOT, '.venv', 'Scripts', 'python.exe')

# ---- 外部程序（Ollama / NapCat） ----
_FIXED_DRIVES = None


def _fixed_drives():
    """本机固定磁盘的盘符，C 在最先。

    只认 DRIVE_FIXED，跳过网络盘和光驱：探测不存在的网络盘会卡几秒，
    而装在这两类盘上的 Ollama 本来也起不来。取不到时返回空表。
    """
    global _FIXED_DRIVES
    if _FIXED_DRIVES is not None:
        return _FIXED_DRIVES
    _FIXED_DRIVES = []
    if os.name == 'nt':
        try:
            import ctypes
            get_type = ctypes.windll.kernel32.GetDriveTypeW
        except Exception:
            get_type = None
        if get_type is not None:
            for letter in 'CDEFGHIJKLMNOPQRSTUVWXYZAB':
                root = letter + ':\\'
                try:
                    if get_type(root) == 3:      # DRIVE_FIXED
                        _FIXED_DRIVES.append(root)
                except Exception:
                    continue
    return _FIXED_DRIVES


def _first_dir(fallback, *candidates):
    """按顺序取第一个存在的目录；一个都没有时返回 fallback，好让报错信息固定。"""
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return fallback


def _inside_root(path):
    """这个路径是否落在项目目录里（决定要不要把模型目录也钉在项目里）。"""
    try:
        return os.path.commonpath([os.path.abspath(path), ROOT]) == ROOT
    except ValueError:
        return False


def _ollama_home():
    """Ollama 安装目录：显式配置 > 项目内 tools/Ollama > PATH > 常见安装位置。

    默认装在项目里（`<ROOT>/tools/Ollama`），这样整套目录搬走就能带走；
    已经有系统级安装的机器也能直接用上，不必为了跑这个项目再下一份。
    相对路径（环境变量或 setup.json 里的 `tools/Ollama`）按项目根目录展开。
    """
    picked = _pick_path('NAL_OLLAMA_HOME', 'ollama_home')
    if picked:
        return picked
    inside = os.path.join(ROOT, 'tools', 'Ollama')
    found = shutil.which('ollama')
    local = os.environ.get('LOCALAPPDATA') or ''
    prog = os.environ.get('PROGRAMFILES') or ''
    drives = [os.path.join(root, 'Ollama') for root in _fixed_drives()]
    return _first_dir(inside,
                      os.path.dirname(os.path.abspath(found)) if found else '',
                      os.path.join(local, 'Programs', 'Ollama') if local else '',
                      os.path.join(prog, 'Ollama') if prog else '',
                      *drives)


OLLAMA_HOME = _ollama_home()
OLLAMA_EXE = os.path.join(OLLAMA_HOME, 'ollama.exe')
# 装在项目里时把模型也放在同一层，别让几个 G 的权重散到用户目录；
# 用系统级安装时保持它原来的模型目录，免得看不见之前下过的模型。
OLLAMA_MODELS = (os.path.join(OLLAMA_HOME, 'models')
                 if _inside_root(OLLAMA_HOME) else '')


def _napcat_home():
    """NapCat Shell 目录：显式配置 > 项目内 tools/NapCat/Shell > 常见位置。

    NapCat 没有安装器，解压到哪就在哪，所以按目录名猜一遍；猜不中时
    launcher 会报出具体缺的路径，用 NAL_NAPCAT_HOME 指过去即可。
    """
    picked = _pick_path('NAL_NAPCAT_HOME', 'napcat_home')
    if picked:
        return picked
    inside = os.path.join(ROOT, 'tools', 'NapCat', 'Shell')
    local = os.environ.get('LOCALAPPDATA') or ''
    home = os.path.expanduser('~')
    drives = [os.path.join(root, 'NapCat', 'Shell') for root in _fixed_drives()]
    return _first_dir(inside,
                      os.path.join(local, 'NapCat', 'Shell') if local else '',
                      os.path.join(home, 'NapCat', 'Shell') if home else '',
                      *drives)


NAPCAT_HOME = _napcat_home()
NAPCAT_EXE = os.path.join(NAPCAT_HOME, 'NapCatWinBootMain.exe')


def _port(env_name, default):
    try:
        return int(os.environ.get(env_name, default))
    except (TypeError, ValueError):
        return default


# ---- 端口 ----
def _ollama_port():
    """Ollama 的端口：NAL_PORT_OLLAMA > OLLAMA_HOST 里的端口 > 11434。

    OLLAMA_HOST 是 Ollama 自己的变量（`127.0.0.1:11434`）。已经 export 过的机器
    按它走，免得我们按 11434 去连一个改了端口的服务。
    """
    raw = os.environ.get('NAL_PORT_OLLAMA')
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    host = os.environ.get('OLLAMA_HOST') or ''
    m = re.search(r':(\d+)\s*$', host)
    if m:
        return int(m.group(1))
    return 11434


PORT_OLLAMA = _ollama_port()
PORT_NAPCAT_UI = _port('NAL_PORT_NAPCAT_UI', 6099)
PORT_BRIDGE_WS = _port('NAL_PORT_BRIDGE_WS', 6700)
PORT_ADMIN = _port('NAL_PORT_ADMIN', 6780)

# ---- 身份（与 rp_config.py 保持一致；首次运行的向导会写进 setup.json）----
QQ_SELF_ID = _pick('NAL_SELF_ID', 'self_qq', '10000000')
# 群号留空 = 不限制（加进哪个群就服务哪个群）；_run_bot.bat 会按空值省掉 --group
GROUP_ID = _pick('NAL_GROUP_ID', 'group_id', '')
ADMIN_ID = _pick('NAL_ADMIN_ID', 'admin_qq', '')

# ---- 启动 bat ----
RUN_NAPCAT_BAT = os.path.join(SCRIPTS_DIR, '_run_napcat.bat')
RUN_BOT_BAT = os.path.join(SCRIPTS_DIR, '_run_bot.bat')


def as_env():
    """解析结果 → 环境变量字典，供子进程（含 .bat）复用。"""
    env = {
        'NAL_HOME': ROOT,
        'NAL_VENV_PY': VENV_PY,
        'NAL_OLLAMA_HOME': OLLAMA_HOME,
        'NAL_NAPCAT_HOME': NAPCAT_HOME,
        'NAL_PORT_OLLAMA': str(PORT_OLLAMA),
        'NAL_PORT_NAPCAT_UI': str(PORT_NAPCAT_UI),
        'NAL_PORT_BRIDGE_WS': str(PORT_BRIDGE_WS),
        'NAL_PORT_ADMIN': str(PORT_ADMIN),
        'NAL_SELF_ID': QQ_SELF_ID,
        'NAL_GROUP_ID': GROUP_ID,
        'NAL_ADMIN_ID': ADMIN_ID,
    }
    if OLLAMA_MODELS:
        env['OLLAMA_MODELS'] = OLLAMA_MODELS
    if not os.environ.get('OLLAMA_HOST'):
        # 拉起 `ollama serve` 前让它绑到我们算出来的端口，客户端和它才对得上
        env['OLLAMA_HOST'] = '127.0.0.1:%d' % PORT_OLLAMA
    return env


def export_env(target=None):
    """把解析结果写进当前进程环境（launcher 启动前调用一次）。"""
    env = os.environ if target is None else target
    env.update(as_env())


def describe():
    """[(名称, 路径, 是否存在), ...]，给启动横幅与自检用。"""
    items = (
        ('Python', VENV_PY),
        ('Ollama', OLLAMA_EXE),
        ('NapCat', NAPCAT_EXE),
        ('state', STATE_DIR),
    )
    return [(name, path, os.path.exists(path)) for name, path in items]
