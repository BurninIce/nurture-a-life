# -*- coding: utf-8 -*-
"""书架的模型调用：口味、挑书、读书笔记。

挑书和写笔记都用对话模型，system 直接是角色卡渲染出的 system_en.txt，
保证选书口味和读书笔记都按她的人设来，不走判官的冷读法。
"""
import json
import logging
import re

import card_source
import persona
import rp_config as cfg

LOG = logging.getLogger('bookshelf')

DEFAULT_PACE = 400
MIN_ABANDON_PROGRESS = 0.03


def _system():
    try:
        with open(persona.system_path(), encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        try:
            return card_source.render_static()[:6000]
        except Exception:
            return ''


def _chat(user, max_tokens=cfg.BOOKSHELF_NOTE_MAX_TOKENS,
          temperature=cfg.BOOKSHELF_NOTE_TEMPERATURE):
    messages = []
    system = _system()
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': user})
    try:
        from rp_common import chat_model
        return chat_model('aux', messages, temperature=temperature,
                          max_tokens=max_tokens) or ''
    except Exception:
        LOG.exception('书架模型调用失败')
        return ''


def _json(raw):
    m = re.search(r'\{.*\}', str(raw or ''), re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def reading_config():
    try:
        card = card_source.load()
    except Exception:
        return {}
    row = card.get('reading') if isinstance(card, dict) else None
    return row if isinstance(row, dict) else {}


def pace_chars(lang='zh'):
    pace = reading_config().get('pace_chars_per_min') or DEFAULT_PACE
    if isinstance(pace, dict):
        pace = pace.get(lang) or pace.get('zh') or DEFAULT_PACE
    try:
        return max(60, int(pace))
    except (TypeError, ValueError):
        return DEFAULT_PACE


def taste_hint():
    row = reading_config()
    genres = row.get('genres') or []
    note = str(row.get('note') or '').strip()
    parts = []
    if genres:
        parts.append('平时偏爱：' + '、'.join(str(g) for g in genres[:6]))
    if note:
        parts.append(note)
    return '；'.join(parts)


def taste(now=None, extra=''):
    """让对话模型给一段当下想读的口味，用作本地索引查询。"""
    hint = taste_hint()
    prompt = (
        '现在是她自己的时间，书架要补货了。她按自己的口味挑下一批想读的书。\n'
        + (('她的长期阅读偏好：%s\n' % hint) if hint else '')
        + (('额外线索：%s\n' % extra) if extra else '')
        + '用一两句话说清楚她现在想读什么题材、什么气质、想避开什么。'
        '只输出 JSON：{"taste": "…"}'
    )
    raw = _chat(prompt, max_tokens=160, temperature=0.9)
    got = str(_json(raw).get('taste') or '').strip()
    return got or hint or '文学、小说、人物故事'


def _render_pool(pool, limit=16):
    lines = []
    for i, row in enumerate(pool[:limit], 1):
        title = str(row.get('title') or '').strip() or row.get('id')
        author = str(row.get('author') or '').strip()
        lines.append('%d. %s%s' % (i, title, (' / ' + author) if author else ''))
    return '\n'.join(lines)


def _pick_ids(pool, raw, want):
    obj = _json(raw)
    got = obj.get('ids') or obj.get('numbers') or obj.get('titles') or []
    out = []
    by_title = {str(r.get('title') or ''): r.get('id') for r in pool}
    by_id = {str(r.get('id')): r.get('id') for r in pool}
    for item in got if isinstance(got, list) else []:
        key = str(item).strip()
        hit = by_id.get(key) or by_title.get(key)
        if not hit and key.isdigit():
            n = int(key)
            if 1 <= n <= len(pool):
                hit = pool[n - 1].get('id')
        if hit:
            out.append(hit)
    seen, unique = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            unique.append(x)
    return unique[:want]


def pick_candidates(taste_text, pool, want):
    """从候选池里按口味提名 want 本；只认池内编号，失败返回空。"""
    if not pool:
        return []
    body = _render_pool(pool)
    prompt = (
        (('她现在想读：%s\n\n' % taste_text) if taste_text else '')
        + '下面是可以选的书（编号. 书名 / 作者）：\n%s\n\n' % body
        + '挑 %d 本最符合她现在口味的，只能从上面的编号里选，书名必须原样。\n' % want
        + '只输出 JSON：{"ids": [编号, …]}'
    )
    raw = _chat(prompt, max_tokens=120, temperature=0.7)
    got = _pick_ids(pool, raw, want)
    if got:
        return got
    raw = _chat(prompt + '\n上一次没有选出有效编号，请只回编号。',
                max_tokens=100, temperature=0.4)
    got = _pick_ids(pool, raw, want)
    if not got:
        LOG.info('模型挑书无有效结果：%s', str(raw)[:80])
    return got


def choose(taste_text, candidates, need):
    """从 6 本候选里最终挑 need 本进未读；失败返回空。"""
    if not candidates or need <= 0:
        return []
    body = _render_pool(candidates, limit=len(candidates))
    prompt = (
        (('她现在想读：%s\n\n' % taste_text) if taste_text else '')
        + '书架只剩这些候选（编号. 书名 / 作者）：\n%s\n\n' % body
        + '挑 %d 本放进她的未读书架。只能选编号，书名必须原样。\n' % need
        + '只输出 JSON：{"ids": [编号, …]}'
    )
    raw = _chat(prompt, max_tokens=120, temperature=0.7)
    got = _pick_ids(candidates, raw, need)
    if not got:
        raw = _chat(prompt + '\n请只回编号，不要解释。', max_tokens=100,
                    temperature=0.4)
        got = _pick_ids(candidates, raw, need)
    if not got:
        LOG.info('模型最终挑书无有效结果：%s', str(raw)[:80])
    return got


def read_note(book, previous, excerpt, progress, session_chars, minutes):
    """写一段人设化的读书笔记，并顺带判断还想不想读。"""
    title = str(book.get('title') or '').strip() or '这本书'
    author = str(book.get('author') or '').strip()
    lang = str(book.get('lang') or '').strip()
    head = '你正在读《%s》' % title
    if author:
        head += '（%s' % author + ('，%s' % lang if lang else '') + '）'
    else:
        head += ('（%s）' % lang if lang else '')
    prev = str(previous or '').strip()
    prompt = (
        head + '。\n'
        + ('上次读到：%s\n' % prev if prev else '这是你第一次翻开这本书。\n')
        + '这次读了大约 %d 字，现在是 %d%%。\n' % (session_chars, int(progress * 100))
        + '这次读到的片段（节选，不是全部）：\n%s\n\n' % excerpt
        + '以你自己的口吻写 2 到 3 句读书笔记，写你的感受和判断，'
          '不要抄原文，不要写剧情梗概，也不要像书评。\n'
        + '再写一句你现在最想弄清楚的问题，像是会在聊天里随口问出来的。\n'
        + '如果你已经不想继续读下去，把 abandon 设为 true；'
          '如果还想读，设为 false。\n'
        + '只输出 JSON：{"note": "…", "curiosity": "…", '
          '"interest": 0.0, "abandon": false}'
    )
    raw = _chat(prompt, max_tokens=cfg.BOOKSHELF_NOTE_MAX_TOKENS,
                temperature=cfg.BOOKSHELF_NOTE_TEMPERATURE)
    obj = _json(raw)
    note = str(obj.get('note') or '').strip()
    curiosity = str(obj.get('curiosity') or '').strip()
    try:
        interest = float(obj.get('interest'))
    except (TypeError, ValueError):
        interest = 0.5
    abandon = bool(obj.get('abandon')) and progress >= MIN_ABANDON_PROGRESS
    if not note:
        note = '读了一会儿《%s》，没留下什么特别的感受。' % title
    return {'note': note[:240], 'curiosity': curiosity[:60],
            'interest': max(0.0, min(1.0, interest)), 'abandon': abandon}
