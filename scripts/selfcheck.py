# -*- coding: utf-8 -*-
"""启动自检（调研 2026-10-22 §10.7）。

全部检查都在**启动时**跑，失败只告警不阻断（除了明确的致命项）：
  1. 对话模型可达且真能出字（api 走一次 1 token 请求；本地走 /api/tags + 一次流式）
  2. embedding 能出向量，且维度与角色目录的 `corpus_embeddings.json` 缓存一致
     （换过 embedding 模型后维度变了 → 缓存作废，重命名后自动重建）
  3. `state/` 可写
  4. 所有 `memory_card.json` 可解析（损坏的列出来，不静默）

设计原则：错误信息是**写给用户看的产品文案**，说清是哪一项、观测到什么、
该怎么修，而不是抛一段堆栈。
"""
import glob
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import paths  # noqa: E402  （路径/端口的唯一来源）
import persona  # noqa: E402  （角色资产/能力位）

OK, WARN, FAIL = 'ok', 'warn', 'fail'

# 一条 min 级别的问题清单：(level, 环节, 详情, 修法)
Result = tuple


def _res(level, item, detail, fix=''):
    return (level, item, detail, fix)


# ---- 依赖矩阵：安装项与 pip 包（每项都给出可执行的修法）----

def _pkg_version(name):
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return None


def _version_tuple(text):
    out = []
    for part in str(text).split('.'):
        digits = ''.join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def check_python_runtime():
    """当前解释器是不是项目 venv（PATH 上的 python 可能完全是另一个环境）。"""
    venv_py = paths.VENV_PY
    if not os.path.exists(venv_py):
        return _res(WARN, 'Python 解释器', '项目 venv 不存在：%s' % venv_py,
                    'py -3.13 -m venv .venv && .venv\\Scripts\\python.exe -m pip install -r requirements.txt')
    same = (os.path.normcase(os.path.abspath(sys.executable)) ==
            os.path.normcase(os.path.abspath(venv_py)))
    if not same:
        return _res(WARN, 'Python 解释器', '当前跑在 %s，不是项目 venv' % sys.executable,
                    '改用 %s 启动' % venv_py)
    return _res(OK, 'Python 解释器', '%s（%s）' % (sys.executable, sys.version.split()[0]))


def check_python_deps():
    """核心 pip 依赖。requirements.txt 锁了完整版本，这里只校验最低版本。"""
    required = (('websockets', '17.0'),)
    missing, stale = [], []
    for name, want in required:
        got = _pkg_version(name)
        if got is None:
            missing.append(name)
        elif _version_tuple(got) < _version_tuple(want):
            stale.append('%s %s<%s' % (name, got, want))
    if missing or stale:
        return _res(FAIL, 'pip 依赖', '缺失：%s%s' % (
            ', '.join(missing) or '无',
            ('；版本过低：' + ', '.join(stale)) if stale else ''),
            '%s -m pip install -r requirements.txt' % paths.VENV_PY)
    return _res(OK, 'pip 依赖', 'websockets %s' % _pkg_version('websockets'))


def check_ollama_install():
    if not os.path.exists(paths.OLLAMA_EXE):
        return _res(WARN, 'Ollama 程序', '找不到 %s' % paths.OLLAMA_EXE,
                    '安装 Ollama，或用 NAL_OLLAMA_HOME 指向安装目录')
    return _res(OK, 'Ollama 程序', paths.OLLAMA_EXE)


def check_napcat_install():
    if not os.path.exists(paths.NAPCAT_EXE):
        return _res(WARN, 'NapCat 程序', '找不到 %s' % paths.NAPCAT_EXE,
                    '安装 NapCat Shell，或用 NAL_NAPCAT_HOME 指向其目录')
    return _res(OK, 'NapCat 程序', paths.NAPCAT_HOME)


def check_personas():
    """所有角色卡是否完整；当前角色指针是否有效。"""
    ids = persona.list_ids()
    active = persona.active_id()
    bad = []
    bad_ids = set()
    for pid in ids:
        ok, why = persona.validate(pid, strict=True)
        if not ok:
            bad.append('%s（%s）' % (pid, why))
            bad_ids.add(pid)
            continue
        for problem in persona.cg_asset_problems(pid):
            bad.append('%s（%s）' % (pid, problem))
            bad_ids.add(pid)
    if bad:
        return _res(WARN, '角色资产', '%d/%d 个角色有问题：%s'
                    % (len(bad_ids), len(ids), '；'.join(bad[:3])),
                    '补齐角色卡/系统提示/数值规则；'
                    '缺资产的指令会直接失效，不会借用别的角色')
    return _res(OK, '角色资产', '%d 个可用：%s；当前 %s'
                % (len(ids), '、'.join(ids), persona.display_name(active)))


def check_dialog_model(model_key=None):
    """对话模型：能不能连上、能不能真的吐字。"""
    import rp_core as core
    kind, model_id, _ = core.resolve_model(model_key)
    if kind in ('api', 'localapi'):
        from rp_common import chat_api
        try:
            out = chat_api([{'role': 'user', 'content': 'ping'}], kind, model_id,
                           max_tokens=1, timeout=15)
        except Exception as e:
            fix = ('本地 API：检查 local_api.key / 环境变量 LOCAL_API_KEY、'
                   '服务地址（LOCAL_API_BASE_URL）与 /health 连通性'
                   if kind == 'localapi' else
                   '检查 qwen.key / 环境变量 QWEN_API_KEY 与 QWEN_BASE_URL 是否配套')
            return _res(FAIL, '对话模型', '%s 请求失败：%s' % (model_id, str(e)[:160]), fix)
        if out is None:
            return _res(WARN, '对话模型', '%s 通了但没返回内容' % model_id,
                        '多半是额度或模型名不对，跑一次真实对话确认')
        return _res(OK, '对话模型', '%s 可用' % model_id)

    # 本地 Ollama：先确认服务与模型都在，再要一个字
    base = os.environ.get('OLLAMA_HOST') or ('http://127.0.0.1:%d' % paths.PORT_OLLAMA)
    base = base.rstrip('/')
    try:
        with urllib.request.urlopen(base + '/api/tags', timeout=5) as r:
            tags = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return _res(FAIL, '对话模型', 'Ollama 不可达（%s）：%s' % (base, str(e)[:120]),
                    '先启动 Ollama（start.bat 会拉），或确认 %s serve 在跑' % paths.OLLAMA_EXE)
    names = [m.get('name') or m.get('model') or '' for m in (tags.get('models') or [])]
    if model_id and model_id not in names and (model_id + ':latest') not in names:
        return _res(FAIL, '对话模型', '本地没有模型 %s（已有：%s）'
                    % (model_id, ', '.join(names[:6]) or '无'),
                    'ollama pull %s' % model_id)
    try:
        payload = json.dumps({'model': model_id, 'messages': [{'role': 'user', 'content': 'ping'}],
                              'stream': True, 'options': {'num_predict': 1}}).encode('utf-8')
        req = urllib.request.Request(base + '/api/chat', data=payload,
                                     headers={'Content-Type': 'application/json'})
        got = b''
        with urllib.request.urlopen(req, timeout=60) as r:
            got = r.read(200)
        if not got:
            return _res(WARN, '对话模型', '%s 流式请求没有输出' % model_id,
                        '确认显存够（hw_profile 档位是否与实际显存匹配）')
        return _res(OK, '对话模型', '%s 可流式输出' % model_id)
    except Exception as e:
        return _res(WARN, '对话模型', '%s 流式请求失败：%s' % (model_id, str(e)[:160]),
                    '若只是首次加载慢，可忽略；持续失败就 ollama run %s 手工试一次' % model_id)


def check_embedding():
    """embedding 能不能出向量，维度与当前角色的语料缓存是否一致。"""
    import embedding as emb
    pid = persona.current()
    vec = emb.embed('启动自检')
    if not vec:
        return _res(WARN, 'embedding', '取不到向量（%s 不可用）' % emb.active_model(),
                    'ollama pull %s；检索会退化为近因召回，对话不受影响' % emb.EMBED_MODEL)
    dim = len(vec)
    spec = persona.corpus_spec(pid)
    if not spec:
        return _res(OK, 'embedding', '%s 出向量（%d 维），当前角色无对话语料'
                    % (emb.active_model(), dim))
    cache = spec['cache']
    if not os.path.exists(cache):
        return _res(OK, 'embedding', '%s 出向量（%d 维），语料缓存待首次构建' % (emb.active_model(), dim))
    try:
        with open(cache, encoding='utf-8') as f:
            rows = json.load(f)
        cached_dim = next((len(v) for v in rows if v), 0)
    except (OSError, ValueError) as e:
        return _res(WARN, 'embedding', '语料缓存读不了：%s' % str(e)[:100],
                    '删掉 %s，下次启动会自动重建' % os.path.relpath(cache, ROOT))
    if cached_dim and cached_dim != dim:
        bak = cache + '.dim-mismatch.bak'
        try:
            os.replace(cache, bak)
            note = '已改名为 %s，下次检索会重建' % os.path.basename(bak)
        except OSError as e:
            note = '改名失败（%s），请手动删掉该文件' % str(e)[:60]
        return _res(WARN, 'embedding', '维度不一致：当前 %d 维 vs 缓存 %d 维（换过 embedding 模型）'
                    % (dim, cached_dim), note)
    return _res(OK, 'embedding', '%s 出向量（%d 维），与缓存一致' % (emb.active_model(), dim))


def check_state_writable(state_root=None):
    """state/ 能不能写。"""
    root = state_root or os.path.join(ROOT, 'state')
    try:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, '.selfcheck_probe')
        with open(probe, 'w', encoding='utf-8') as f:
            f.write('ok')
        os.remove(probe)
        return _res(OK, 'state 目录', '%s 可写' % root)
    except OSError as e:
        return _res(FAIL, 'state 目录', '%s 写不进去：%s' % (root, str(e)[:120]),
                    '记忆与调参都落在这里，必须可写；检查磁盘权限或是否被占用')


def check_memory_files(state_root=None):
    """所有 memory_card.json（含各角色作用域）是否都能解析。损坏的列出来。"""
    root = state_root or os.path.join(ROOT, 'state')
    paths = glob.glob(os.path.join(root, 'personas', '*', 'memory_card.json'))
    paths += glob.glob(os.path.join(root, 'personas', '*', 'users', '*',
                                    'memory_card.json'))
    bad = []
    for p in paths:
        try:
            with open(p, encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict) or 'card' not in data:
                bad.append((os.path.relpath(p, root), '结构不对（缺 card）'))
        except (OSError, ValueError) as e:
            bad.append((os.path.relpath(p, root), str(e)[:60]))
    if bad:
        return _res(WARN, '记忆文件', '%d/%d 份有问题：%s'
                    % (len(bad), len(paths), '；'.join('%s（%s）' % b for b in bad[:3])),
                    '损坏的可以从 state/backups/ 里最近的备份还原，或直接删掉让它重建')
    return _res(OK, '记忆文件', '%d 份均可解析' % len(paths))


def run_all(model_key=None, state_root=None):
    """跑全部检查，返回 [(level, item, detail, fix), ...]。"""
    checks = (
        check_python_runtime,
        check_python_deps,
        check_personas,
        check_ollama_install,
        check_napcat_install,
        lambda: check_dialog_model(model_key),
        check_embedding,
        lambda: check_state_writable(state_root),
        lambda: check_memory_files(state_root),
    )
    out = []
    for fn in checks:
        try:
            out.append(fn())
        except Exception as e:                     # 单项炸了不影响其他项
            out.append(_res(WARN, '自检异常', '%s：%s' % (getattr(fn, '__name__', '?'),
                                                        str(e)[:120]), ''))
    return out


def worst(results):
    levels = [r[0] for r in results]
    if FAIL in levels:
        return FAIL
    if WARN in levels:
        return WARN
    return OK


def main(argv=None):
    """命令行入口：`python scripts/selfcheck.py` 打印全部检查项。

    桥接启动时也调 run_all()，这里只是给「不启动机器人先看一眼」留个入口。
    """
    import argparse
    ap = argparse.ArgumentParser(description='启动自检（不启动机器人）')
    ap.add_argument('--model', help='只测这个对话模型，默认按 runtime_config 里选的')
    args = ap.parse_args(argv)
    results = run_all(args.model)
    mark = {OK: '[OK]  ', WARN: '[警告]', FAIL: '[失败]'}
    for level, item, detail, fix in results:
        print('%s %s：%s' % (mark.get(level, level), item, detail))
        if fix:
            print('       修法：%s' % fix)
    level = worst(results)
    if level == OK:
        print('\n自检通过。')
        return 0
    if level == WARN:
        print('\n自检有警告，不影响启动，但建议按上面的修法处理。')
        return 0
    print('\n自检有失败项，先修掉再启动。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
