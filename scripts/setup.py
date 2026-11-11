# -*- coding: utf-8 -*-
"""首次运行设置：问几句，把绑定写进 state/setup.json。

start.bat 在 state/setup.json 不存在时先跑本脚本，之后每次启动都跳过。
答案全部落在本地文件，不进 git（state/ 已 gitignore）。

写下的字段（paths.py / rp_config.py 读取，环境变量始终优先）：

    self_qq   bot 自己登录的 QQ（NapCat 扫码的那个号）
    admin_qq  管理员 QQ，能发 /status 等指令；留空 = 不设管理员
    group_id  只守的群；留空 = 加进哪个群就服务哪个群
    base_url  OpenAI 兼容端点（本机 Ollama 是 http://127.0.0.1:<端口>/v1）
    model     模型名，取端点 /v1/models 里那串

API key 不写进 setup.json：写了就落到项目根的 local_api.key（已 gitignore）。

用法：
    python scripts/setup.py            # 交互设置（已有 setup.json 会先提示）
    python scripts/setup.py --force    # 覆盖已有设置
    python scripts/setup.py --check    # 只打印当前绑定与连通性
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import paths  # noqa: E402

SETUP_PATH = os.path.join(paths.STATE_DIR, 'setup.json')
KEY_PATH = os.path.join(paths.ROOT, 'local_api.key')
DEFAULT_BASE = 'http://127.0.0.1:%d/v1' % paths.PORT_OLLAMA


def _ask(prompt, default=''):
    """问一句；回车用默认值。EOF（管道输入结束）也返回默认值，不炸。"""
    tail = ' [%s]' % default if default else ''
    try:
        raw = input('%s%s: ' % (prompt, tail))
    except EOFError:
        # 没有可读输入（被管道/SCHEDULE 拉起、或 stdin 被关掉）：直接退出，
        # 不能拿默认值无限重问必填项。
        print()
        raise SystemExit('没有读到输入，已取消。双击 start.bat 再试一次。')
    return raw.strip() or default


def _ask_qq(prompt, default='', required=False):
    while True:
        val = _ask(prompt, default)
        if not val:
            if required:
                print('  → 这个必须填。')
                continue
            return ''
        if val.isdigit() and 5 <= len(val) <= 12:
            return val
        print('  → 填数字 QQ 号。')


def _ask_url(prompt, default):
    while True:
        val = _ask(prompt, default).rstrip('/')
        if val.startswith(('http://', 'https://')):
            return val
        print('  → 要 http:// 或 https:// 开头的完整地址。')


def _normalize_base(url):
    """用户可能填端点根、也可能填到 /v1，统一成以 /v1 结尾。"""
    url = url.rstrip('/')
    return url if url.endswith('/v1') else url + '/v1'


def _probe(base_url, model, key):
    """问一下端点 /models，顺便确认模型名在不在列表里。返回 (ok, 说明)。"""
    req = urllib.request.Request(base_url.rstrip('/') + '/models')
    if key:
        req.add_header('Authorization', 'Bearer ' + key)
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode('utf-8', 'replace') or '{}')
    except urllib.error.HTTPError as e:
        return False, '端点返回 HTTP %s' % e.code
    except Exception as e:  # 连不上、超时、不是 JSON
        return False, '连不上端点（%s）' % e
    ids = [str(m.get('id') or '') for m in (data.get('data') or [])
           if isinstance(m, dict)]
    if not ids:
        return True, '端点通了，但 /models 没返回列表（不影响对话）'
    if model in ids:
        return True, '端点通了，模型 %s 在列表里' % model
    return True, '端点通了，但列表里没有 %s；可选的第一个是 %s' % (model, ids[0])


def _load():
    try:
        with open(SETUP_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data):
    os.makedirs(paths.STATE_DIR, exist_ok=True)
    tmp = SETUP_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, SETUP_PATH)


def _key_exists():
    try:
        return bool(open(KEY_PATH, encoding='utf-8').read().strip())
    except OSError:
        return False


def show():
    """打印当前绑定（不打印 key 本身）。"""
    data = _load()
    if not data:
        print('还没有 state/setup.json —— 跑一次 python scripts/setup.py。')
        return 1
    print('state/setup.json：')
    for label, key in (('机器人 QQ', 'self_qq'), ('管理员 QQ', 'admin_qq'),
                       ('只守的群', 'group_id'), ('模型端点', 'base_url'),
                       ('模型名', 'model')):
        val = data.get(key) or '（空）'
        print('  %-9s %s' % (label, val))
    print('  %-9s %s' % ('API key', 'local_api.key 已写' if _key_exists() else '没设'))
    return 0


def check():
    """打印绑定 + 连一次端点。"""
    data = _load()
    if show() or not data:
        return 1
    key = ''
    try:
        key = open(KEY_PATH, encoding='utf-8').read().strip()
    except OSError:
        pass
    base = str(data.get('base_url') or '')
    if not base:
        print('没配端点，跳过连通性检查。')
        return 1
    started = time.time()
    ok, msg = _probe(base, str(data.get('model') or ''), key)
    print('%s %s（%.1fs）' % ('[OK]' if ok else '[FAIL]', msg, time.time() - started))
    return 0 if ok else 1


def _ask_install_ollama():
    """没装 Ollama 时问一次要不要现在装、装到哪。

    不静默下载：默认答案是否，目录也由用户定，回车才用推荐目录。
    返回装好的目录（写进 setup.json 的 ollama_home），跳过时返回空串。
    """
    import install_ollama as inst
    ok, exe, _why = inst.check()
    if ok:
        print()
        print('Ollama 已经就位（%s）。' % exe)
        return ''
    print()
    print('本机没找到 Ollama。本地跑模型要用它，也可以跳过，改成接自己的端点。')
    ans = _ask('现在把 Ollama 下载到本机吗（约 1.5 GB，回车跳过，输入 y 安装）').strip().lower()
    if ans not in ('y', 'yes'):
        print('  → 跳过。以后想装就运行 python scripts\\install_ollama.py')
        return ''
    default = inst.default_home()
    while True:
        target = _ask('装到哪个目录（路径里不要有 -）', default).strip()
        ok, reason = inst.check_target(target)
        if ok:
            break
        print('  → %s' % reason)
        default = ''    # 被否掉的默认值不再端上来，免得一路回车原地打转
    print('  开始下载，中断了可以再跑一次 python scripts\\install_ollama.py')
    try:
        exe = inst.install(target)
    except inst.InstallError as e:
        print('  → 装失败了：%s' % e)
        print('     先跳过，之后可以再跑 python scripts\\install_ollama.py')
        return ''
    print('  → 装好了：%s' % exe)
    return target


def run(force=False):
    old = _load()
    if old and not force:
        print('已经有 state/setup.json，想改就删掉它或加 --force 再跑。以下是现状：\n')
        show()
        return 0

    print('=' * 58)
    print('  首次运行设置：问四件事，答案只留在本机（state/setup.json）')
    print('  回车 = 用中括号里的默认值；以后想改，重跑本脚本加 --force')
    print('=' * 58)
    print()

    data = {}
    data['self_qq'] = _ask_qq('1/4 机器人 QQ（NapCat 登录的那个号）',
                              str(old.get('self_qq') or ''), required=True)
    data['admin_qq'] = _ask_qq('2/4 管理员 QQ（能发管理指令，可留空）',
                               str(old.get('admin_qq') or ''))
    data['group_id'] = _ask_qq('3/4 只守的群号（可留空 = 加进哪个群就服务哪个群）',
                               str(old.get('group_id') or ''))
    data['base_url'] = _normalize_base(
        _ask_url('4/4 模型端点（OpenAI 兼容；本机 Ollama 回车即可）',
                 str(old.get('base_url') or DEFAULT_BASE)))
    data['model'] = _ask('   模型名（填端点里已有的模型）',
                         str(old.get('model') or ''))
    if not data['model']:
        data['model'] = 'qwen-vl'
        print('  → 没填，先记成 %s，之后在面板里改。' % data['model'])

    key = _ask('   API key（本机 Ollama 一般不用；写进 local_api.key，可留空）')
    if key:
        with open(KEY_PATH, 'w', encoding='utf-8') as f:
            f.write(key.strip() + '\n')
        print('  → 已写入 %s（已 gitignore，不会进仓库）' % KEY_PATH)
    else:
        key = ''
        try:
            key = open(KEY_PATH, encoding='utf-8').read().strip()
        except OSError:
            pass

    home = _ask_install_ollama()
    if home:
        data['ollama_home'] = home

    _save(data)
    print()
    print('已写入 %s' % SETUP_PATH)

    print()
    print('连一下端点看看：')
    ok, msg = _probe(data['base_url'], data['model'], key)
    print('  %s %s' % ('[OK]' if ok else '[WARN]', msg))
    if not ok:
        print('  连接有问题不影响继续；先把服务起起来，或改 state/setup.json 里的端点。')

    print()
    print('下一步：双击 start.bat（它会拉起 Ollama / NapCat / bot / 管理面板）。')
    print('NapCat 首次登录要扫码；登录后在它的 WebUI 里加一个 WebSocket 客户端：')
    print('  ws://127.0.0.1:%d/onebot/v11/ws' % paths.PORT_BRIDGE_WS)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='首次运行设置：写 state/setup.json')
    ap.add_argument('--force', action='store_true', help='覆盖已有设置')
    ap.add_argument('--check', action='store_true', help='只打印绑定与连通性')
    args = ap.parse_args(argv)
    if args.check:
        return check()
    return run(force=args.force)


if __name__ == '__main__':
    sys.exit(main())
