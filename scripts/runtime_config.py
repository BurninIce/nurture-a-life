# -*- coding: utf-8 -*-
"""运行时配置层（热生效开关）。

群聊引入后配置项从几个膨胀到 40+，原来全部写死在 rp_config.py，改一个要重启进程。
本模块把「高频、需要运行中切换」的开关抽出来，做成进程内可变 + 落盘 JSON：

    state/runtime_config.json  ← 持久化（重启后仍生效）
    runtime_config.get(...)     ← 运行中读当前值（热生效）

设计：
- 这里只放「高频开关 + 少量关键阈值」，rp_config 的对应常量保留作**默认值回退**。
- 界面（admin_server）写 JSON 并调 runtime_config.reload()，进程内立即生效，无需重启。
- 与 rp_config 的关系：rp_config 是「代码默认值」，runtime_config 是「用户覆盖值」。
  读取时 get(key) 优先取覆盖值，没有则回退 rp_config 常量。
- 注册表自检：启动 load() 时校验 key 唯一、cfg 目标存在、默认值类型/范围/枚举；
  也可单独跑 `python scripts/runtime_config.py --check`（有问题退出码 1）。
  盘上的值按同一套 schema 校验，非法值丢弃并回退默认，不让坏配置拖到运行期。

用法：
    import runtime_config as rc
    rc.get('group_enabled')            # True/False（热生效）
    rc.get('gen_model')                # 'qwen3.5'
    rc.set_many({'group_enabled': False})  # 写盘 + 立即生效
"""
import json
import logging
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE_PATH = os.path.join(ROOT, 'state', 'runtime_config.json')

_LOG = logging.getLogger('runtime_config')

_LOCK = threading.RLock()
_SCHEMA_TYPES = ('bool', 'int', 'float', 'str', 'list')
_VALIDATION = None      # validate_schema() 的缓存结果（问题描述列表）

# ---------------------------------------------------------------------------
# 字段定义表（SCHEMA）：面板据此自动渲染，后端据此校验读写。
# 每项：
#   key     → runtime_config 的键（与 rp_config 常量名解耦）
#   type    → bool / int / float / str / list
#   group   → 面板分组
#   label   → 中文名
#   desc    → 说明（hover/副标题）
#   cfg     → 回退的 rp_config 常量名（延迟取值）
#   hot     → True=热生效（进程内立即），False=存盘重启生效（B 类低频调参）
#   min/max/step → 数值范围（可选）
#   choices → 枚举选项（可选，str 用）
#   hidden  → True=面板不自动渲染该项（值仍可读写），改由专门 UI 承载
#   ui      → 该项由哪种专门 UI 承载（model 模型卡 / slider 思考滑块 /
#             switch 右上角角色切换）；panel_check.py 会核对面板是否真实现了它
# ---------------------------------------------------------------------------
SCHEMA = [
    # —— 角色（热生效；只切指针，不碰资产/记忆）——
    # 切换入口在页面右上角；保留该 key 供 API/指令/启动读取，配置页不再渲染。
    # hidden 项不进通用表单，面板上看到的说明文字来自 admin_payloads，这里不写 desc。
    {'key': 'character', 'type': 'str', 'group': '角色', 'hot': True, 'hidden': True,
     'ui': 'switch', 'label': '当前角色', 'default': 'character'},

    # —— 运行开关（热生效）——
    {'key': 'group_enabled', 'type': 'bool', 'group': '运行开关', 'hot': True,
     'label': '群聊', 'desc': '关闭后不接管群消息（不记录、不主动、不回应），私聊照常', 'cfg': 'QQ_GROUP_ENABLED'},
    {'key': 'group_disabled', 'type': 'list', 'group': '运行开关', 'hot': True,
     'label': '关闭群聊的群', 'desc': '这些群单独不接管（逗号分隔的群号）；群内管理员可用 /group off 加入、/group on 移出',
     'default': []},
    {'key': 'proactive_enabled', 'type': 'bool', 'group': '运行开关', 'hot': True,
     'label': '主动开话题', 'desc': '群冷场时她主动起话头', 'default': True},
    # 对话/判官模型由配置页顶部的两块模型卡单独渲染（custom UI），这里只留存储定义。
    # 值形如 local|<tag> / localapi|<model> / qwen|<profile>|<model>；旧注册表 key 仍可解析。
    {'key': 'gen_model', 'type': 'str', 'group': '对话模型', 'hot': True, 'hidden': True,
     'ui': 'model', 'label': '对话模型', 'cfg': 'QQ_MODEL'},
    {'key': 'judge_model', 'type': 'str', 'group': '判官模型', 'hot': True, 'hidden': True,
     'ui': 'model', 'label': '判官模型', 'cfg': 'QQ_JUDGE_MODEL'},
    {'key': 'gen_thinking', 'type': 'str', 'group': '对话模型', 'hot': True, 'hidden': True,
     'label': '对话思考', 'cfg': 'API_THINKING', 'ui': 'slider',
     'choices': ['off', 'low', 'medium', 'high'],
     'labels': {'off': '关', 'low': '低', 'medium': '中', 'high': '高'}},
    {'key': 'judge_thinking', 'type': 'str', 'group': '判官模型', 'hot': True, 'hidden': True,
     'label': '判官思考', 'cfg': 'API_THINKING', 'ui': 'slider',
     'choices': ['off', 'low', 'medium', 'high'],
     'labels': {'off': '关', 'low': '低', 'medium': '中', 'high': '高'}},
    {'key': 'model_catalog_limit', 'type': 'int', 'group': '运行开关', 'hot': True,
     'label': '模型清单上限', 'desc': '模型下拉最多列出几个',
     'default': 10, 'min': 1, 'max': 100, 'unit': '个'},
    # 识图/今日日常等旁路调用只认这一档；对话/判官各自带档，见模型卡。
    {'key': 'api_profile', 'type': 'str', 'group': '运行开关', 'hot': True,
     'label': '辅助调用端点', 'cfg': 'QWEN_PROFILE_DEFAULT',
     'desc': '识图/今日日常/链接抓取用哪套端点；对话与判官在各自模型卡里单独选。'
             '切换前会先探测一次，失败则保持原档',
     'choices': ['dashscope'],
     'labels': {'dashscope': '千问官方'}},
    {'key': 'private_allow', 'type': 'list', 'group': '运行开关', 'hot': True,
     'label': '私聊白名单', 'desc': '逗号分隔的 QQ 号；留空 = 任何人可私聊', 'default': []},
    {'key': 'auto_open_admin', 'type': 'bool', 'group': '运行开关', 'hot': False,
     'label': '启动打开管理面板', 'desc': 'bot 启动后自动在浏览器打开我们的面板(6780)', 'default': True},
    {'key': 'auto_open_napcat_ui', 'type': 'bool', 'group': '运行开关', 'hot': False,
     'label': '启动打开 NapCat UI', 'desc': '启动时自动在浏览器打开 NapCat 扫码页(6099)', 'default': True},
    {'key': 'image_understand', 'type': 'bool', 'group': '运行开关', 'hot': True,
     'label': '图片识别', 'desc': '用视觉模型识别群友发的图片内容（本地视觉模型）', 'cfg': 'IMAGE_UNDERSTAND_ENABLED'},
    {'key': 'link_fetch', 'type': 'bool', 'group': '运行开关', 'hot': True,
     'label': '链接抓取', 'desc': '抓取群友发的链接标题/摘要喂给模型', 'cfg': 'LINK_FETCH_ENABLED'},

    # —— 主动开话题（重启生效）——
    # 发言门阈值/权重与冷却不在这里：它们属于「她有多愿意开口」，随角色卡走，
    # 在角色卡页编辑（见 admin_payloads._gate_schema）。
    {'key': 'gate_idle_sec', 'type': 'int', 'group': '主动开话题', 'hot': False,
     'label': '静默阈值', 'desc': '群内静默超过多少秒才允许主动', 'cfg': 'GATE_IDLE_SEC',
     'min': 60, 'max': 7200, 'unit': '秒'},
    {'key': 'gate_proactive_min_gap', 'type': 'int', 'group': '主动开话题', 'hot': False,
     'label': '主动最小间隔', 'desc': '两次主动之间最小间隔（秒）', 'cfg': 'GATE_PROACTIVE_MIN_GAP',
     'min': 60, 'max': 21600, 'unit': '秒'},
    {'key': 'gate_proactive_daily_max', 'type': 'int', 'group': '主动开话题', 'hot': False,
     'label': '每日主动上限', 'desc': '每群每天主动次数上限', 'cfg': 'GATE_PROACTIVE_DAILY_MAX',
     'min': 0, 'max': 50, 'unit': '次/天'},

    # —— 私聊主动（热生效；只对私聊白名单生效）——
    {'key': 'private_proactive_enabled', 'type': 'bool', 'group': '私聊主动', 'hot': True,
     'label': '私聊主动', 'desc': '一对一私聊里她自己找话题（仅白名单；默认关）',
     'cfg': 'PRIVATE_PROACTIVE_ENABLED'},
    {'key': 'private_proactive_idle_min', 'type': 'int', 'group': '私聊主动', 'hot': True,
     'label': '最短空闲', 'desc': '最后一条私聊后至少多久才考虑开口',
     'cfg': 'PRIVATE_PROACTIVE_IDLE_MIN', 'min': 600, 'max': 86400, 'unit': '秒'},
    {'key': 'private_proactive_idle_max', 'type': 'int', 'group': '私聊主动', 'hot': True,
     'label': '最长空闲', 'desc': '候选时间在最短~最长之间随机（避免每天同一时刻）',
     'cfg': 'PRIVATE_PROACTIVE_IDLE_MAX', 'min': 600, 'max': 172800, 'unit': '秒'},
    {'key': 'private_proactive_min_gap', 'type': 'int', 'group': '私聊主动', 'hot': True,
     'label': '主动最小间隔', 'desc': '两次主动之间最小间隔',
     'cfg': 'PRIVATE_PROACTIVE_MIN_GAP', 'min': 600, 'max': 172800, 'unit': '秒'},
    {'key': 'private_proactive_daily_max', 'type': 'int', 'group': '私聊主动', 'hot': True,
     'label': '每日主动上限', 'desc': '每个私聊对象每天主动次数上限（0=关闭）',
     'cfg': 'PRIVATE_PROACTIVE_DAILY_MAX', 'min': 0, 'max': 10, 'unit': '次/天'},
    {'key': 'private_proactive_hour_start', 'type': 'int', 'group': '私聊主动', 'hot': True,
     'label': '时段开始', 'desc': '学习样本不足时的活跃时段起点（小时）',
     'cfg': 'PRIVATE_PROACTIVE_HOUR_START', 'min': 0, 'max': 23, 'unit': '点'},
    {'key': 'private_proactive_hour_end', 'type': 'int', 'group': '私聊主动', 'hot': True,
     'label': '时段结束', 'desc': '学习样本不足时的活跃时段终点（24=到 23 点）',
     'cfg': 'PRIVATE_PROACTIVE_HOUR_END', 'min': 1, 'max': 24, 'unit': '点'},
    {'key': 'private_proactive_recent_days', 'type': 'int', 'group': '私聊主动', 'hot': True,
     'label': '最近互动天数', 'desc': '只在最近 N 天聊过的对象里主动，避免久未联系突然打扰',
     'cfg': 'PRIVATE_PROACTIVE_RECENT_DAYS', 'min': 1, 'max': 30, 'unit': '天'},

    # —— 晋升门槛（重启生效）——
    {'key': 'promote_min_interactions', 'type': 'int', 'group': '晋升门槛', 'hot': False,
     'label': '晋升互动下限', 'cfg': 'PROMOTE_MIN_INTERACTIONS',
     'min': 1, 'max': 50, 'unit': '轮'},
    {'key': 'promote_min_important_events', 'type': 'int', 'group': '晋升门槛', 'hot': False,
     'label': '晋升事件下限', 'desc': '记忆卡里 importance≥2 的事件数下限', 'cfg': 'PROMOTE_MIN_IMPORTANT_EVENTS',
     'min': 1, 'max': 20, 'unit': '条'},

    # —— 记忆归纳（重启生效）——
    {'key': 'reinduct_every', 'type': 'int', 'group': '记忆归纳', 'hot': False,
     'label': '归纳间隔', 'desc': '每 N 轮互动重归纳一次', 'cfg': 'REINDUCT_EVERY', 'module': 'memory_card',
     'min': 1, 'max': 50, 'unit': '轮'},
    {'key': 'buffer_cap', 'type': 'int', 'group': '记忆归纳', 'hot': False,
     'label': '话题缓冲上限', 'desc': '缓冲达此轮数强制闭合', 'cfg': 'BUFFER_CAP', 'module': 'memory_card',
     'min': 2, 'max': 30, 'unit': '轮'},
    {'key': 'recall_k', 'type': 'int', 'group': '记忆归纳', 'hot': False,
     'label': '回忆检索条数', 'cfg': 'RECALL_K', 'module': 'memory_util',
     'min': 1, 'max': 10, 'unit': '条'},

    # —— 上下文预算（重启生效）——
    {'key': 'group_turn_limit', 'type': 'int', 'group': '上下文预算', 'hot': False,
     'label': '群聊历史条数', 'desc': '传给模型的群聊历史条数', 'cfg': 'GROUP_TURN_LIMIT',
     'min': 4, 'max': 60, 'unit': '条'},
    {'key': 'group_msg_max_chars', 'type': 'int', 'group': '上下文预算', 'hot': False,
     'label': '单条消息上限', 'desc': '单条群消息进上下文的最大字数', 'cfg': 'GROUP_MSG_MAX_CHARS',
     'min': 50, 'max': 1000, 'unit': '字'},
    {'key': 'member_brief_limit', 'type': 'int', 'group': '上下文预算', 'hot': False,
     'label': '成员摘要上限', 'desc': '本轮发言人之外，还展开几个人的摘要', 'cfg': 'MEMBER_BRIEF_LIMIT',
     'min': 1, 'max': 8, 'unit': '人'},

    # —— 工具（热生效）——
    {'key': 'tools_enabled', 'type': 'bool', 'group': '工具', 'hot': True,
     'label': '工具总开关', 'desc': '允许她按需调用外部检索工具（默认关）', 'cfg': 'TOOLS_ENABLED'},
    {'key': 'web_search_enabled', 'type': 'bool', 'group': '工具', 'hot': True,
     'label': '网页搜索', 'desc': '遇到实时信息/明确要求时联网检索', 'cfg': 'TOOL_WEB_SEARCH_ENABLED'},
    {'key': 'news_enabled', 'type': 'bool', 'group': '工具', 'hot': True,
     'label': '新闻检索', 'desc': '问到新闻/热搜时检索最近几天要点', 'cfg': 'TOOL_NEWS_ENABLED'},
    {'key': 'tool_router_private', 'type': 'bool', 'group': '工具', 'hot': True,
     'label': '私聊启用', 'cfg': 'TOOL_ROUTER_PRIVATE'},
    {'key': 'tool_router_group_at', 'type': 'bool', 'group': '工具', 'hot': True,
     'label': '群聊旁听也检索', 'desc': '关闭时群里只有 @ 她才检索（推荐）', 'cfg': 'TOOL_ROUTER_GROUP_AT'},
    {'key': 'tool_search_timeout', 'type': 'int', 'group': '工具', 'hot': True,
     'label': '检索超时', 'desc': '单次联网检索的超时（秒）', 'cfg': 'TOOL_SEARCH_TIMEOUT',
     'min': 5, 'max': 120, 'unit': '秒'},
    {'key': 'tool_daily_max', 'type': 'int', 'group': '工具', 'hot': True,
     'label': '每日调用上限', 'desc': '每个自然日全部联网工具的总次数，0=不限；超额当天不再联网',
     'cfg': 'TOOL_DAILY_MAX', 'min': 0, 'max': 5000, 'unit': '次'},
    {'key': 'novel_enabled', 'type': 'bool', 'group': '工具', 'hot': True,
     'label': '原作小说检索', 'desc': '提到原作/第X卷/外传时检索小说原文（本地索引，不出网）',
     'cfg': 'TOOL_NOVEL_ENABLED'},
    {'key': 'novel_top_k', 'type': 'int', 'group': '工具', 'hot': True,
     'label': '小说检索段数', 'cfg': 'TOOL_NOVEL_TOP_K',
     'min': 1, 'max': 8, 'unit': '段'},
    {'key': 'novel_min_score', 'type': 'float', 'group': '工具', 'hot': True,
     'label': '小说命中阈值', 'desc': '相似度低于此值就当没查到（越高越保守）',
     'cfg': 'TOOL_NOVEL_MIN_SCORE', 'min': 0.0, 'max': 1.0},
    {'key': 'lore_enabled', 'type': 'bool', 'group': '工具', 'hot': True,
     'label': '角色设定条目', 'desc': '问到角色卡里的人物、世界观或能力设定时注入对应条目（本地，不出网，不需 @）',
     'cfg': 'LORE_ENABLED'},

    # —— 语音（热生效）——
    {'key': 'tts_enabled', 'type': 'bool', 'group': '语音', 'hot': True,
     'label': '语音回复', 'desc': '关闭后 /tts 与自然语言触发都不出声，文字照常，各会话的 /tts on 也一起失效',
     'cfg': 'TTS_ENABLED'},
    {'key': 'tts_natural', 'type': 'bool', 'group': '语音', 'hot': True,
     'label': '自然语言触发', 'desc': '「测测语音」「说句话」这类话也发语音；关闭后只认 /tts',
     'cfg': 'TTS_NATURAL_ENABLED'},
    {'key': 'tts_default_style', 'type': 'str', 'group': '语音', 'hot': True,
     'label': '默认语气', 'desc': '会话没设过语气时使用（/tts daily|battle 可为单个群/单个人设）',
     'cfg': 'TTS_DEFAULT_STYLE', 'choices': ['daily', 'battle'],
     'labels': {'daily': '日常', 'battle': '战斗'}},

    # —— CG（热生效）——
    {'key': 'cg_enabled', 'type': 'bool', 'group': 'CG', 'hot': True,
     'label': 'CG 生成',
     'desc': '允许 /cg 按当前关系与本人最近一次共同经历画一张（本地 ComfyUI）',
     'default': True},
    {'key': 'cg_cooldown', 'type': 'int', 'group': 'CG', 'hot': True,
     'label': '生成冷却', 'desc': '同一个人两次 /cg 的最小间隔', 'default': 600,
     'min': 60, 'max': 86400, 'unit': '秒'},
    {'key': 'cg_daily_max', 'type': 'int', 'group': 'CG', 'hot': True,
     'label': '每日上限', 'desc': '每个人每天最多画几张（0=不限）', 'default': 5,
     'min': 0, 'max': 50, 'unit': '张/天'},

]


def _schema_index():
    return {s['key']: s for s in SCHEMA}


def _defaults():
    """从 SCHEMA 生成默认值：cfg 常量回退（可跨模块），否则用 default。"""
    import rp_config as cfg
    out = {}
    _mod_cache = {}
    for s in SCHEMA:
        k = s['key']
        if 'cfg' in s:
            mod_name = s.get('module', 'rp_config')
            mod = _mod_cache.get(mod_name)
            if mod is None:
                mod = __import__(mod_name)
                _mod_cache[mod_name] = mod
            out[k] = getattr(mod, s['cfg'], s.get('default'))
        elif 'default' in s:
            out[k] = s['default']
        else:
            out[k] = None
    return out

# 进程内当前值（启动时读盘，运行中可 reload/set_many 更新）
_DATA = {}


def load():
    """读盘初始化 _DATA（合并默认值，缺字段补齐）。返回 dict。

    盘上的值按 SCHEMA 再校验一次：类型/范围/枚举不合法就丢弃并保留默认，
    避免手改坏的文件把字符串塞进阈值常量，等到运行期才炸。
    """
    global _DATA
    base = _defaults()
    idx = _schema_index()
    bad = []
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k, v in saved.items():
                s = idx.get(k)
                if not s:
                    continue
                cv = _coerce(s, v)
                if cv is None:
                    bad.append(k)
                    continue
                base[k] = cv
    except (OSError, ValueError):
        pass
    if bad:
        _LOG.warning('runtime_config 忽略 %d 个非法值（沿用默认）：%s',
                     len(bad), ', '.join(sorted(bad)))
    with _LOCK:
        _DATA = base
    return dict(_DATA)


def get(key, default=None):
    """读当前值（未 load 过则先 load）。"""
    if not _DATA:
        load()
    if default is None:
        default = _defaults().get(key)
    return _DATA.get(key, default)


def all():
    """返回全部当前值（浅拷贝）。"""
    if not _DATA:
        load()
    return dict(_DATA)


def group_on(gid):
    """某个群当前是否接管：全局开关优先，再看本群设置（回退旧的关闭名单）。"""
    if not get('group_enabled'):
        return False
    off = [str(g) for g in (get('group_disabled') or [])]
    try:
        import group_settings
        return group_settings.enabled(gid, legacy_off=off)
    except Exception:
        return str(gid) not in off


def _save():
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_DATA, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def _character_ok(pid):
    """角色指针只接受资产齐全的角色；校验失败则不落盘（不回退别人）。"""
    try:
        import persona
        ok, _reason = persona.validate(pid, strict=True)
        return ok
    except Exception:
        return False


def set_many(kv):
    """批量更新（只接受 SCHEMA 声明的 key，做类型校验），写盘并立即生效。返回更新后的 dict。

    必须先 load()：否则 _DATA 是空的，_save() 会把盘上已有的设置全部覆盖掉。
    """
    if not _DATA:
        load()
    idx = _schema_index()
    with _LOCK:
        for k, v in (kv or {}).items():
            s = idx.get(k)
            if not s:
                continue
            v = _coerce(s, v)
            if v is None:
                continue
            if k == 'character' and not _character_ok(v):
                continue
            _DATA[k] = v
        _save()
        out = dict(_DATA)
    return out


def _coerce(s, v):
    """按 schema 类型做最小校验/转换，非法值返回 None（跳过）。"""
    t = s['type']
    try:
        if t == 'bool':
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return v != 0
            if isinstance(v, str):
                sv = v.strip().lower()
                if sv in ('true', '1', 'yes', 'on', 'y'):
                    return True
                if sv in ('false', '0', 'no', 'off', 'n', ''):
                    return False
            return None
        if t == 'int':
            n = int(v)
            return _clamp(n, s)
        if t == 'float':
            f = float(v)
            return _clamp(f, s)
        if t == 'str':
            if 'choices' in s and v not in s['choices']:
                return None
            return str(v)
        if t == 'list':
            return [str(x).strip() for x in (v or []) if str(x).strip()]
    except (TypeError, ValueError):
        return None
    return v


def _clamp(n, s):
    lo, hi = s.get('min'), s.get('max')
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


_MISSING = object()
_IMPORTING = object()


def _type_ok(t, v):
    if t == 'bool':
        return isinstance(v, bool)
    if t == 'int':
        return isinstance(v, int) and not isinstance(v, bool)
    if t == 'float':
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == 'str':
        return isinstance(v, str)
    if t == 'list':
        return isinstance(v, list)
    return False


def _schema_default(s, mod_cache, problems):
    """解析一条 schema 的默认值；cfg 目标不存在时记问题并返回 _MISSING。

    模块仍在 import 中（例如 memory_card 初始化时回调 runtime_config）时不做判断：
    那一刻读到的只是半成品模块，报缺常量是假警报。调用方据此跳过缓存，
    等启动完成后的显式校验再给结论。
    """
    key = s.get('key') or '?'
    if 'cfg' not in s:
        return s.get('default')
    mod_name = s.get('module', 'rp_config')
    try:
        mod = mod_cache.get(mod_name)
        if mod is None:
            mod = __import__(mod_name)
            mod_cache[mod_name] = mod
    except Exception as exc:
        problems.append('%s: 无法导入模块 %s（%s）' % (key, mod_name, exc))
        return _MISSING
    spec = getattr(mod, '__spec__', None)
    if spec is not None and getattr(spec, '_initializing', False):
        return _IMPORTING
    if not hasattr(mod, s['cfg']):
        problems.append('%s: %s 缺少常量 %s' % (key, mod_name, s['cfg']))
        return _MISSING
    return getattr(mod, s['cfg'])


def validate_schema(schema=None):
    """自检注册表：key 唯一、类型合法、cfg 目标存在、默认值在范围内。

    只读，不写盘、不应用覆盖。返回问题描述列表（空 = 通过）。
    `schema` 参数用于测试注入坏样例；正常调用传 None，结果会缓存。
    """
    global _VALIDATION
    rows = SCHEMA if schema is None else schema
    problems = []
    seen = set()
    mod_cache = {}
    state = {'deferred': False}
    for i, s in enumerate(rows):
        if not isinstance(s, dict):
            problems.append('#%d: 不是 dict' % i)
            continue
        key = s.get('key')
        if not key or not isinstance(key, str):
            problems.append('#%d: 缺少 key' % i)
            continue
        if key in seen:
            problems.append('%s: key 重复' % key)
        seen.add(key)
        t = s.get('type')
        if t not in _SCHEMA_TYPES:
            problems.append('%s: 未知类型 %r' % (key, t))
        if not isinstance(s.get('hot'), bool):
            problems.append('%s: hot 必须是 bool' % key)
        if 'cfg' not in s and 'default' not in s:
            problems.append('%s: 既没有 cfg 也没有 default' % key)
            continue
        if 'cfg' in s and not isinstance(s['cfg'], str):
            problems.append('%s: cfg 必须是字符串' % key)
            continue
        value = _schema_default(s, mod_cache, problems)
        if value is _IMPORTING:
            state['deferred'] = True
            continue
        if value is _MISSING:
            continue
        if t in _SCHEMA_TYPES and not _type_ok(t, value):
            problems.append('%s: 默认值类型应为 %s，实际 %s（%r）'
                            % (key, t, type(value).__name__, value))
        lo, hi = s.get('min'), s.get('max')
        if lo is not None and hi is not None and lo > hi:
            problems.append('%s: min > max' % key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if lo is not None and value < lo:
                problems.append('%s: 默认值 %r 小于 min %r' % (key, value, lo))
            if hi is not None and value > hi:
                problems.append('%s: 默认值 %r 大于 max %r' % (key, value, hi))
        if t == 'str' and 'choices' in s and value not in s['choices']:
            problems.append('%s: 默认值 %r 不在 choices 中' % (key, value))
    if schema is None and not state['deferred']:
        _VALIDATION = problems
    return list(problems)


def validation_problems():
    """已缓存的注册表问题（启动日志/面板可用）；尚未校验则先跑一次。"""
    if _VALIDATION is None:
        return validate_schema()
    return list(_VALIDATION)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv not in ([], ['--check'], ['-c']):
        print('用法: python runtime_config.py --check')
        return 2
    problems = validate_schema()
    try:
        defaults = _defaults()
    except Exception as exc:
        problems.append('默认值生成失败：%r' % (exc,))
        defaults = {}
    print('runtime_config：%d 项注册，%d 项可解析默认值'
          % (len(SCHEMA), len(defaults)))
    for p in problems:
        print('  [!] %s' % p)
    if problems:
        print('校验失败：%d 项' % len(problems))
        return 1
    print('校验通过')
    return 0


def set_value(key, value):
    """单键更新。"""
    return set_many({key: value})


def reset():
    """恢复所有开关为默认值（写盘并立即生效）。返回默认 dict。"""
    base = _defaults()
    with _LOCK:
        _DATA.update(base)
        _save()
        out = dict(_DATA)
    return out


def reload():
    """从盘重读（外部改了文件时用）。"""
    return load()


def apply_overrides():
    """把 B 类（非热生效）的覆盖值写回对应模块常量，实现「存盘 + 重启生效」。

    在 bridge 启动时调用一次：rc.load() 读盘后，将 _DATA 中 hot=False 的 key
    按其 schema 的 cfg/module 声明 setattr 回 rp_config / memory_card 模块，
    使 group_gate / memory_card 里直接读 cfg.GATE_XXX 的代码拿到覆盖值。

    返回写回的 (key, 模块, 常量, 值) 列表（供日志/调试）。
    """
    if not _DATA:
        load()
    mod_cache = {}
    applied = []
    for s in SCHEMA:
        if s.get('hot'):
            continue  # 热生效项走 rc.get()，不写回常量
        if 'cfg' not in s:
            continue
        key = s['key']
        if key not in _DATA:
            continue
        val = _DATA[key]
        if val is None:
            continue
        mod_name = s.get('module', 'rp_config')
        mod = mod_cache.get(mod_name)
        if mod is None:
            try:
                mod = __import__(mod_name)
            except ImportError:
                continue
            mod_cache[mod_name] = mod
        if hasattr(mod, s['cfg']):
            setattr(mod, s['cfg'], val)
            applied.append((key, mod_name, s['cfg'], val))
    return applied


if __name__ == '__main__':
    sys.exit(main())
