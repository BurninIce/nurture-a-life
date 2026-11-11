# -*- coding: utf-8 -*-
"""角色卡导入：把一个 JSON 包变成可直接切换的 persona 资产。

支持两种输入：
  1. 完整包 {"persona": {...}, "card": {...}, "stats": {...}}
  2. 裸 card.json（顶层就是 static / relationship / lore）

缺 persona.json 时按卡内信息补最小清单；缺 stats.json 时按关系分档生成
通用三项规则；缺 outfit 时补通用着装档。只写 JSON 文本资产，**不导入**
tts / cg / 语料 / 原作索引等二进制或外部文件。

安全边界：
  - id 必须符合 persona.valid_id；已存在默认拒绝，overwrite=true 时先备份
  - stats.characters 必须含该 id，绝不借用别的角色数值规则
  - 落盘后必须通过 persona.validate(strict=True) 与 card_source 渲染
"""
import copy
import datetime
import json
import os
import re
import shutil
import threading

import card_source
import persona
import stat_rules

MAX_BUNDLE_BYTES = 2 * 1024 * 1024
_FILES = ('persona.json', 'card.json', 'stats.json', 'system_en.txt')
_STAT_KEYS = ('affection', 'trust', 'familiarity')
_CONTROL_KEYS = ('overwrite', 'activate', 'dry_run', 'id')
_PROMPT_KEYS = ('daily', 'private_proactive', 'gate_judge', 'gate_proactive')
_GATE_LIST_KEYS = ('direct_names', 'interest_words', 'expert_words')

_LOCK = threading.RLock()


class PersonaImportError(ValueError):
    pass


class PersonaImportConflict(PersonaImportError):
    pass


# ---------- 默认内容 ----------

_DEFAULT_STATS = {
    'affection': {
        'label': '好感',
        'meaning': '她愿不愿意靠近你、把你当成一个具体的人。',
        'up': ['真诚、愉快的相处：小步（1~3）', '一起经历过事：按经历幅度'],
        'down': ['明确排斥、厌烦：1~3'],
        'not_affect': ['顺从、讨好、夸赞不直接涨好感'],
        'gain_caps': {'陌生': 4, '相识': 8, '熟络': 10, '亲密': 12, '倾心': 15},
        'gain_caps_big': {'陌生': 6, '相识': 12, '熟络': 25, '亲密': 30, '倾心': 32},
        'down_cap': 3,
    },
    'trust': {
        'label': '信任',
        'meaning': '她敢不敢把真实的自己、真实的目的和弱点交到你手里。',
        'up': ['说到做到、兑现承诺：小步（1~3）', '尊重她的边界：小步（1~2）'],
        'down': ['撒谎、隐瞒被拆穿：1~2'],
        'not_affect': ['玩笑、调情本身不涨信任'],
        'gain_caps': {'陌生': 4, '相识': 6, '熟络': 8, '亲密': 10, '倾心': 12},
        'gain_caps_big': {'陌生': 4, '相识': 10, '熟络': 20, '亲密': 25, '倾心': 28},
        'down_cap': 2,
    },
    'familiarity': {
        'label': '熟悉度',
        'meaning': '你们一起经历过多少；只说明认识得久，不代表她喜欢你或信任你。',
        'up': ['聊得久、反复互动：小步（1~3）', '一起做过事、有共同经历：按经历幅度'],
        'down': [],
        'not_affect': ['熟悉度永不下降——相处过就是认识过'],
        'gain_caps': {'陌生': 3, '相识': 6, '熟络': 8, '亲密': 10, '倾心': 12},
        'gain_caps_big': {'陌生': 3, '相识': 8, '熟络': 20, '亲密': 22, '倾心': 24},
        'down_cap': 0,
    },
}

_DEFAULT_FLOOR = [
    {'level': '倾心', 'affection': 85, 'trust': 75, 'familiarity': 80},
    {'level': '亲密', 'affection': 70, 'trust': 60, 'familiarity': 65},
    {'level': '熟络', 'affection': 40, 'trust': 30, 'familiarity': 40},
    {'level': '相识', 'affection': 20, 'trust': 20, 'familiarity': 0},
]

_DEFAULT_REL_STATS = {
    'familiarity': [
        {'id': 'F1', 'min': 0,
         'speech': 'friendly and polite; you do not pretend to know their history.',
         'behavior': 'react to what they say now; do not invent shared memories.'},
        {'id': 'F2', 'min': 20,
         'speech': 'formality relaxes; callbacks to earlier exchanges are allowed.',
         'behavior': 'you may recall one past exchange that actually appears in memory.'},
        {'id': 'F3', 'min': 45,
         'speech': 'casual and brief; shorthand and running jokes are fine.',
         'behavior': 'pick up an unfinished topic once; never speak for them.'},
    ],
    'affection': [
        {'id': 'A1', 'min': 0,
         'speech': 'courteous and reserved; no teasing yet.',
         'behavior': 'respond, but leave the initiative with them.'},
        {'id': 'A2', 'min': 35,
         'speech': 'warmer, softer, a little teasing.',
         'behavior': 'show interest in their day; ask one question back.'},
        {'id': 'A3', 'min': 70,
         'speech': 'openly fond and attentive; pleasure shows.',
         'behavior': 'notice their mood and comfort them; never use jealousy as affection.'},
    ],
    'trust': [
        {'id': 'T1', 'min': 0,
         'speech': 'feelings stay behind a calm surface; painful topics get a brief answer.',
         'behavior': 'take claims with polite reserve; handle your own troubles yourself.'},
        {'id': 'T2', 'min': 40,
         'speech': 'candid about present feelings and small worries.',
         'behavior': 'take their word; give the outline of a painful topic once, then stop.'},
        {'id': 'T3', 'min': 70,
         'speech': 'you can admit weakness, guilt and fear plainly.',
         'behavior': 'answer the specific event asked about; you may ask for their opinion.'},
    ],
}


def _default_outfit():
    return {
        'line': "Today's outfit: {outfit}",
        'bands': [
            {'id': 'cold', 'below': 15,
             'hint': 'below 15°C: warm layers are required',
             'require_any': ['jacket', 'coat', 'sweater', 'hoodie'],
             'forbid': ['sleeveless', 'short-sleeve', 'short sleeve', 'sandal',
                        'shorts', 'bare legs'],
             'fallback': [
                 'A warm sweater, a heavy jacket, straight trousers and sturdy shoes.',
                 'A long coat over a knit top, jeans and closed shoes.',
             ]},
            {'id': 'mild', 'below': 26,
             'hint': '15–25°C: light layers, no heavy coat',
             'require_any': [],
             'forbid': ['down jacket', 'down coat', 'heavy coat', 'wool coat',
                        'scarf', 'thermal', 'boots'],
             'fallback': [
                 'A long-sleeve shirt, loose trousers and canvas sneakers.',
                 'A light jacket over a T-shirt, jeans and everyday shoes.',
             ]},
            {'id': 'hot', 'below': None,
             'hint': '26°C or above: breathable summer wear only',
             'require_any': [],
             'forbid': ['coat', 'jacket', 'cardigan', 'knit', 'sweater', 'wool',
                        'thermal', 'tights', 'boots', 'long-sleeve', 'long sleeve'],
             'fallback': [
                 'A short-sleeve shirt, light trousers and sandals.',
                 'A breathable top, shorts and canvas shoes.',
             ]},
        ],
        'season_band': {
            '1': 'cold', '2': 'cold', '3': 'mild', '4': 'mild',
            '5': 'mild', '6': 'hot', '7': 'hot', '8': 'hot',
            '9': 'hot', '10': 'mild', '11': 'mild', '12': 'cold',
        },
        'rain_words': ['雨', 'rain', 'drizzle', 'shower', 'thunder'],
        'snow_words': ['雪', 'snow', 'sleet', 'blizzard'],
        'umbrella': 'She carries a simple umbrella.',
    }


# ---------- 工具 ----------

def _warn(warnings, text):
    if text not in warnings:
        warnings.append(text)


def _is_card(obj):
    return isinstance(obj, dict) and all(
        k in obj for k in ('static', 'relationship', 'lore'))


def _clean_claiming(text):
    name = re.split(r'\s*[\(（]', str(text or '').strip(), 1)[0].strip()
    return name


def _claiming_alias(text):
    m = re.search(r'[\(（]([^)）]+)[\)）]', str(text or ''))
    return m.group(1).strip() if m else ''


def _unique(items):
    out, seen = [], set()
    for item in items:
        s = str(item or '').strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------- 归一化 ----------

def _normalize_card(card, pid, display, warnings):
    card = copy.deepcopy(card)
    card['version'] = int(card.get('version') or 1)
    card['character'] = pid
    if not str(card.get('preamble') or '').strip():
        card['preamble'] = (
            'You are %s. This card defines who you are. Blocks after it '
            '(relationship, canon notes, daily scene, memory) are per-turn context.'
            % display)

    static = card.get('static')
    if not isinstance(static, dict):
        raise PersonaImportError('card.static 必须是对象')
    defaults = (
        ('Claiming', display, 'static.Claiming'),
        ('Gender', 'unspecified', 'static.Gender'),
        ('Age', 'unknown', 'static.Age'),
        ('Residence', 'unknown', 'static.Residence'),
    )
    for key, value, label in defaults:
        if not str(static.get(key) or '').strip():
            static[key] = value
            _warn(warnings, '缺少 %s，已补默认值' % label)
    clothing = static.get('Clothing')
    if not isinstance(clothing, dict):
        clothing = {}
        static['Clothing'] = clothing
        _warn(warnings, '缺少 static.Clothing，已补通用着装描述')
    if not str(clothing.get('Style') or '').strip():
        clothing['Style'] = 'casual everyday clothes'
        _warn(warnings, '缺少 static.Clothing.Style，已补默认值')
    clothing.setdefault(
        'Today', 'follow 「Today\'s outfit」 in the daily block when asked.')
    clothing.setdefault('Past', 'ordinary everyday clothes.')

    rel = card.get('relationship')
    if not isinstance(rel, dict):
        raise PersonaImportError('card.relationship 必须是对象')
    if not str(rel.get('header') or '').strip():
        rel['header'] = '【Relationship with the current speaker】'
    if not str(rel.get('group_note') or '').strip():
        rel['group_note'] = (
            'Group chat: the room changes, not your closeness; treat the '
            'current speaker the same as in private.')
    if not isinstance(rel.get('bridges'), list):
        rel['bridges'] = []
    stats = rel.get('stats')
    if not isinstance(stats, list) or not stats:
        stats = []
        rel['stats'] = stats
        _warn(warnings, '缺少 relationship.stats，已补三项默认分档')
    by_key = {}
    for item in stats:
        if not isinstance(item, dict):
            raise PersonaImportError('relationship.stats 里的条目必须是对象')
        key = str(item.get('key') or '').strip()
        if not key:
            raise PersonaImportError('relationship.stats 条目缺少 key')
        by_key[key] = item
    for key in _STAT_KEYS:
        item = by_key.get(key)
        if item is None:
            item = {'key': key, 'label': key.title(),
                    'tiers': copy.deepcopy(_DEFAULT_REL_STATS[key])}
            stats.append(item)
            _warn(warnings, 'relationship.stats 缺少 %s，已补默认分档' % key)
        item.setdefault('label', key.title())
        tiers = item.get('tiers')
        if not isinstance(tiers, list) or not tiers:
            raise PersonaImportError('relationship.stats.%s.tiers 不能为空' % key)
        for i, tier in enumerate(tiers):
            if not isinstance(tier, dict):
                raise PersonaImportError('relationship.stats.%s.tiers 条目必须是对象' % key)
            if tier.get('min') is None:
                tier['min'] = i * 30
            try:
                tier['min'] = int(tier['min'])
            except (TypeError, ValueError):
                raise PersonaImportError('relationship.stats.%s.tiers.min 必须是整数' % key)
            tier.setdefault('speech', '...')
            tier.setdefault('behavior', '...')

    lore = card.get('lore')
    if not isinstance(lore, dict):
        raise PersonaImportError('card.lore 必须是对象')
    lore.setdefault('header', '【Canon notes】')
    if not str(lore.get('note') or '').strip():
        lore['note'] = 'Your own past. Answer only the point asked; never invent beyond it.'
    entries = lore.get('entries')
    if not isinstance(entries, list):
        raise PersonaImportError('card.lore.entries 必须是数组')
    for entry in entries:
        if not isinstance(entry, dict):
            raise PersonaImportError('card.lore.entries 里的条目必须是对象')
        if not str(entry.get('id') or '').strip():
            raise PersonaImportError('canon 条目缺少 id')
        if not str(entry.get('text') or '').strip():
            raise PersonaImportError('canon 条目 %s 缺少 text' % entry['id'])
        if not isinstance(entry.get('keywords'), list):
            entry['keywords'] = []

    _fill_outfit(card, warnings)
    return card


def _fill_outfit(card, warnings):
    outfit = card.get('outfit')
    default = _default_outfit()
    if not isinstance(outfit, dict):
        card['outfit'] = default
        _warn(warnings, '缺少 outfit，已补通用着装档')
        return
    outfit.setdefault('line', default['line'])
    bands = outfit.get('bands')
    if not isinstance(bands, list) or not bands:
        outfit['bands'] = default['bands']
        _warn(warnings, 'outfit.bands 为空，已补通用档位')
    else:
        for band in bands:
            if not isinstance(band, dict) or not str(band.get('id') or '').strip():
                raise PersonaImportError('outfit.bands 条目缺少 id')
            if not isinstance(band.get('fallback'), list) or not band['fallback']:
                raise PersonaImportError('outfit.bands.%s 缺少 fallback' % band['id'])
            band.setdefault('hint', '')
            band.setdefault('require_any', [])
            band.setdefault('forbid', [])
            band.setdefault('below', None)
    season = outfit.get('season_band')
    if not isinstance(season, dict):
        season = {}
        outfit['season_band'] = season
    for month, band_id in default['season_band'].items():
        season.setdefault(month, band_id)
    if not isinstance(outfit.get('rain_words'), list):
        outfit['rain_words'] = default['rain_words']
    if not isinstance(outfit.get('snow_words'), list):
        outfit['snow_words'] = default['snow_words']
    outfit.setdefault('umbrella', default['umbrella'])


def _normalize_gate(gate, display, label, warnings):
    """发言决策与频率冷却：缺项按 rp_config 默认补齐，数字夹到安全范围。"""
    gate = gate if isinstance(gate, dict) else {}
    out_gate = persona.gate_defaults()
    missing = [k for k in persona.GATE_TUNING_RANGES if k not in gate]
    if missing:
        _warn(warnings, 'gate 缺少发言决策参数（%s%s），已按默认值补齐'
              % ('、'.join(missing[:6]), ' 等' if len(missing) > 6 else ''))
    for key in _GATE_LIST_KEYS:
        if key not in gate:
            continue
        val = gate.get(key)
        if not isinstance(val, (list, tuple)):
            raise PersonaImportError('gate.%s 必须是数组' % key)
        out_gate[key] = _unique(val)
    if not out_gate.get('direct_names'):
        out_gate['direct_names'] = _unique([display, label])
    out_gate.setdefault('interest_words', [])
    out_gate.setdefault('expert_words', [])
    for key, (lo, hi) in persona.GATE_TUNING_RANGES.items():
        if key not in gate:
            continue
        val = _as_float(gate.get(key))
        if val is None:
            raise PersonaImportError('gate.%s 必须是数字' % key)
        val = max(lo, min(hi, val))
        out_gate[key] = int(round(val)) if key in persona.GATE_TUNING_INTS else val
    return out_gate


def _normalize_prompts(prompts, warnings):
    prompts = prompts if isinstance(prompts, dict) else {}
    out_prompts = {}
    for key, val in prompts.items():
        if key not in _PROMPT_KEYS:
            _warn(warnings, '忽略未使用的 prompt：%s' % key)
            continue
        if isinstance(val, str) and val.strip():
            out_prompts[key] = val
    return out_prompts


def _normalize_manifest(manifest, pid, display, label, aliases, warnings):
    group = manifest.get('group') if isinstance(manifest.get('group'), dict) else {}
    try:
        roster = int(group.get('roster_limit') or 5)
    except (TypeError, ValueError):
        roster = 5
    roster = max(1, min(30, roster))

    try:
        bubbles = int(manifest.get('bubble_limit') or 2)
    except (TypeError, ValueError):
        bubbles = 2
    bubbles = max(1, min(10, bubbles))

    out_gate = _normalize_gate(manifest.get('gate'), display, label, warnings)
    out_prompts = _normalize_prompts(manifest.get('prompts'), warnings)
    if manifest.get('qq_profile'):
        _warn(warnings, '导入不包含头像，QQ 档案同步已关闭')
    assets = manifest.get('assets')
    if isinstance(assets, dict) and any(k in assets for k in
                                        ('tts', 'cg', 'corpus', 'novel')):
        _warn(warnings, '导入只含 JSON 文本，TTS/CG/语料/原作索引资产已关闭')

    return {
        'version': int(manifest.get('version') or 1),
        'id': pid,
        'label': label,
        'display_name': display,
        'aliases': aliases,
        'group': {'roster_limit': roster},
        'bubble_limit': bubbles,
        'gate': out_gate,
        'prompts': out_prompts,
    }


def _generic_stats(pid, display):
    return {
        'version': 1,
        'active': pid,
        'characters': {
            pid: {
                'name': display,
                'stats': copy.deepcopy(_DEFAULT_STATS),
                'aversions': [],
                'relationship': {
                    'levels': list(stat_rules.DEFAULT_LEVELS),
                    'floor': copy.deepcopy(_DEFAULT_FLOOR),
                },
            },
        },
    }


def _merge_stats(raw, pid, display, warnings):
    if raw is None:
        _warn(warnings, '缺少 stats，已按关系分档生成通用三项规则')
        return _generic_stats(pid, display)
    stats = copy.deepcopy(raw)
    chars = stats.get('characters')
    if not isinstance(chars, dict) or pid not in chars:
        raise PersonaImportError(
            'stats.characters 里没有角色 %s；禁止借用其他角色的数值规则' % pid)
    others = [k for k in chars if k != pid]
    if others:
        _warn(warnings, 'stats 里的其它角色已忽略：%s' % '、'.join(others))
        stats['characters'] = {pid: chars[pid]}
    if stats.get('active') != pid:
        stats['active'] = pid
        _warn(warnings, 'stats.active 已改为 %s' % pid)

    char = stats['characters'][pid]
    if not isinstance(char, dict):
        raise PersonaImportError('stats.characters.%s 必须是对象' % pid)
    char.setdefault('name', display)
    cur = char.get('stats')
    if not isinstance(cur, dict):
        cur = {}
    for key in _STAT_KEYS:
        base = copy.deepcopy(_DEFAULT_STATS[key])
        item = cur.get(key)
        if not isinstance(item, dict):
            cur[key] = base
            _warn(warnings, 'stats 缺少 %s，已补通用规则' % key)
            continue
        merged = dict(base)
        merged.update(item)
        for table in ('gain_caps', 'gain_caps_big'):
            if not isinstance(merged.get(table), dict):
                merged[table] = base[table]
                _warn(warnings, 'stats.%s.%s 无效，已补默认上限' % (key, table))
            else:
                for level, value in base[table].items():
                    merged[table].setdefault(level, value)
        try:
            down_cap = int(merged.get('down_cap', base['down_cap']))
        except (TypeError, ValueError):
            down_cap = base['down_cap']
        merged['down_cap'] = max(0, down_cap)
        cur[key] = merged
    char['stats'] = cur
    if not isinstance(char.get('aversions'), list):
        char['aversions'] = []
    rel = char.get('relationship')
    if not isinstance(rel, dict):
        rel = {}
    levels = rel.get('levels')
    if levels is None:
        levels = list(stat_rules.DEFAULT_LEVELS)
    if list(levels) != list(stat_rules.DEFAULT_LEVELS):
        raise PersonaImportError(
            'relationship.levels 必须保持系统档位 %s' % '/'.join(stat_rules.DEFAULT_LEVELS))
    rel['levels'] = list(levels)
    if not isinstance(rel.get('floor'), list) or not rel['floor']:
        rel['floor'] = copy.deepcopy(_DEFAULT_FLOOR)
        _warn(warnings, 'relationship.floor 缺失，已补默认门槛')
    char['relationship'] = rel
    try:
        stat_rules._validate(stats)
    except ValueError as e:
        raise PersonaImportError('数值规则不合法：%s' % e)
    return stats


def normalize(payload):
    """把请求体整理成 {id, display_name, manifest, card, stats, warnings}。"""
    if not isinstance(payload, dict):
        raise PersonaImportError('导入内容必须是 JSON 对象')
    package = payload.get('package')
    if isinstance(package, dict):
        merged = dict(package)
        merged.update({k: v for k, v in payload.items() if k != 'package'})
        payload = merged

    card = payload.get('card')
    if card is None and _is_card(payload):
        card = {k: v for k, v in payload.items() if k not in _CONTROL_KEYS}
    if not isinstance(card, dict):
        raise PersonaImportError('缺少 card（角色卡 JSON）')
    manifest = payload.get('persona') or payload.get('manifest') or {}
    if not isinstance(manifest, dict):
        raise PersonaImportError('persona/manifest 必须是对象')
    stats = payload.get('stats')
    if stats is not None and not isinstance(stats, dict):
        raise PersonaImportError('stats 必须是对象')

    raw_id = payload.get('id') or manifest.get('id') or card.get('character') or ''
    pid = str(raw_id).strip().lower()
    if not pid:
        raise PersonaImportError(
            '缺少角色 id：请在 package.id / persona.id / card.character 里写一个英文 id')
    if not persona.valid_id(pid):
        raise PersonaImportError(
            '角色 id 只能用小写字母、数字、- 和 _，且以字母或数字开头（最长 32 位）：%r'
            % raw_id)

    warnings = []
    static = card.get('static') if isinstance(card.get('static'), dict) else {}
    claiming = str(static.get('Claiming') or '')
    display = str(manifest.get('display_name') or manifest.get('label') or '').strip()
    if not display:
        display = _clean_claiming(claiming) or pid
    label = str(manifest.get('label') or display).strip() or display
    aliases = _unique(list(manifest.get('aliases') or [])
                      + [display, label, _claiming_alias(claiming)])
    if card.get('character') and str(card.get('character')).strip().lower() != pid:
        _warn(warnings, 'card.character 已从 %r 改为 %r'
              % (card.get('character'), pid))

    card = _normalize_card(card, pid, display, warnings)
    manifest = _normalize_manifest(manifest, pid, display, label, aliases, warnings)
    stats = _merge_stats(stats, pid, display, warnings)
    return {'id': pid, 'display_name': display, 'manifest': manifest,
            'card': card, 'stats': stats, 'warnings': warnings}


# ---------- 落盘 ----------

def _write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def _backup(target, pid):
    root = os.path.join(persona.STATE_ROOT, 'backups', 'persona_import')
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    path = os.path.join(root, '%s_%s' % (pid, stamp))
    os.makedirs(path, exist_ok=True)
    for name in _FILES:
        src = os.path.join(target, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(path, name))
    return path


def _rollback(target, existed, backup):
    if existed and backup:
        for name in _FILES:
            src = os.path.join(backup, name)
            dst = os.path.join(target, name)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
            elif os.path.isfile(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
        return
    for name in _FILES:
        path = os.path.join(target, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    try:
        os.rmdir(target)
    except OSError:
        pass


def import_bundle(payload):
    """导入一个角色包；返回摘要。dry_run 只校验不落盘。"""
    bundle = normalize(payload)
    pid = bundle['id']
    overwrite = bool(payload.get('overwrite'))
    activate = bool(payload.get('activate'))
    dry = bool(payload.get('dry_run'))
    target = os.path.join(persona.PERSONAS_ROOT, pid)

    with _LOCK:
        existed = os.path.isdir(target)
        if existed and not overwrite:
            raise PersonaImportConflict(
                '角色 %s 已存在；勾选覆盖后才会替换' % pid)
        if dry:
            return {'ok': True, 'id': pid, 'name': bundle['display_name'],
                    'created': not existed, 'overwritten': False,
                    'activated': False, 'dry_run': True,
                    'warnings': bundle['warnings']}

        backup = _backup(target, pid) if existed else ''
        try:
            os.makedirs(target, exist_ok=True)
            _write_json(os.path.join(target, 'persona.json'), bundle['manifest'])
            _write_json(os.path.join(target, 'card.json'), bundle['card'])
            _write_json(os.path.join(target, 'stats.json'), bundle['stats'])
            persona.forget(pid)
            card_source.forget(pid)
            card_source.render(pid=pid)
            ok, reason = persona.validate(pid, strict=True)
            if not ok:
                raise PersonaImportError('导入后校验失败：%s' % reason)
        except Exception:
            _rollback(target, existed, backup)
            raise

        activated = False
        warnings = list(bundle['warnings'])
        if activate:
            activated, info = persona.activate(pid)
            if not activated:
                _warn(warnings, '导入成功但切换失败：%s' % info)
        return {'ok': True, 'id': pid, 'name': bundle['display_name'],
                'created': not existed, 'overwritten': existed,
                'backup': backup, 'activated': activated,
                'warnings': warnings}


def update_bundle(payload):
    """面板编辑保存：只改清单/卡的可编辑项，资产与 QQ 档案等其余字段原样保留。

    payload: {id, manifest?: {...}, card?: {...}}
    写盘前备份，渲染 system_en.txt 后严格校验；任何一步失败都回滚。
    """
    pid = str(payload.get('id') or '').strip().lower()
    if not persona.valid_id(pid) or not persona.exists(pid):
        raise PersonaImportError('角色不存在：%s' % (pid or '(空)'))
    raw_manifest = payload.get('manifest')
    raw_card = payload.get('card')
    if raw_manifest is None and raw_card is None:
        raise PersonaImportError('没有要保存的内容')
    if raw_manifest is not None and not isinstance(raw_manifest, dict):
        raise PersonaImportError('manifest 必须是对象')
    if raw_card is not None and not isinstance(raw_card, dict):
        raise PersonaImportError('card 必须是对象')

    target = os.path.join(persona.PERSONAS_ROOT, pid)
    warnings = []
    with _LOCK:
        cur = persona.manifest(pid)
        manifest = copy.deepcopy(cur)
        if raw_manifest is not None:
            for key in ('label', 'display_name'):
                if key in raw_manifest:
                    val = str(raw_manifest.get(key) or '').strip()
                    if not val:
                        raise PersonaImportError('清单 %s 不能为空' % key)
                    manifest[key] = val
            if 'aliases' in raw_manifest:
                aliases = raw_manifest.get('aliases')
                if not isinstance(aliases, (list, tuple)):
                    raise PersonaImportError('aliases 必须是数组')
                manifest['aliases'] = _unique(aliases)
            group = raw_manifest.get('group')
            if isinstance(group, dict) and 'roster_limit' in group:
                try:
                    roster = int(group.get('roster_limit'))
                except (TypeError, ValueError):
                    raise PersonaImportError('group.roster_limit 必须是整数')
                manifest.setdefault('group', {})['roster_limit'] = max(1, min(30, roster))
            if 'bubble_limit' in raw_manifest:
                try:
                    bubbles = int(raw_manifest.get('bubble_limit'))
                except (TypeError, ValueError):
                    raise PersonaImportError('bubble_limit 必须是整数')
                manifest['bubble_limit'] = max(1, min(10, bubbles))
            if 'gate' in raw_manifest:
                display = manifest.get('display_name') or pid
                manifest['gate'] = _normalize_gate(
                    raw_manifest.get('gate'), display,
                    manifest.get('label') or display, warnings)
            if 'prompts' in raw_manifest:
                manifest['prompts'] = _normalize_prompts(
                    raw_manifest.get('prompts'), warnings)

        display = manifest.get('display_name') or pid
        card = None
        if raw_card is not None:
            card = copy.deepcopy(raw_card)
            card['character'] = pid
            card = _normalize_card(card, pid, display, warnings)

        backup = _backup(target, pid)
        try:
            _write_json(os.path.join(target, 'persona.json'), manifest)
            if card is not None:
                _write_json(os.path.join(target, 'card.json'), card)
            persona.forget(pid)
            card_source.forget(pid)
            card_source.render(pid=pid)
            ok, reason = persona.validate(pid, strict=True)
            if not ok:
                raise PersonaImportError('保存后校验失败：%s' % reason)
        except Exception:
            _rollback(target, True, backup)
            raise
        return {'ok': True, 'id': pid, 'name': display,
                'backup': backup, 'warnings': warnings}
