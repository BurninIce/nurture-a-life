# -*- coding: utf-8 -*-
"""
RP 管道共享工具（rp_eval.py / rp_gui.py 共用，勿在模块内包装 stdout）。

- clean_output：RP 输出清洗（多轮幻觉截断）——调制链收敛项，勿改截断规则
- strip_cues / strip_actions：括号提示白名单（合法 cue 只认英文方括号）
- retrieval_query：检索 query = 她上一句 + 用户这句
- stream_chat：Ollama /api/chat 流式调用，逐块 yield 累积文本

注意：Python 3.6 兼容（rp_eval.py 跑在 3.6.8；rp_gui.py 跑在 3.13），
不要用 walrus/类型标注等 3.7+ 语法。
"""
import json
import os
import re
import urllib.error
import urllib.request

import paths

OLLAMA_URL = 'http://127.0.0.1:%d/api/chat' % paths.PORT_OLLAMA


def read_key_file(name):
    """项目根下的 key 文件（环境变量没设时的备选）。文件已 gitignore，不入库。"""
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            name)
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def _post_json(req, timeout, model='', key_env='', key_file='qwen.key'):
    """发请求并解析 JSON。401/429 转成可操作的错误——否则上层只会看到 "HTTP Error 401"。"""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', 'replace')[:240]
        except Exception:
            pass
        if e.code == 401:
            if key_file != 'qwen.key':
                raise RuntimeError(
                    'API key 无效（401）。检查环境变量 %s 或 %s；'
                    '403 还可能是 key 被停用/过期或来源 IP 不在放行网段。响应：%s'
                    % (key_env or 'LOCAL_API_KEY', key_file, body))
            raise RuntimeError(
                'API key 无效（401）。检查环境变量 %s 或 qwen.key，'
                '并确认 key 与 QWEN_BASE_URL 属于同一个服务。响应：%s'
                % (key_env or 'QWEN_API_KEY', body))
        if e.code == 403:
            raise RuntimeError('访问被拒（403）：key 可能被停用/过期，'
                               '或来源 IP 不在放行网段。响应：' + body)
        if e.code == 429:
            raise RuntimeError('限流（429）：把并发降到 2，或按 Retry-After 等待。响应：' + body)
        raise RuntimeError('HTTP %d（%s）：%s' % (e.code, model, body))


# 注入模板文件（程序外内容，rp_gui/memory_card/corpus_rag 共用）
TEMPLATES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'prompts', 'injection_templates.md')


def load_templates():
    """从 prompts/injection_templates.md 读取注入模板，返回 dict。

    格式：`KEY: value`（值可含占位符 {x}）。失败返回空 dict（调用方兜底）。
    """
    tpl = {}
    try:
        with open(TEMPLATES_PATH, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or ':' not in line:
                    continue
                key, _, val = line.partition(':')
                key = key.strip()
                val = val.strip()
                if key and val:
                    tpl[key] = val
    except OSError:
        pass
    return tpl


# ---------- 语气提示（cue）白名单 ----------
# 合法 cue 只认英文方括号（[shyly]）：中文方括号是模型自造的旁白/标注，
# 一律删掉而不是从那里截断整条回复
# ——旧实现 text.find('[') 会把「嗯[轻笑]说得好」后面的半句全丢掉。
# 短方括号括注（[轻笑]/[旁白]）：删掉本身，保留后文
_SHORT_BRACKET_MAX = 24
_CLOSED_BRACKET_RE = re.compile(r'\[([^\[\]]{0,200})\]')
# 群聊里的动作括注：（轻笑）（歪头）这类短纯中文括注
_ACTION_RE = re.compile(r'[（(]([^\s（()）。，！？…、~～]{1,6})[）)]')
# TTS 输入要剥掉的所有括号括注（动作/旁白），成对才算
_PAREN_RE = re.compile(r'[（(][^（()）]*[）)]')
# 回复开头照抄了对话记录里的时间戳（[11-10 18:50]）——非台词，剥掉
_LEAD_STAMP_RE = re.compile(r'^\s*\[\d{1,2}-\d{1,2} \d{1,2}:\d{2}\]\s*')


def strip_cues(text):
    """剥掉方括号提示/旁白，返回展示文本。

    - `[shyly]` 英文方括号 → 删掉
    - `[轻笑]` 短中文括注   → 直接删除，**不再从该处截断整条回复**
    - `[时间就像…]` 长括注 / 未闭合 `[` → 沿用老行为截断（防复读旁白）
    """
    if not text or '[' not in text:
        return text
    out = []
    pos = 0
    while True:
        m = _CLOSED_BRACKET_RE.search(text, pos)
        if not m:
            rest = text[pos:]
            # 未闭合的 [：短尾巴截掉（老行为），长尾巴当正文保留
            idx = rest.find('[')
            if idx >= 0 and len(rest) - idx < 80:
                rest = rest[:idx]
            out.append(rest)
            break
        inner = m.group(1)
        if len(inner) > _SHORT_BRACKET_MAX:
            # 长括注 = 模型开始写旁白/复读，从这里截断
            out.append(text[pos:m.start()])
            break
        out.append(text[pos:m.start()])
        pos = m.end()
    return ''.join(out)


def strip_actions(text):
    """群聊用：删掉（轻笑）（歪头）这类**短纯中文**动作括注。

    只针对 1–6 字、无标点的括注——`（其实我也不确定，大概是吧）` 这种正文
    括号不动，避免把正常内容吃掉。私聊不调用（私聊保留动作描写）。
    """
    if not text:
        return text
    return _ACTION_RE.sub('', text)


def strip_for_tts(text):
    """TTS 输入只留台词：删掉（动作/旁白）括注。

    展示文本保留动作描写，但念出来会变成「括号里的话」——（轻轻歪头）、
    （其实我也不确定）都不该出声。嵌套括号由内向外逐层剥；
    未闭合的括号不动（宁可漏删，不能把后面的台词一起吃掉）。
    """
    out = text or ''
    while True:
        new = _PAREN_RE.sub('', out)
        if new == out:
            return out
        out = new


def retrieval_query(previous, text, prev_chars=200):
    """检索用 query：**她上一句 + 这句用户发言**。

    「那次挺好玩的」单独去 embed 就是噪声；带上她上一句「你说的是夏日祭那次？」
    才查得到东西。上一句只取尾部——离当前这句最近的半句才承载指代。
    记忆检索与语料 RAG 共用这一份 query。
    """
    prev = (previous or '').strip()
    cur = (text or '').strip()
    if prev:
        prev = prev[-prev_chars:]
        return (prev + '\n' + cur) if cur else prev
    return cur


def clean_output(text, *, strip_action=False, keep_terms=None,
                 drop_prefixes=(), self_prefixes=()):
    """清洗 RP 模型输出：截断复读的元注释/自编多轮对话/残留标签。

    strip_action: 群聊传 True——额外剥掉（轻笑）这类短动作括注。
    keep_terms: 术语正则（lore_rag.term_pattern）——模型会用【术语】给能力名加括号，
                不先还原成「术语」就会被下面的元注释截断从句子中间砍掉。
    drop_prefixes: 开头若原样复读了注入块标题（【Canon notes】等），先剥掉再清洗。
    self_prefixes: 她自己的名字前缀（「角色名：」）。对话记录里别人的行都带
                「昵称：」，模型会照格式给自己也加上；这类自报家门不是台词，硬剥。
    """
    head = text.lstrip() if text else text
    if head:
        head = _LEAD_STAMP_RE.sub('', head)
    prefixes = tuple(drop_prefixes or ()) + tuple(self_prefixes or ())
    for _ in range(3):
        hit = next((p for p in prefixes if p and head.startswith(p)), '')
        if not hit:
            break
        head = head[len(hit):].lstrip()
    text = head
    if keep_terms:
        # 括号里可能还挂着外文名（【术语（Latin）】），术语后的短尾巴一起吃掉
        text = re.sub(r'【(%s)[^】]{0,24}】' % keep_terms, r'「\1」', text)
    # 截断到第一个【（元注释复读）
    idx = text.find('【')
    if idx > 0:
        text = text[:idx]
    # 截断自编多轮对话（（角色A）…（角色B）… 等对话者标签）
    idx = re.search(r'[（(](?:角色A|角色B|角色C|角色D)[）)]', text)
    if idx:
        text = text[:idx.start()]
    # 方括号：英文 cue 提取、短中文括注删除、长括注/未闭合截断
    text = strip_cues(text)
    # 截断残留的 im_start 标签
    idx = text.find('<|im_start|>')
    if idx > 0:
        text = text[:idx]
    # 去残留标签
    text = re.sub(r'<\|[^|]*\|>', '', text)
    text = re.sub(r'<\|im_(start|end)\|[^>]*>', '', text)
    if strip_action:
        text = strip_actions(text)
    return text.strip()


def stream_chat(model, messages, options=None, url=OLLAMA_URL, timeout=300, think=None):
    """调用 Ollama /api/chat 流式接口，逐块 yield 累积文本。

    model: Ollama 模型名（如 'qwen3.5-9b:latest'）
    messages: [{'role': 'system'|'user'|'assistant', 'content': ...}, ...]
    options: 传给 options 的参数字典（temperature/top_p/repeat_penalty/num_predict）
    think: qwen3.5 等思考型模型传 think=False 关闭思考链（Ollama 0.32+ 支持；未传不带字段）
    """
    payload = {
        'model': model,
        'messages': messages,
        'stream': True,
        'options': options or {},
    }
    if think is not None:
        payload['think'] = think
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    resp = urllib.request.urlopen(req, timeout=timeout)
    text = ''
    for line in resp:
        line = line.decode('utf-8', errors='replace').strip()
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except ValueError:
            continue
        if chunk.get('message', {}).get('content'):
            text += chunk['message']['content']
            yield text
        if chunk.get('done'):
            break


def chat_qwen(messages, model, base_url=None, api_key=None, temperature=0.7,
              max_tokens=None, timeout=None, enable_search=False, thinking=None,
              profile=None):
    """调用千问（DashScope）非流式接口，返回完整文本。

    messages: [{'role': 'system'|'user'|'assistant', 'content': ...}, ...]
    model: 千问模型 ID（如 qwen3.8-max / qwen3.6-flash / qwen-plus）
    base_url/api_key/timeout: 默认取 rp_config 常量；api_key 默认取环境变量 QWEN_API_KEY
    enable_search: 联网搜索。compatible-mode 支持把 enable_search 放在请求顶层
        （2026-10-20 实测有效）。**不要**改走原生端点：/api/v1/services/aigc/...
        在当前 PAYG 域名下返回 400 url error，联网会静默失败、降级成模型瞎编。
    thinking: None=沿用 QWEN_DISABLE_THINKING（默认关）；'off'/'low'/'medium'/'high'
        =思考挡位（越高思维链越长，输出预算会被抬到 API_THINKING_MIN_TOKENS 以上）；
        True/False 兼容旧调用（= 'high' / 'off'）。
    profile: rp_config.QWEN_PROFILES 里的档位名，端点与 key 成对取；
        None=全局 api_profile（识图/日常/搜索等旁路调用用）。
    """
    from rp_config import (QWEN_TIMEOUT, QWEN_API_KEY_FILE,
                           QWEN_DISABLE_THINKING, QWEN_PROFILES, active_endpoint)
    prof = str(profile or '').strip().lower()
    if prof:
        spec = QWEN_PROFILES.get(prof)
        if not spec:
            raise ValueError('未知档位：%s（可选：%s）'
                             % (profile, ' / '.join(QWEN_PROFILES)))
        base_url = base_url or spec['base_url']
        key_env = spec['key_env']
    else:
        _url, key_env = active_endpoint()
        base_url = base_url or _url
    api_key = api_key or os.environ.get(key_env, '') \
        or read_key_file(QWEN_API_KEY_FILE)
    timeout = timeout or QWEN_TIMEOUT
    headers = {'Content-Type': 'application/json',
               'Authorization': 'Bearer ' + api_key}
    payload = {
        'model': model,
        'messages': messages,
        'temperature': temperature,
    }
    level = _thinking_level(thinking)
    if level and level != 'off':
        max_tokens = _thinking_budget(max_tokens)
    if max_tokens:
        payload['max_tokens'] = int(max_tokens)
    if enable_search:
        # 顶层参数即可触发联网检索；不换端点，跟着 QWEN_BASE_URL 走
        payload['enable_search'] = True
    if level and level != 'off':
        payload['reasoning_effort'] = level
    elif level == 'off' or QWEN_DISABLE_THINKING:
        # 关掉走 enable_thinking（文档明确支持）；默认值沿用 QWEN_DISABLE_THINKING
        payload['enable_thinking'] = False
    url = base_url.rstrip('/') + '/chat/completions'
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                 headers=headers)
    resp = _post_json(req, timeout, model, key_env)
    return resp['choices'][0]['message']['content'].strip()


def chat_localapi(messages, model, base_url=None, api_key=None, temperature=0.7,
                  max_tokens=None, timeout=None, reasoning_effort=None,
                  thinking=None):
    """调用本地部署的 OpenAI 兼容服务，返回完整文本。

    与 chat_qwen 的差异（都是接入文档实测的坑）：
    - 关思考链用 ``reasoning_effort='none'``；``enable_thinking=false`` 在这里无效。
    - 思考没关时 max_tokens 太小会返回空正文；本函数默认关思考，短输出安全。
    - HTTP 明文，地址由部署方配置，不经过公网。
    thinking: None=按 reasoning_effort 默认（关）；'off'/'low'/'medium'/'high'
        =思考挡位（'off' 发 reasoning_effort='none'，其余直接透传）；True/False
        兼容旧调用（= 'high' / 'off'）。
    """
    from rp_config import (LOCAL_API_BASE_URL, LOCAL_API_KEY_ENV, LOCAL_API_KEY_FILE,
                           LOCAL_API_TIMEOUT, LOCAL_API_REASONING_EFFORT)
    base_url = base_url or LOCAL_API_BASE_URL
    api_key = api_key or os.environ.get(LOCAL_API_KEY_ENV, '') \
        or read_key_file(LOCAL_API_KEY_FILE)
    # 本机 Ollama 一类端点是免 key 的：没有 key 就不发 Authorization，
    # 不能因此判定为致命错误（首次运行的向导允许留空）。
    timeout = timeout or LOCAL_API_TIMEOUT
    payload = {
        'model': model,
        'messages': messages,
        'temperature': temperature,
    }
    level = _thinking_level(thinking)
    if level is None:
        effort = (LOCAL_API_REASONING_EFFORT if reasoning_effort is None
                  else reasoning_effort)
    else:
        effort = 'none' if level == 'off' else level
        if level != 'off':
            max_tokens = _thinking_budget(max_tokens)
    if max_tokens:
        payload['max_tokens'] = int(max_tokens)
    if effort:
        payload['reasoning_effort'] = effort
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = 'Bearer ' + api_key
    req = urllib.request.Request(
        base_url.rstrip('/') + '/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers=headers)
    resp = _post_json(req, timeout, model, LOCAL_API_KEY_ENV,
                      key_file=LOCAL_API_KEY_FILE)
    return resp['choices'][0]['message']['content'].strip()


def _thinking_level(value):
    """配置值 → 挡位：bool 兼容旧调用；str 取挡位；None/未知 = 不传（后端默认）。"""
    from rp_config import API_THINKING_LEVELS
    if value is True:
        return 'high'
    if value is False:
        return 'off'
    if isinstance(value, str):
        v = value.strip().lower()
        return v if v in API_THINKING_LEVELS else 'off'
    return None


def _thinking_budget(max_tokens):
    """开思考时的输出预算下限（思维链占输出 token，小额度会让正文为空）。"""
    if not max_tokens:
        return max_tokens
    from rp_config import API_THINKING_MIN_TOKENS
    return max(int(max_tokens), API_THINKING_MIN_TOKENS)


def chat_api(messages, kind, model, *, temperature=0.7, max_tokens=None,
             enable_search=False, timeout=None, thinking=None, profile=None):
    """按模型 kind 路由到对应 API 后端（api=千问云端，localapi=本地服务）。

    thinking: None=各后端默认（关）；'off'/'low'/'medium'/'high'=思考挡位。
    profile: 仅千问用，指定用哪套端点与 key；None=全局 api_profile。
    """
    if kind == 'localapi':
        if enable_search:
            raise RuntimeError('本地 API 不支持联网搜索，请改用千问 API')
        return chat_localapi(messages, model, temperature=temperature,
                             max_tokens=max_tokens, timeout=timeout,
                             thinking=thinking)
    return chat_qwen(messages, model, temperature=temperature,
                     max_tokens=max_tokens, enable_search=enable_search,
                     timeout=timeout, thinking=thinking, profile=profile)
