# -*- coding: utf-8 -*-
"""面板与配置层的一致性自检（不联网、不写盘）。

这里要防的是一类具体的事故：后端改了配置项或加了接口，面板还按老样子渲染。
面板 100% 由 `/api/schema` 驱动，所以它落后于后端时**不会有任何报错**——
只是少几个开关、模型选项停在旧枚举上。跑通全部单测也发现不了，
因为自测都在 scripts 层，没有一个断言摸过 admin_ui。

于是把「面板说的 / 后端有的」这层契约写成检查：

  1. 面板里 fetch 过的每个 /api 路径，admin_server 确实有这条路由
  2. 面板里 `$('id')` 引用的每个元素，HTML 里真的有这个 id（含 tab-*）
  3. 每个 data-tab 都有对应的 tab 面板与切换列表条目
  4. runtime_config.SCHEMA 的每个字段在面板里都有承载控件：
     未 hidden 的走通用渲染（类型必须可渲染），
     hidden 的必须声明 ui，且那种 UI 与那个键真的出现在面板/后端载荷里
  5. 面板里以名字写入的配置键必须还在 SCHEMA 里（删字段时面板要跟着删）
  6. 角色卡页的发言决策字段与 persona.GATE_TUNING_RANGES 一一对应
  7. 提供商只有一份来源：下拉由 admin_payloads 算出来（只含本机配好的），
     面板不许出现提供商名称，目录里的每个提供商 rp_core 都要认得
  8. 面板与配置层不出现未登记端点、本机绝对路径或额外内部字样（额外词由本机环境变量提供）

用法：
    python scripts/panel_check.py            # 全跑，有问题退出码 1
    python scripts/panel_check.py --quiet     # 只打印问题
"""
import argparse
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PANEL = os.path.join(ROOT, 'admin_ui', 'index.html')
SERVER = os.path.join(HERE, 'admin_server.py')
PAYLOADS = os.path.join(HERE, 'admin_payloads.py')
CORE = os.path.join(HERE, 'rp_core.py')

# 通用渲染能画出来的类型（renderField 里的分支）
_RENDERABLE = ('bool', 'int', 'float', 'str', 'list')
# hidden 字段允许的自定义 UI → 面板里必须有那个函数的名字
_HIDDEN_UI = {'model': 'modelCard(', 'slider': 'gearSlider(',
              'switch': 'fillCharSwitch('}
# 面板/配置层只应出现公网服务与本地回环；这里按“效果”检查，不在公开仓库里
# 维护内部服务名清单（清单本身也会泄露内部称呼）。需要额外的本机检查时，
# 设 NAL_PANEL_FORBIDDEN（逗号分隔），只在当前机器生效。
_PUBLIC_HOSTS = frozenset((
    'dashscope.aliyuncs.com', 'api.deepseek.com', 'api.siliconflow.cn',
    'api.moonshot.cn', 'open.bigmodel.cn', '127.0.0.1', 'localhost',
))
_EXTRA_FORBIDDEN = tuple(
    w.strip().lower() for w in os.environ.get('NAL_PANEL_FORBIDDEN', '').split(',')
    if w.strip())
_ABS_PATH_RE = re.compile(r'(?i)\b[a-z]:\\')


def _read(path):
    with io.open(path, encoding='utf-8') as f:
        return f.read()


def _api_paths(text):
    """面板里请求过的 /api 路径（去掉 query）。"""
    out = set()
    for m in re.finditer(r"fetch\(\s*[`'\"](/api/[^`'\"]*)", text):
        out.add(m.group(1).split('?')[0].rstrip('/'))
    return out


def _server_routes(text):
    """admin_server 里出现过的 /api 路径字面量。"""
    return {m.group(1).rstrip('/') for m in
            re.finditer(r"'(/api/[a-z0-9\-/]+)'", text)}


def _panel_ids(text):
    return {m.group(1) for m in re.finditer(r'id="([A-Za-z0-9_\-]+)"', text)}


def _dynamic_ids(text):
    """JS 里 `el.id = 'x'` 动态命名的元素（同样算存在）。"""
    return {m.group(1) for m in re.finditer(r"\.id = '([A-Za-z0-9_\-]+)'", text)}


def _id_refs(text):
    refs = set()
    for pat in (r"\$\('([A-Za-z0-9_\-]+)'\)", r"getElementById\('([A-Za-z0-9_\-]+)'\)"):
        refs |= {m.group(1) for m in re.finditer(pat, text)}
    return refs


def check_routes(panel, server):
    routes = _server_routes(server)
    missing = sorted(p for p in _api_paths(panel) if p not in routes)
    return ['面板请求 %s，但 admin_server 没有这条路由' % p for p in missing]


def check_element_ids(panel):
    known = _panel_ids(panel) | _dynamic_ids(panel)
    missing = sorted(_id_refs(panel) - known)
    return ['面板引用了不存在的元素 id：%s' % i for i in missing]


def check_tabs(panel):
    problems = []
    tabs = sorted({m.group(1) for m in re.finditer(r'data-tab="([a-z_]+)"', panel)})
    if not tabs:
        return ['面板里找不到任何 data-tab']
    for t in tabs:
        if 'id="tab-%s"' % t not in panel:
            problems.append('标签页 %s 没有对应的 <div id="tab-%s">' % (t, t))
    lists = [m.group(1) for m in
             re.finditer(r"\[([^\]]*)\]\.forEach\(k => \$\('tab-' \+ k\)", panel)]
    if not lists:
        problems.append('面板里没有切换标签页时统一显隐的那份清单（渲染逻辑脱钩）')
    for body in lists:
        listed = sorted(re.findall(r"'([a-z_]+)'", body))
        if listed != tabs:
            problems.append('标签页清单 %s 与实际 data-tab %s 不一致'
                            % (','.join(listed), ','.join(tabs)))
    return problems


def _hidden_field_problems(panel, payloads, schema):
    problems = []
    for spec in schema:
        key, ui = spec['key'], spec.get('ui')
        marker = _HIDDEN_UI.get(ui or '')
        if not marker:
            problems.append('配置项 %s 标了 hidden 但没有可渲染它的自定义 UI'
                            '（ui 需是 %s）' % (key, ' / '.join(sorted(_HIDDEN_UI))))
            continue
        if marker not in panel:
            problems.append('配置项 %s 要求 %s 型控件，面板里没有对应实现（%s）'
                            % (key, ui, marker.rstrip('(')))
        if key not in panel and "'%s'" % key not in payloads:
            problems.append('配置项 %s 后端有定义，但既没进 /api/schema 的模型卡，'
                            '面板里也找不到它' % key)
    return problems


def _visible_field_problems(schema):
    problems = []
    for spec in schema:
        key, typ = spec['key'], spec.get('type')
        if typ not in _RENDERABLE:
            problems.append('配置项 %s 的类型 %s 面板画不出来' % (key, typ))
        choices = spec.get('choices') or []
        labels = spec.get('labels') or {}
        if choices and labels and not set(choices) <= set(labels):
            problems.append('配置项 %s 的 labels 少了：%s'
                            % (key, '、'.join(sorted(set(choices) - set(labels)))))
        if not choices and labels:
            problems.append('配置项 %s 有 labels 却没有 choices' % key)
        if typ in ('int', 'float') and 'min' in spec and 'max' in spec:
            if spec['min'] > spec['max']:
                problems.append('配置项 %s 的 min 大于 max' % key)
    return problems


def check_schema(panel, payloads, schema):
    problems = _visible_field_problems(schema)
    problems += _hidden_field_problems(panel, payloads,
                                       [s for s in schema if s.get('hidden')])
    keys = {s['key'] for s in schema}
    for key in sorted({m.group(1) for m in
                       re.finditer(r"save\('([a-z_0-9]+)'", panel)}):
        if key not in keys:
            problems.append('面板保存 %s，但 runtime_config.SCHEMA 已经没有这一项' % key)
    return problems


def check_gate_fields(persona, payloads_text):
    block = re.search(r"_GATE_FIELDS = \{(.*?)\n\}", payloads_text, re.S)
    if not block:
        return ['admin_payloads 里没有 _GATE_FIELDS（角色卡页的发言决策字段表）']
    listed = set(re.findall(r"^\s+'([a-z_0-9]+)':", block.group(1), re.M))
    want = set(persona.GATE_TUNING_RANGES)
    problems = []
    if want - listed:
        problems.append('角色卡页缺字段定义：%s' % '、'.join(sorted(want - listed)))
    if listed - want:
        problems.append('角色卡页有已不存在的字段：%s' % '、'.join(sorted(listed - want)))
    return problems


def check_providers(panel, payloads_text, core_text):
    """提供商只有一份来源：后端算出可用列表，面板照着渲染，不许自己留清单。"""
    labels = re.search(r"_CATALOG_LABELS = \{([^}]*)\}", payloads_text, re.S)
    if not labels:
        return ['admin_payloads 里没有 _CATALOG_LABELS']
    providers = set(re.findall(r"'([a-z_]+)':", labels.group(1)))
    body = re.search(r"def _providers\(.*?(?=\ndef |\Z)", payloads_text, re.S)
    if not body:
        return ['admin_payloads 里没有 _providers（提供商下拉不再是后端算出来的）']

    problems = []
    if "'providers': _providers(" not in payloads_text:
        problems.append('模型卡载荷没带 providers，面板拿不到提供商列表')
    if 'b.providers' not in panel:
        problems.append('面板的提供商下拉不读 b.providers，八成是写回硬编码清单了')
    for name in re.findall(r"': '([^']*)'", labels.group(1)):
        if name in panel:
            problems.append('提供商名称「%s」又出现在面板里（只该在后端目录）' % name)
    listed = set(re.findall(r"^\s+'([a-z_]+)':", body.group(0), re.M))
    if not listed:
        problems.append('_providers 里没有可用性判据，下拉会是空的')
    for pid in sorted(listed - providers):
        problems.append('_providers 判定提供商 %s 可用性，但它不在 _CATALOG_LABELS 里'
                        '（面板选得到，/api/model-catalog 会报未知提供商）' % pid)
    for pid in sorted(providers - listed):
        problems.append('提供商 %s 没有可用性判据，面板下拉里永远不会出现' % pid)
    for p in sorted(providers):
        if "'%s'" % p not in core_text:
            problems.append('rp_core 不认得提供商 %s（模型键解析/拼装缺分支）' % p)
    return problems


def _bad_urls(text):
    """返回 (位置, 主机) —— 不在 _PUBLIC_HOSTS 里的 http(s) 端点。"""
    out = []
    for m in re.finditer(r'https?://([^/\s"\'`)]+)', text):
        host = m.group(1).rsplit('@', 1)[-1].split(':', 1)[0].lower()
        if host not in _PUBLIC_HOSTS:
            out.append((m.start(), host))
    return out


def check_leaks(named):
    problems = []
    for name, text in named.items():
        low = text.lower()
        for m in _ABS_PATH_RE.finditer(text):
            line = text[:m.start()].count('\n') + 1
            problems.append('%s:%d 出现本机绝对路径' % (name, line))
        for pos, host in _bad_urls(text):
            line = text[:pos].count('\n') + 1
            problems.append('%s:%d 出现未登记端点 %s' % (name, line, host))
        for token in _EXTRA_FORBIDDEN:
            pos = low.find(token)
            if pos >= 0:
                line = text[:pos].count('\n') + 1
                problems.append('%s:%d 出现内部字样「%s」' % (name, line, token))
    return problems


def run_all():
    panel = _read(PANEL)
    server = _read(SERVER)
    payloads = _read(PAYLOADS)
    core = _read(CORE)
    import persona
    import runtime_config as rc
    checks = [
        ('路由', lambda: check_routes(panel, server)),
        ('元素', lambda: check_element_ids(panel)),
        ('标签页', lambda: check_tabs(panel)),
        ('配置项', lambda: check_schema(panel, payloads, rc.SCHEMA)),
        ('发言决策', lambda: check_gate_fields(persona, payloads)),
        ('模型提供商', lambda: check_providers(panel, payloads, core)),
        ('内部字样', lambda: check_leaks({'admin_ui/index.html': panel,
                                          'scripts/admin_payloads.py': payloads,
                                          'scripts/runtime_config.py':
                                              _read(os.path.join(HERE, 'runtime_config.py'))})),
    ]
    problems = []
    for name, fn in checks:
        found = fn()
        problems += ['[%s] %s' % (name, p) for p in found]
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description='面板与配置层一致性自检')
    ap.add_argument('--quiet', action='store_true', help='只打印问题')
    args = ap.parse_args(argv)
    try:
        problems = run_all()
    except Exception as e:      # 检查脚本自己坏了也要让人看见
        print('panel_check 运行失败：%s' % e)
        return 1
    for p in problems:
        print(p)
    if problems:
        print('panel_check：%d 项不一致' % len(problems))
        return 1
    if not args.quiet:
        print('panel_check：面板与配置层一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())
