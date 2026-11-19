# -*- coding: utf-8 -*-
"""角色资产目录里的 CG 身份层。

每个角色的 card.json 只放对话事实，CG 专用字段（身份段落、眼锁、默认城市、
分阶段服装）放同目录 cg.json。CG 代码只负责机位/光色/动作/风格模板，
不写死名字、发色、瞳色或服装。示例见 prompts/personas/character/cg.json。

切换角色卡：默认读 prompts/personas/character/card.json；设环境变量
NAL_CARD 为角色 id（alt）或路径即可，也可以每次调用传
card='personas/alt/card.json'。
"""
import json
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CARD = 'character'
STAGES = ('相识', '熟络', '亲密', '倾心')

REQUIRED_FIELDS = (
    'name_zh', 'city_zh', 'eye_lock_zh', 'identity_zh', 'identity_zh_back',
    'back_focus_zh', 'back_subject_zh',
)

_lock = threading.Lock()
_cache = {}


def resolve(card=None):
    """角色 id 或路径 → 绝对路径。空值走 NAL_CARD，再退默认卡。"""
    name = str(card or os.environ.get('NAL_CARD') or DEFAULT_CARD).strip()
    if os.path.isabs(name):
        return name
    if os.sep in name or '/' in name:
        return os.path.join(ROOT, name)
    bare = name[:-5] if name.endswith('.json') else name
    if bare.endswith('_card'):
        bare = bare[:-5]
    return os.path.join(ROOT, 'prompts', 'personas', bare, 'card.json')


def load(card=None):
    """读并校验 cg 段；同一文件按 mtime 缓存。"""
    path = resolve(card)
    # 资产目录化后卡名为 card.json，CG 配置单独放同目录 cg.json。
    sidecar = os.path.join(os.path.dirname(path), 'cg.json')
    use_sidecar = os.path.basename(path) == 'card.json' and os.path.exists(sidecar)
    mtime = os.path.getmtime(path)
    if use_sidecar:
        mtime = max(mtime, os.path.getmtime(sidecar))
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    block = data.get('cg')
    if use_sidecar:
        with open(sidecar, encoding='utf-8') as f:
            block = json.load(f)
    if not isinstance(block, dict):
        raise ValueError('%s 缺少 cg 段' % os.path.basename(path))
    missing = [k for k in REQUIRED_FIELDS if not str(block.get(k) or '').strip()]
    outfits = block.get('outfits_zh')
    if not isinstance(outfits, dict):
        missing.append('outfits_zh')
        outfits = {}
    missing.extend('outfits_zh.' + s for s in STAGES
                   if not str(outfits.get(s) or '').strip())
    if missing:
        raise ValueError('%s 的 cg 段缺少：%s'
                         % (os.path.basename(path), ', '.join(missing)))
    profile = dict(block)
    profile['outfits_zh'] = {s: str(outfits[s]).strip() for s in STAGES}
    profile['path'] = path
    profile['character'] = str(data.get('character') or '').strip()
    with _lock:
        _cache[path] = (mtime, profile)
    return profile
