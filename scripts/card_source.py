# -*- coding: utf-8 -*-
"""角色卡事实源：由 persona 解析当前角色的 card.json。

    static        固定卡字段 → 渲染成 system_en.txt（所有模型共用）
    relationship  好感/信任/熟悉度分档 + 组合桥接句 → 每轮按当前说话人注入
    lore          详细背景与条目 → lore_rag 按关键词/向量检索后注入

模型 key 不再参与选卡：她是谁由 runtime_config.character 决定。
角色卡统一放 prompts/personas/<id>/card.json。

改卡流程：编辑 JSON → `.venv/Scripts/python.exe scripts/card_source.py --render`
`--check` 只校验 txt 与 JSON 一致（自测用），不写文件。
"""
import argparse
import json
import os
import sys
import threading

import persona
import rp_config

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_lock = threading.Lock()
_cache = {}   # path -> (mtime, data)


def source_path(pid=None):
    return persona.card_json_path(pid)


def system_path(pid=None):
    return persona.system_path(pid)


def forget(pid=None):
    """丢掉某角色卡的读取缓存（覆盖导入后立刻读到新文件）。"""
    path = source_path(pid)
    with _lock:
        _cache.pop(path, None)


def load(path=None, pid=None):
    """读当前角色的卡 JSON（按 path+mtime 热加载）。"""
    path = path or source_path(pid)
    mtime = os.path.getmtime(path)
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    with _lock:
        _cache[path] = (mtime, data)
    return data


def _render_value(key, value, indent=''):
    if isinstance(value, dict):
        lines = ['%s%s:' % (indent, key)]
        for k, v in value.items():
            lines.extend(_render_value(k, v, indent + '  '))
        return lines
    if isinstance(value, list):
        return ['%s%s:' % (indent, key)] + ['%s  - %s' % (indent, v) for v in value]
    return ['%s%s: %s' % (indent, key, value)]


def render_static(data=None):
    data = data or load()
    lines = []
    for key, value in data['static'].items():
        lines.extend(_render_value(key, value))
    return data['preamble'] + '\n\n' + '\n'.join(lines) + '\n'


# ---------- 关系分档 ----------

def _tier_index(tiers, value):
    idx = 0
    for i, tier in enumerate(tiers):
        if value >= tier['min']:
            idx = i
    return idx


def tiers_for(stats, group=False, data=None):
    """{'familiarity': 1..4, 'affection': 1..3, 'trust': 1..3}（1 起）。群聊按 group_cap 封顶。"""
    data = data or load()
    out = {}
    for stat in data['relationship']['stats']:
        try:
            value = int(stats.get(stat['key'], 0))
        except (TypeError, ValueError):
            value = 0
        level = _tier_index(stat['tiers'], value) + 1
        if group and stat.get('group_cap'):
            level = min(level, stat['group_cap'])
        out[stat['key']] = level
    return out


def relation_text(stats, group=False, data=None):
    """注入给模型的关系块：只有言行指引，不含数值与关系名。"""
    data = data or load()
    rel = data['relationship']
    levels = tiers_for(stats, group=group, data=data)
    lines = [rel['header']]
    for stat in rel['stats']:
        tier = stat['tiers'][levels[stat['key']] - 1]
        parts = []
        if tier.get('speech'):
            parts.append('Speech: ' + tier['speech'])
        if tier.get('behavior'):
            parts.append('Behavior: ' + tier['behavior'])
        if parts:
            lines.append('%s — %s' % (stat['label'], ' '.join(parts)))
    seen = set()
    for bridge in rel['bridges']:
        if all(levels.get(k) in v for k, v in bridge['when'].items()):
            if bridge['text'] not in seen:
                seen.add(bridge['text'])
                lines.append('Balance: ' + bridge['text'])
    if group and rel.get('group_note'):
        lines.append(rel['group_note'])
    return '\n'.join(lines)


def lore_entries(data=None):
    return (data or load())['lore']['entries']


def header_prefixes(data=None):
    """本轮可能注入的块标题：模型偶尔会原样复读在回复开头，需先剥掉。"""
    data = data or load()
    return (data['lore']['header'], data['relationship']['header'],
            rp_config.GROUP_NOTE_HEADER)


# ---------- CLI ----------

def check(pid=None, data=None):
    """返回与 JSON 不一致的卡文件名列表。"""
    text = render_static(data if data is not None else load(pid=pid))
    path = system_path(pid)
    try:
        with open(path, encoding='utf-8') as f:
            if f.read() == text:
                return []
    except OSError:
        pass
    return [os.path.basename(path)]


def render(pid=None, data=None):
    text = render_static(data if data is not None else load(pid=pid))
    path = system_path(pid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    os.replace(tmp, path)
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--render', action='store_true', help='把 JSON 渲染到当前角色的 system 文件')
    ap.add_argument('--check', action='store_true', help='只校验，不写文件')
    ap.add_argument('--persona', help='角色 id（默认 runtime_config.character）')
    args = ap.parse_args()
    pid = args.persona or persona.active_id()
    if args.render:
        text = render(pid=pid)
        print('rendered %d chars -> %s' % (len(text), os.path.basename(system_path(pid))))
        return 0
    bad = check(pid=pid)
    if bad:
        print('out of date: ' + ', '.join(bad))
        return 1
    print('cards up to date')
    return 0


if __name__ == '__main__':
    sys.exit(main())
