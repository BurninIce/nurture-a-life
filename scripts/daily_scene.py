# -*- coding: utf-8 -*-
"""
角色「今日日常」场景生成器（千问 API 联网推演）。

- 每天 1 次：用千问 API（enable_search 联网，真实天气/社会热点）生成一个
  今日日常场景（氛围：时段/天气/地点 + 一件日常小事），注入 system prompt，
  给对话一个「当下」锚点，避免模型过于专注材料/原作设定。
- 缓存：state/daily_scene.json（{date, text, outfit, weather, recent}），当天所有消息共用同一场景（稳定锚点）
- 着装：与场景同一次调用产出英文 outfit，按实况体感温度档（角色卡 outfit.bands）校验；
  不合格或缺失时按档位预设轮换，拿不到天气就按月份季节档
- 跨天刷新：桥接 ticker 调 refresh_async()，日期变了就在后台重新生成，不用重启
- 去重：缓存里记最近 10 天的场景，生成时要求换场所类别、换小事类别
- 失败兜底：API 失败/超时/无 key/模型输出拒绝文本 → 返回 ''（静默不注入，不阻塞对话，不缓存失败）
  失败后 15 分钟内不再重试，避免 ticker 每 30 秒打一次 API。

注意：py3.6 兼容（rp_eval.py 跑在 3.6.8），不用 walrus/类型标注。
"""
import datetime
import json
import logging
import os
import random
import threading
import time
import urllib.parse
import re
import urllib.request

from rp_common import chat_api, chat_qwen  # noqa: F401  （chat_qwen 保留给旧调用/测试打桩）
import card_source
import day_life
import persona
import rp_config as cfg

LOG = logging.getLogger('daily_scene')

DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'state', 'daily_scene.json')
STATE_PATH = DEFAULT_STATE_PATH
_OVERRIDE = None
WEATHER_URL = 'https://wttr.in/%s?format=j1'  # 免 key 天气 API（结构化 JSON）

RECENT_KEEP = 10       # 缓存里保留最近几条场景（当天 + 前 9 天），去重窗口覆盖两周
GEN_RETRY_SEC = 900    # 生成失败后的冷却时间
MIN_SCENE_CHARS = 20
MAX_SCENE_CHARS = 220
# 模型在缺天气/被约束卡住时会把「拒绝回答」当场景输出（实测见过），
# 这类文本一旦缓存就会整天注入 system。
REFUSAL_MARKS = ('请补充', '由于您', '未提供', '无法生成', '无法准确', '抱歉',
                 '作为AI', '作为 AI')

_gen_lock = threading.Lock()
_fail_until = 0.0


def set_path(path=None):
    """切换缓存路径（离线自测隔离用）。path=None 恢复按角色解析。"""
    global STATE_PATH, _OVERRIDE
    _OVERRIDE = path
    STATE_PATH = path or DEFAULT_STATE_PATH
    return _path()


def _path():
    """当前角色的日常缓存；测试显式 set_path 时优先。"""
    return _OVERRIDE or persona.daily_scene_path()

# wttr.in 的 weatherDesc 恒为英文（j1 不吃 lang=zh），
# 而 qwen3.6-flash 拿到 "Smoky haze" 会当噪音忽略，写出「午后阳光、燥热」——
# 和 11°C 的实况直接矛盾。所以自己翻，别指望模型。
WEATHER_DESC_ZH = {
    'sunny': '晴', 'clear': '晴', 'partly cloudy': '多云', 'cloudy': '阴',
    'overcast': '阴天', 'mist': '薄雾', 'fog': '雾', 'freezing fog': '冻雾',
    'haze': '霾', 'smoky haze': '烟霾', 'smoke': '烟霾',
    'light drizzle': '毛毛雨', 'patchy light drizzle': '零星毛毛雨',
    'light rain': '小雨', 'patchy light rain': '零星小雨',
    'moderate rain': '中雨', 'heavy rain': '大雨',
    'light rain shower': '阵雨', 'moderate or heavy rain shower': '强阵雨',
    'torrential rain shower': '暴雨', 'patchy rain nearby': '局部有雨',
    'patchy rain possible': '可能有雨', 'thundery outbreaks possible': '可能有雷阵雨',
    'light snow': '小雪', 'moderate snow': '中雪', 'heavy snow': '大雪',
    'blizzard': '暴雪', 'blowing snow': '风雪', 'sleet': '雨夹雪',
    'light sleet': '小雨夹雪', 'wintry mix': '雨雪混合',
}


def _weather_zh(desc):
    """英文天气描述转中文；未收录时保留原文（总比丢掉强）。"""
    d = (desc or '').strip()
    return WEATHER_DESC_ZH.get(d.lower(), d)


def _today():
    return datetime.date.today().strftime('%Y-%m-%d')


def _load_cache():
    try:
        with open(_path(), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(data):
    # 后台刷新时对话线程可能正在读，先写临时文件再原子替换，读方不会半截。
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _fetch_weather_info(city):
    """wttr.in 抓结构化天气（免 key）。返回 {temp_c, feels_c, desc, humidity, wind}；失败返回 None。

    UA 必须是浏览器串：实测 `curl/8.0` 或空 UA 会触发
    `SSL: UNEXPECTED_EOF_WHILE_READING`，天气永远拿不到（2026-10-20 查出来的），
    而 Mozilla/5.0 一次就通（39KB JSON）。
    weatherDesc 仍是英文（j1 格式不吃 lang=zh），交给模型在写场景时译成中文。
    """
    try:
        url = WEATHER_URL % urllib.parse.quote(city)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        cur = resp['current_condition'][0]
        desc = (cur.get('lang_zh') or [{}])[0].get('value') or \
               (cur.get('weatherDesc') or [{}])[0].get('value') or ''
        return {'temp_c': _num(cur.get('temp_C')), 'feels_c': _num(cur.get('FeelsLikeC')),
                'desc': _weather_zh(desc), 'humidity': cur.get('humidity', '?'),
                'wind': cur.get('windspeedKmph', '?')}
    except Exception:
        return None


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _format_weather(info):
    """只给模型气温与天气现象：湿度/风速等原始数值会被原样抄进场景。"""
    if not info:
        return ''
    temp = info.get('temp_c')
    return '气温%s°C、%s' % ('?' if temp is None else temp, info.get('desc', ''))


def _fetch_weather(city):
    """中文短语天气文本；失败返回 ''。"""
    return _format_weather(_fetch_weather_info(city))


# ---------- 着装 ----------

def _outfit_cfg():
    return card_source.load()['outfit']


def _band(info, date=None):
    """按体感温度（缺失用气温）选档；没有天气按月份季节档。"""
    oc = _outfit_cfg()
    bands = oc['bands']
    temp = None
    if info:
        temp = info.get('feels_c') if info.get('feels_c') is not None else info.get('temp_c')
    if temp is None:
        month = (date or datetime.date.today()).month
        want = oc['season_band'].get(str(month))
        return next((b for b in bands if b['id'] == want), bands[-1])
    for b in bands:
        if b.get('below') is None or temp < b['below']:
            return b
    return bands[-1]


def _wet(info):
    desc = ((info or {}).get('desc') or '').lower()
    oc = _outfit_cfg()
    return any(w in desc for w in oc['rain_words'] + oc['snow_words'])


def outfit_ok(outfit, band):
    """英文、长度合理、满足档位的必需/禁用词。"""
    text = (outfit or '').strip()
    if not text or re.search('[\u3000-\u9fff\uff00-\uffef]', text):
        return False
    if not 8 <= len(text.split()) <= 60:
        return False
    low = text.lower()
    if any(w in low for w in band.get('forbid', [])):
        return False
    need = band.get('require_any') or []
    return not need or any(w in low for w in need)


def pick_outfit(candidate, info, date=None):
    """模型给的合格就用，否则用档位预设（按日期轮换）；雨雪天补伞。"""
    date = date or datetime.date.today()
    band = _band(info, date)
    outfit = (candidate or '').strip()
    if not outfit_ok(outfit, band):
        presets = band['fallback']
        outfit = presets[date.toordinal() % len(presets)]
    if _wet(info) and 'umbrella' not in outfit.lower():
        outfit = outfit.rstrip() + ' ' + _outfit_cfg()['umbrella']
    return outfit


def _outfit_note(info):
    """着装段落。角色卡的 static 只保证「有几个字段」，不保证有性别/年龄/住所：
    示例卡 character 就只有 Identity/Personality/Speech/Behavior/Rule 五项，
    直接取 st['Gender'] 会在全新安装（默认角色）上 KeyError，整段日常生成失败。
    """
    st = card_source.load()['static']

    def _get(key, default):
        val = st.get(key)
        return val.strip() if isinstance(val, str) and val.strip() else default

    clothing = st.get('Clothing')
    style = (clothing.get('Style') if isinstance(clothing, dict) else '') or ''
    band = _band(info)
    # hint 是可选字段：示例卡 character 的档位只有 require/forbid/fallback，
    # 缺了就按档位 id 给一句通用说明，不能直接 KeyError。
    hint = (band.get('hint') or '').strip() or ('按「%s」档选择厚薄' % band.get('id', '当季'))
    return cfg.DAILY_OUTFIT_NOTE.format(
        gender=_get('Gender', '未登记'), age=_get('Age', '未登记'),
        residence=_get('Residence', '未登记'),
        style=(style.strip() or '普通日常'), band_hint=hint,
        rain_hint='；有雨雪，带伞' if _wet(info) else '')


def _plan_note():
    """日程生成说明：固定作息来自角色卡 life，没有就交给模型按性格安排。"""
    rows = ['· ' + line for line in day_life.routine()]
    if not rows:
        rows = ['（没有固定作息，按她的性格与今天的状态安排。）']
    habits = day_life.habits()
    if habits:
        rows.append('习惯：' + '；'.join(habits))
    return cfg.DAILY_PLAN_NOTE.format(
        name=persona.direct_name(), routine='\n'.join(rows))


def _split_output(raw):
    """模型输出 → (scene, outfit)。不是 JSON 时整段当场景、着装留空。"""
    text = (raw or '').strip()
    start, end = text.find('{'), text.rfind('}')
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict) and obj.get('scene'):
                return str(obj['scene']).strip(), str(obj.get('outfit') or '').strip()
        except ValueError:
            pass
    return text, ''


def _split_plan(raw):
    """从同一次调用的 JSON 里取 plan 列表；缺失或格式不对返回 []（由作息兜底）。"""
    text = (raw or '').strip()
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return []
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return []
    plan = obj.get('plan') if isinstance(obj, dict) else None
    return plan if isinstance(plan, list) else []


def _gen_model_key():
    """当前对话模型（面板「生成模型」）的注册表 key；读不到就用 DAILY_MODEL。"""
    try:
        import runtime_config as rc
        return rc.get('gen_model') or cfg.DAILY_MODEL
    except Exception:
        return cfg.DAILY_MODEL


def _generate(recent=None, place_type=None):
    """抓真实天气 + 调千问 API 生成今日日常场景与着装。

    返回 {'raw': 模型原文, 'weather': 天气 dict 或 None}；彻底失败时 raw 为 ''。
    （自测替身可以直接返回字符串，ensure_today 两种都认。）
    """
    weather_city = (cfg.DAILY_CITY or cfg.DAILY_SCENE_CITY).strip()
    info = _fetch_weather_info(weather_city)
    weather = _format_weather(info)
    prompt = persona.prompt(
        'daily', scene_city=cfg.DAILY_SCENE_CITY,
        name=persona.direct_name(),
        weather_city=weather_city,
        weather=weather if weather else
        '（暂缺——那就只写环境氛围，不要出现具体气温、天气词，更不要提"数据"两个字；'
        '也不要向用户索要天气、不要说明缺少数据）')
    if place_type:
        prompt += '\n' + cfg.DAILY_PLACE_TYPE_NOTE.format(place_type=place_type)
    if recent:
        prompt += '\n' + cfg.DAILY_RECENT_NOTE.format(
            recent='\n'.join('· %s' % t for t in recent))
    outfit_note = '\n' + _outfit_note(info) + '\n' + _plan_note()
    hard_parts = []
    if place_type:
        hard_parts.append(cfg.DAILY_PLACE_TYPE_HARD_NOTE.format(place_type=place_type))
    if recent:
        hard_parts.append(cfg.DAILY_RECENT_HARD_NOTE)
    hard_note = ('\n' + '\n'.join(hard_parts)) if hard_parts else ''
    # 用面板里选的那个对话模型（gen_model）：换模型时日常跟着换，不会一边云端一边本地。
    kind, model = cfg.model_of(_gen_model_key())
    if kind == 'localapi':
        try:
            raw = chat_api([{'role': 'user',
                             'content': prompt + '\n' + cfg.DAILY_NO_SEARCH_NOTE
                             + outfit_note + hard_note}],
                           kind, model, temperature=cfg.DAILY_GEN_TEMPERATURE,
                           max_tokens=cfg.DAILY_MAX_TOKENS)
            return {'raw': raw, 'weather': info}
        except Exception as e:
            LOG.warning('今日日常生成失败（自建端点）：%s', e)
            return {'raw': '', 'weather': info}
    # 先试联网（只有原生 DashScope 协议支持）；key 不支持联网就退回普通生成，
    # 宁可不带热点，也别把「今日日常」整块丢掉（那会让她的场景感凭空消失）。
    try:
        raw = chat_api([{'role': 'user', 'content': prompt + outfit_note + hard_note}],
                       kind, model, temperature=cfg.DAILY_GEN_TEMPERATURE,
                       max_tokens=cfg.DAILY_MAX_TOKENS, enable_search=True)
        return {'raw': raw, 'weather': info}
    except Exception:
        pass
    # 没有联网时必须明说：否则模型会编出"本地新闻正在播报…"这种看着像真事的内容
    try:
        raw = chat_api(
            [{'role': 'user',
              'content': prompt + '\n' + cfg.DAILY_NO_SEARCH_NOTE + outfit_note + hard_note}],
            kind, model, temperature=cfg.DAILY_GEN_TEMPERATURE,
            max_tokens=cfg.DAILY_MAX_TOKENS, enable_search=False)
        return {'raw': raw, 'weather': info}
    except Exception:
        return {'raw': '', 'weather': info}


def _valid_scene(text):
    """输出能不能当场景用：长度合理、不是模型在要数据/道歉。"""
    t = (text or '').strip()
    if not (MIN_SCENE_CHARS <= len(t) <= MAX_SCENE_CHARS):
        return False
    squeezed = t.replace(' ', '')
    return not any(m in squeezed for m in REFUSAL_MARKS)


def _recent_texts(cache, exclude_today=True):
    """最近若干天写过的场景（不含当天，供去重 prompt 用）。"""
    today = _today()
    out = []
    for row in (cache.get('recent') or []):
        if not isinstance(row, dict):
            continue
        text = (row.get('text') or '').strip()
        if not text:
            continue
        if exclude_today and row.get('date') == today:
            continue
        out.append(text)
    if not out and cache.get('text') and cache.get('date') != today:
        # 老版本缓存没有 recent 字段：把上一条场景也当成「最近写过」
        out.append(cache['text'].strip())
    return out[-RECENT_KEEP + 1:]


def _today_tries(cache):
    """当天已经掷过的场景：重掷时全部避开，而不是只避开最后一条。"""
    if cache.get('date') != _today():
        return []
    rows = [t.strip() for t in (cache.get('tried') or [])
            if isinstance(t, str) and t.strip()]
    if not rows and cache.get('text'):
        rows = [cache['text'].strip()]
    return rows


def _recent_types(cache):
    """最近几天用过的环境类型。"""
    return [r.get('place_type') for r in (cache.get('recent') or [])
            if isinstance(r, dict) and r.get('place_type')]


def _today_tried_types(cache):
    """当天重掷已经用过的环境类型。"""
    if cache.get('date') != _today():
        return []
    return [t for t in (cache.get('tried_types') or []) if t]


def _pick_place_type(cache, force=False):
    """选今天的环境类型：优先没用过的；重掷时排除当天所有已用类型。"""
    used = set(_recent_types(cache))
    if force and cache.get('date') == _today():
        used.update(_today_tried_types(cache))
    free = [t for t in cfg.DAILY_PLACE_TYPES if t not in used]
    return random.choice(free or list(cfg.DAILY_PLACE_TYPES))


def _save_success(cache, text, outfit='', weather=None, place_type=None, plan=None):
    """写当天场景与着装，并把最近几天的场景一起留着用于去重。

    plan 是当天日程（self_state 的初始条目）；模型没给时用角色卡固定作息兜底。
    """
    today = _today()
    tries = _today_tries(cache)
    if text not in tries:
        tries.append(text)
    tried_types = _today_tried_types(cache)
    if place_type and place_type not in tried_types:
        tried_types.append(place_type)
    rows = [r for r in (cache.get('recent') or [])
            if isinstance(r, dict) and r.get('date') != today and r.get('text')]
    rows.append({'date': today, 'text': text, 'place_type': place_type})
    plan = day_life.normalize_items(plan)
    if not plan:
        plan = day_life.routine_items()
    _save_cache({'date': today, 'text': text, 'outfit': outfit, 'weather': weather,
                 'plan': plan, 'recent': rows[-RECENT_KEEP:], 'tried': tries,
                 'tried_types': tried_types})



def ensure_today(force=False):
    """预热/刷新当天场景：未缓存则生成（慢路径），失败不落盘。

    force=True 强制重新生成并覆盖当天缓存（改完 prompt 或天气源时用；
    当天所有旧尝试都会作为「最近写过」喂给模型，并换一个未用过的环境类型）。
    返回场景文本或 ''。
    """
    global _fail_until
    cache = _load_cache()
    if not force and cache.get('date') == _today():
        return cache.get('text', '')
    if time.time() < _fail_until:
        return cache.get('text', '') if cache.get('date') == _today() else ''
    if not _gen_lock.acquire(False):
        # 后台已经在生成：本次不重复打 API
        return cache.get('text', '') if cache.get('date') == _today() else ''
    try:
        recent = _recent_texts(cache)
        if force and cache.get('date') == _today():
            # 重掷时把当天所有旧尝试都作为「已写过」；只留最近几条会让更早的场景再次被抽中
            recent = recent + _today_tries(cache)
        place_type = _pick_place_type(cache, force=force)
        res = _generate(recent, place_type=place_type)
        if isinstance(res, dict):
            raw, weather = res.get('raw') or '', res.get('weather')
        else:
            raw, weather = res or '', None
        text, outfit = _split_output(raw)
        if not _valid_scene(text):
            _fail_until = time.time() + GEN_RETRY_SEC
            LOG.warning('今日日常生成无效（%d 字），%d 分钟后再试：%s',
                        len(text or ''), GEN_RETRY_SEC // 60,
                        (text or '')[:60].replace('\n', ' '))
            return ''
        chosen = pick_outfit(outfit, weather)
        if chosen != outfit:
            LOG.info('今日着装改用档位预设（模型给的：%s）', outfit[:80])
        _save_success(cache, text, chosen, weather, place_type=place_type,
                      plan=_split_plan(raw))
        _fail_until = 0.0
        LOG.info('今日日常已生成（%s，%d 字）：%s', place_type, len(text), text[:40])
        return text
    except Exception:
        _fail_until = time.time() + GEN_RETRY_SEC
        LOG.exception('今日日常生成异常')
        return ''
    finally:
        _gen_lock.release()


def refresh_async():
    """ticker 每轮调用：日期变了就后台生成，不阻塞消息处理。

    返回是否真的起了后台任务。失败冷却期内、或已有任务在跑时返回 False。
    """
    if _load_cache().get('date') == _today():
        return False
    if time.time() < _fail_until or _gen_lock.locked():
        return False
    threading.Thread(target=ensure_today, name='daily-scene',
                     daemon=True).start()
    return True


def get_cached():
    """对话期专用：只读当天缓存，绝不触发 API 生成（每轮零延迟）。
    缓存缺失/失败返回 ''（静默不注入，不阻塞对话）。"""
    cache = _load_cache()
    if cache.get('date') == _today():
        return cache.get('text', '')
    return ''


def get_outfit():
    """当天着装（英文一句）。没有当天场景时返回 ''；老缓存没有 outfit 字段就按天气/季节档补。"""
    cache = _load_cache()
    if cache.get('date') != _today() or not cache.get('text'):
        return ''
    return cache.get('outfit') or pick_outfit('', cache.get('weather'))


def get_plan():
    """当天日程条目（[{time,text,key,source}]）。没有当天缓存返回 []。

    老缓存没有 plan 字段时按角色卡固定作息补一份，保证注入块与日记有素材。
    """
    cache = _load_cache()
    if cache.get('date') != _today():
        return []
    rows = cache.get('plan')
    items = day_life.normalize_items(rows)
    if items:
        return items
    return day_life.normalize_items(day_life.routine_items(), source='routine')
