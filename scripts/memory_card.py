# -*- coding: utf-8 -*-
"""
角色记忆系统核心（事件溯源：事件库为原始载体，记忆卡为归纳层）。

- 事件库（episodes）：不可变原始互动记录，追加不修改（事件溯源 event sourcing）
- 话题缓冲（pending）：未闭合的对话轮次暂存，边界判定 LLM 检测话题结束才整段提交
  为一条事件（话题边界闭合——LLM 自己规划事件，跨轮经历不碎片化）
- 记忆卡（card）：从事件库**重归纳**的派生字段（affection/trust/familiarity/relationship +
  impressions/facts/self_disclosure）——数值不引导，故事引导
- 检索：每轮按用户发言检索相关事件（关键词+标签+近因+importance 打分，Top-K）
- 遗忘：importance=1 且超过 FORGET_DAYS 天未互动 → 淡出（inactive）；importance=3 永久
- 持久化：state/personas/<id>/memory_card.json（已 gitignore）
- 多用户：set_state_dir()/state_scope() 可把状态目录切到
  state/personas/<id>/users/<uid>/（QQ 侧每用户一份）；
  默认目录跟随当前角色（default_state_dir 每次现算，set_state_dir(None)
  会落到当前角色根目录；直接给 STATE_PATH 赋值同样生效）

注意：不在本模块包装 stdout（rp_gui 已包装，重复包装报 closed file）。
"""
import contextlib
import logging
import datetime
import json
import os
import re
import shutil
import threading

from rp_common import stream_chat
import hw_profile as hw
import logutil
import rp_config as cfg
import stat_rules
from memory_util import (
    RECALL_K, RECALL_THRESHOLD, MEMORY_INTENT_THRESHOLD, MEMORY_INTENT_PROTO,
    RECALL_INTENT_THRESHOLD, RECALL_RECENCY_WEIGHT, RECALL_FAVORITE_WEIGHT,
    _parse_time, time_label, episode_time, is_scene_status,
    _live_supersedes, superseded_ep_ids, superseded_map, item_expired,
)
from memory_retrieval import retrieve
from memory_format import (_person, format_card, relation_block,
                           recall_block, _fill_when)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _persona_state_dir():
    try:
        import persona
        return persona.state_root()
    except Exception:
        return os.path.join(ROOT_DIR, 'state', 'personas', 'character')


DEFAULT_STATE_DIR = _persona_state_dir()   # 导入时快照（保留给外部引用）
_STATE_DIR = DEFAULT_STATE_DIR
STATE_PATH = os.path.join(_STATE_DIR, 'memory_card.json')
DEBUG_LOG_PATH = os.path.join(_STATE_DIR, 'debug_memory.jsonl')


def default_state_dir():
    """当前角色的默认记忆目录（每次现算，切角色后不会残留旧指针）。"""
    return _persona_state_dir()
DEBUG_LOG_MAX_LINES = 2000  # 超过则轮转：保留最近 1000 行

# 运行态文件结构版本。缺 version 视为 0（旧文件）；迁移在 load 时**只在内存里**执行，
# 下次 save 才落盘，读路径保持无副作用。
SCHEMA_VERSION = 1
# {目标版本: fn(data)}：从旧版本跨到该版本时执行一次（幂等）。
# 约定：只补字段/规整结构，绝不改事件、卡片、数值——迁移前后 RP 行为必须一致。
_MIGRATIONS = {}

MODEL = 'qwen3.5-9b:latest'
REINDUCT_EVERY = 5   # 每 N 轮互动重归纳
FORGET_DAYS = 30     # importance=1 且超过 N 天未互动 → 淡出
BUFFER_CAP = 8       # 话题缓冲达到该轮数 → 强制闭合（绑定缓冲增长）
BUFFER_HARD_MAX = 16  # LLM 持续失败时缓冲上限，超出无条件丢弃（最后兜底）
BUFFER_IDLE_SEC = 30 * 60  # 缓冲静默超过该时长 → 新消息进来前先结掉旧话题

RELATIONSHIPS = ['陌生', '相识', '熟络', '亲密', '倾心']

# 三项数值的涨/扣分上限、关系档位门槛、角色明确反感的行为，全部在
# prompts/personas/<id>/stats.json（经 stat_rules 读取）。这里不再写死任何角色的偏好：
# 换角色卡时只改那份配置，代码只负责执行限幅与反感结算。

# 归纳输出的长度上限。卡片越满，输出越长（条目每类最多 3 条 + 反思 3 条 + 取代链）。
# **400 会把满卡的输出截断**：10 条条目的卡实测要 1100+ 字符，
# 截断后 JSON 没有收尾的 `}` → 匹配失败 → 整次归纳静默丢弃，
# 表现为「关系数值永远不涨」。宁可多花 token，也不能让归纳悄悄不干活。
INDUCT_PREDICT = 1200
INDUCT_HISTORY_MAX = 30  # 数值变化历史保留条数（每条对应一次归纳）

_LOG = logging.getLogger('memory_card')

# 状态目录锁：state_scope 内全程持有（可重入），保证同一目录写盘不交叉。
# 按目录分开锁：用户 A 的归纳（含 LLM 调用）不该挡住用户 B 的读取/写入。
_STATE_LOCK = threading.RLock()          # 默认 state/ 目录（保留旧名字给外部引用）
_DIR_LOCKS = {}
_DIR_LOCKS_GUARD = threading.Lock()
_LOCAL = threading.local()               # 本线程生效的目录覆盖，不再改全局


def _lock_for(d):
    # 归一化：同一目录用不同写法（大小写/相对路径/尾分隔符）也必须命中同一把锁
    key = os.path.normcase(os.path.abspath(d))
    if key == os.path.normcase(DEFAULT_STATE_DIR):
        return _STATE_LOCK
    with _DIR_LOCKS_GUARD:
        lk = _DIR_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _DIR_LOCKS[key] = lk
        return lk


def _cur_path():
    """本线程当前生效的记忆文件路径（state_scope 内为覆盖值）。"""
    return getattr(_LOCAL, 'path', None) or STATE_PATH


def _cur_debug_path():
    return getattr(_LOCAL, 'debug', None) or DEBUG_LOG_PATH


def state_dir():
    """当前生效的状态目录（默认角色根，多用户时其 users/<uid>/）。"""
    return getattr(_LOCAL, 'dir', None) or _STATE_DIR


def set_state_dir(path=None):
    """切换记忆文件所在目录（多用户隔离用）。path 为空 → 回到当前角色默认目录。

    返回切换后的目录。这是**进程级**切换（测试/GUI 单用户场景用）；
    多用户并发请用 state_scope()，它只影响当前线程。
    """
    global _STATE_DIR, STATE_PATH, DEBUG_LOG_PATH
    d = path or default_state_dir()
    _STATE_DIR = d
    STATE_PATH = os.path.join(d, 'memory_card.json')
    DEBUG_LOG_PATH = os.path.join(d, 'debug_memory.jsonl')
    return d


@contextlib.contextmanager
def state_scope(path=None):
    """在指定状态目录内执行（退出时还原），并全程持该目录的锁。

    path 为空时只加锁、不换目录（GUI 单用户路径）。目录覆盖走 thread-local：
    WS 线程（对话/记忆）与管理面板互不干扰，也不再需要保存/还原全局路径。
    """
    if path:
        d = path
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        p = os.path.join(d, 'memory_card.json')
        dbg = os.path.join(d, 'debug_memory.jsonl')
    else:
        d, p, dbg = state_dir(), None, None
    with _lock_for(d):
        names = ('dir', 'path', 'debug')
        prev = tuple(getattr(_LOCAL, k, None) for k in names)
        if path:
            _LOCAL.dir, _LOCAL.path, _LOCAL.debug = d, p, dbg
        try:
            yield state_dir()
        finally:
            for k, v in zip(names, prev):
                if v is None:
                    if hasattr(_LOCAL, k):
                        delattr(_LOCAL, k)
                else:
                    setattr(_LOCAL, k, v)


def now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M')


def _empty():
    return {
        'schema_version': SCHEMA_VERSION,
        'interaction_count': 0,
        'episodes': [],
        'pending': [],  # 话题缓冲：未闭合的对话轮次 [{user, reply, time}]（边界闭合前暂存）
        # 取代链：[{'new': 新事件id, 'old': 被取代的事件id, 'at': 时间}]
        # **存事件 id，不存卡片条目序号**——卡片每轮重归纳整组重建、序号不稳定；
        # 事件库不可变、id 永久稳定，才是唯一可靠锚点。放 data 顶层而非 card 内，
        # 否则会被 induct 的整组覆盖冲掉。
        'supersedes': [],
        'card': {
            'affection': 30,
            'trust': 30,
            'familiarity': 0,
            'relationship': '相识',
            'impressions': [],
            'facts': [],
            'self_disclosure': [],
            'reflections': [],
        },
        'last_induction_round': 0,
        # 每次归纳的数值变化（供 UI 展示）。episode_ids 是这次归纳**首次纳入**的事件；
        # 系统只到批次粒度，无法把一次变化拆到单个事件上。
        'inductions': [],
        # 已纳入过归纳的最大事件 id：用于判断下次哪些事件是"新素材"。
        # 旧数据在 load() 里补成当前最大 id，避免历史事件被误标成新批次。
        'last_induct_episode_id': 0,
        # 已结算过的人设反感：[{'id': 反感id, 'episode_ids': [事件id], 'at': 时间}]。
        # 重归纳每 5 轮会重看最近 20 条事件，按事件记账才能保证同一次冒犯只扣一次。
        'applied_aversions': [],
    }


def _schema_of(data):
    try:
        return int(data.get('schema_version') or 0)
    except (TypeError, ValueError):
        return 0


def migrate(data):
    """就地升级到 SCHEMA_VERSION（幂等；更高版本原样返回，不降级）。"""
    v = _schema_of(data)
    if v > SCHEMA_VERSION:
        return data
    for target in range(v + 1, SCHEMA_VERSION + 1):
        fn = _MIGRATIONS.get(target)
        if fn is not None:
            fn(data)
        data['schema_version'] = target
    return data


def load():
    """读记忆状态。损坏/缺字段时容错：损坏现场先保留副本，再以空结构补齐返回。"""
    try:
        with open(_cur_path(), encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as e:
        backup = _preserve_corrupt()
        _LOG.error('记忆文件不可读，返回空结构（现场：%s）：%s',
                   backup or '无备份', e)
        return _empty()
    if not isinstance(data, dict) or 'episodes' not in data or 'card' not in data:
        backup = _preserve_corrupt()
        _LOG.error('记忆结构非法（缺 episodes/card），返回空结构（现场：%s）',
                   backup or '无备份')
        return _empty()
    # 字段补全：以 _empty 为底合并（旧版/手改文件缺字段不 KeyError）
    base = _empty()
    card = dict(base['card'])
    card.update(data.get('card') or {})
    data['card'] = card
    data.setdefault('interaction_count', 0)
    data.setdefault('last_induction_round', 0)
    data.setdefault('pending', [])
    if not isinstance(data['pending'], list):
        data['pending'] = []
    data.setdefault('supersedes', [])
    if not isinstance(data['supersedes'], list):
        data['supersedes'] = []
    data.setdefault('inductions', [])
    if not isinstance(data['inductions'], list):
        data['inductions'] = []
    data.setdefault('applied_aversions', [])
    if not isinstance(data['applied_aversions'], list):
        data['applied_aversions'] = []
    if 'last_induct_episode_id' not in data:
        # 旧数据没有这个标记：把现有事件都视为"已结算"，只记录之后的归纳。
        data['last_induct_episode_id'] = max(
            [_as_int(e.get('id'), 0) for e in data.get('episodes') or []], default=0)
    return migrate(data)


def debug_log(entry):
    """追加一条调试记录到当前状态目录的 debug_memory.jsonl（JSONL，失败静默）。

    entry: dict（如每轮三值快照、事件提取结果、重归纳前后变化）
    超 DEBUG_LOG_MAX_LINES 行自动轮转：保留最近一半，防无限增长。
    """
    try:
        entry['ts'] = now_str()
        lines = []
        path = _cur_debug_path()
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                lines = f.readlines()
        if len(lines) >= DEBUG_LOG_MAX_LINES:
            lines = lines[-DEBUG_LOG_MAX_LINES // 2:]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(lines)
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except (OSError, ValueError):
        pass


def _preserve_corrupt():
    """JSON 损坏时把现场存为 backups/memory_card_corrupt_*.json（防后续 save 覆盖，留恢复可能）。

    返回备份路径；没有可备份的文件时返回 None。
    """
    try:
        path = _cur_path()
        if os.path.exists(path) and os.path.getsize(path) > 0:
            ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_dir = os.path.join(os.path.dirname(path), 'backups')
            os.makedirs(backup_dir, exist_ok=True)
            dest = os.path.join(backup_dir, 'memory_card_corrupt_%s.json' % ts)
            shutil.copy2(path, dest)
            return dest
    except OSError:
        pass
    return None


def save(data):
    """原子写：先写临时文件再 os.replace（中途崩溃不破坏原文件）。"""
    out = migrate(dict(data))
    path = _cur_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def backup():
    """手动归档当前记忆（清空记忆前调用）到 state/backups/memory_card_manual_<时间戳>.json。

    旧记忆不删除；manual 归档**永不自动清理**（用户"清空不删除"承诺）。
    返回归档路径；无记忆文件返回 None。
    """
    path = _cur_path()
    if not os.path.exists(path):
        return None
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = os.path.join(os.path.dirname(path), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, 'memory_card_manual_%s.json' % ts)
    n = 1
    while os.path.exists(dst):  # 同秒多次清空防覆盖
        n += 1
        dst = os.path.join(backup_dir, 'memory_card_manual_%s_%d.json' % (ts, n))
    shutil.copy2(path, dst)
    return dst


AUTO_BACKUP_EVERY = 20   # 每 N 轮互动滚动备份一次
MAX_ROLL_BACKUPS = 20    # 滚动备份最多保留份数
MAX_DAILY_BACKUPS = 14   # 每日快照最多保留天数


def auto_backup(data=None):
    """定期自动备份：每日快照（当天首份）+ 每 AUTO_BACKUP_EVERY 轮滚动备份。

    data: 记忆状态 dict；传 None 时只做每日快照（GUI 启动时用）。
    只清理 daily_/roll_ 自动备份；manual_/corrupt_ 永不自动删。
    返回本次创建的备份路径（无则 None）；失败静默（不阻塞对话）。
    """
    path = _cur_path()
    if not os.path.exists(path):
        return None
    backup_dir = os.path.join(os.path.dirname(path), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    created = None
    # 每日快照：当天还没有 → 快照一份（保护上次会话积累的数据）
    daily_name = 'memory_card_daily_%s.json' % datetime.date.today().strftime('%Y%m%d')
    daily_path = os.path.join(backup_dir, daily_name)
    if not os.path.exists(daily_path):
        shutil.copy2(path, daily_path)
        created = daily_path
    # 滚动备份：interaction_count 恰为 N 的倍数时
    if data is not None:
        n = data.get('interaction_count', 0)
        if n > 0 and n % AUTO_BACKUP_EVERY == 0:
            ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            roll_path = os.path.join(backup_dir, 'memory_card_roll_%s.json' % ts)
            if not os.path.exists(roll_path):
                shutil.copy2(path, roll_path)
                created = created or roll_path
    _prune_auto(backup_dir)
    return created


def _prune_auto(backup_dir):
    """清理自动备份：roll_ 保留最近 MAX_ROLL_BACKUPS 份、daily_ 保留 MAX_DAILY_BACKUPS 份。"""
    try:
        for prefix, keep in (('memory_card_roll_', MAX_ROLL_BACKUPS),
                             ('memory_card_daily_', MAX_DAILY_BACKUPS)):
            files = sorted(
                (f for f in os.listdir(backup_dir) if f.startswith(prefix)),
                key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)))
            for f in files[:-keep]:
                try:
                    os.remove(os.path.join(backup_dir, f))
                except OSError:
                    pass
    except OSError:
        pass


def rollback_day(day=None, *, dry_run=False):
    """回滚某一天新增的记忆：删当天的事件/话题缓冲/归纳记录，并撤销当天归纳的数值改动。

    day: 'YYYY-MM-DD'，缺省今天。dry_run 只算不改（面板预览用）。

    约束与近似：
      - 当天事件必须是事件库末尾连续的一段；删中间会让 id 与卡片 src 引用错位，直接拒绝；
      - 数值回滚到当天第一次归纳的 before，没有归纳就不动；
      - interaction_count 按「删掉的事件 + 删掉的缓冲」回退，归纳轮次夹到计数以内。
        当天被丢弃的缓冲（debug 里 action=dropped）不留痕，无法计入——计数可能略高，
        只影响重归纳节奏，不影响记忆内容；
      - 只引用了被删事件的卡片条目一并删除，否则事件没了条目还在，等于没忘。
    改前自动归档到 backups/memory_card_manual_<时间戳>.json（永不自动清理）。
    """
    day = day or datetime.date.today().strftime('%Y-%m-%d')
    data = load()
    eps = data.get('episodes') or []
    pend = data.get('pending') or []
    inds = data.get('inductions') or []
    rm_eps = [e for e in eps if str(e.get('time') or '').startswith(day)]
    rm_pend = [p for p in pend if str(p.get('time') or '').startswith(day)]
    rm_ind = [i for i in inds if str(i.get('ts') or '').startswith(day)]
    card = data.get('card') or {}
    stat_keys = ('affection', 'trust', 'familiarity', 'relationship')

    report = {
        'day': day,
        'episodes': [{'id': e.get('id'), 'time': e.get('time') or '',
                      'summary': e.get('summary') or ''} for e in rm_eps],
        'pending': len(rm_pend),
        'inductions': len(rm_ind),
        'card_items': 0,
        'removed': len(rm_eps) + len(rm_pend) + len(rm_ind),
        'count_before': data.get('interaction_count'),
        'count_after': data.get('interaction_count'),
        'card_before': {k: card.get(k) for k in stat_keys},
        'card_after': {k: card.get(k) for k in stat_keys},
        'backup': '',
    }
    if not report['removed']:
        return report

    if rm_eps:
        tail = eps[len(eps) - len(rm_eps):]
        if [e.get('id') for e in tail] != [e.get('id') for e in rm_eps]:
            raise ValueError('当天事件不在事件库末尾，回滚会打乱事件 id 与卡片引用')

    if rm_ind:
        first = min(rm_ind, key=lambda x: str(x.get('ts') or ''))
        before = first.get('before') or {}
        for k in stat_keys:
            if k in before:
                card[k] = before[k]

    gone = {e.get('id') for e in rm_eps}
    for key in ('impressions', 'facts', 'self_disclosure', 'reflections'):
        items = card.get(key) or []
        keep = []
        for it in items:
            refs = set()
            if isinstance(it, dict):
                refs = set(it.get('src') or it.get('source_episode_ids') or [])
            # refs 为空的老条目无从判断来源，保留
            if refs and refs <= gone:
                report['card_items'] += 1
            else:
                keep.append(it)
        card[key] = keep

    data['episodes'] = [e for e in eps if e.get('id') not in gone]
    data['pending'] = [p for p in pend if p not in rm_pend]
    data['inductions'] = [i for i in inds if i not in rm_ind]
    data['supersedes'] = [r for r in (data.get('supersedes') or [])
                          if not ({r.get('old'), r.get('new')} & gone)]
    if data.get('last_induct_episode_id') in gone:
        data['last_induct_episode_id'] = max(
            [e.get('id') or 0 for e in data['episodes']], default=0)

    drop = len(rm_eps) + len(rm_pend)
    count = max(0, int(data.get('interaction_count') or 0) - drop)
    data['interaction_count'] = count
    data['last_induction_round'] = min(
        int(data.get('last_induction_round') or 0), count)

    report['count_after'] = count
    report['card_after'] = {k: card.get(k) for k in stat_keys}
    if not dry_run:
        report['backup'] = backup() or ''
        save(data)
    return report


def bump_interaction(data):
    """每轮互动计数（无论是否值得记）。"""
    data['interaction_count'] += 1


def add_episode(data, episode):
    """追加一个值得记的事件（不可变记录）。

    time 用 setdefault：话题跨会话闭合时事件已带缓冲里最后一轮的时间戳（不覆盖）。
    """
    ids = [e.get('id', 0) for e in data['episodes']]
    episode['id'] = (max(ids) if ids else 0) + 1
    episode.setdefault('time', now_str())
    episode.setdefault('start_time', episode.get('time') or now_str())
    episode.setdefault('inactive', False)
    data['episodes'].append(episode)


def forget(data):
    """遗忘：importance=1 且超过 FORGET_DAYS 天未互动 → 淡出（inactive）。

    收藏事件永不淡出（固定保留）。
    """
    now = datetime.datetime.now()
    for ep in data['episodes']:
        if ep.get('inactive') or ep.get('favorite'):
            continue
        if ep.get('importance', 1) <= 1:
            dt = _parse_time(ep.get('time'))
            if dt is not None and (now - dt).days > FORGET_DAYS:
                ep['inactive'] = True


# ---------- 事件的人工维护（收藏 / 归档删除） ----------

CARD_ITEM_KEYS = ('impressions', 'facts', 'self_disclosure', 'reflections')
MAX_DELETIONS = 200   # 删除归档最多保留条数（每条含完整事件原文）


def _find_episode(data, ep_id):
    try:
        ep_id = int(ep_id)
    except (TypeError, ValueError):
        return None
    for e in data.get('episodes') or []:
        if e.get('id') == ep_id:
            return e
    return None


def set_episode_favorite(ep_id, favorite=True):
    """收藏/取消收藏一条事件（固定保留 + 检索加权 + 优先注入）。"""
    data = load()
    ep = _find_episode(data, ep_id)
    if ep is None:
        raise ValueError('事件 #%s 不存在' % ep_id)
    ep['favorite'] = bool(favorite)
    save(data)
    return {'id': ep.get('id'), 'favorite': ep['favorite'],
            'time': episode_time(ep), 'summary': ep.get('summary') or ''}


def delete_episode(ep_id, reason='', dry_run=False):
    """归档删除一条事件（可恢复）。

    事件原文移入 data['deletions']（带删除时间与原因），不是直接抹掉。
    连带处理：
      - 卡片条目只引用这条事件 → 一并删除（事件没了条目还在，等于没忘）；
      - 还引用其他事件 → 保留，只把死 id 从 src 里摘掉；
      - 取代链含这条事件 → 移除（若它是 new，被取代的旧事件自动恢复生效）。
    dry_run=True 只返回影响清单，不改不落盘（面板二次确认用）。
    """
    data = load()
    ep = _find_episode(data, ep_id)
    if ep is None:
        raise ValueError('事件 #%s 不存在或已删除' % ep_id)
    eid = ep.get('id')
    # 只要还引用着别的（哪怕已淡出的）事件，就保留条目——宁留勿删
    other_ids = {e.get('id') for e in data.get('episodes') or []} - {eid}
    card = data.get('card') or {}
    removed, kept = [], []
    for key in CARD_ITEM_KEYS:
        for it in card.get(key) or []:
            if not isinstance(it, dict):
                continue
            refs = list(it.get('src') or it.get('source_episode_ids') or [])
            if eid not in refs:
                continue
            rest = [r for r in refs if r in other_ids]
            row = {'key': key, 'text': it.get('text') or ''}
            if rest:
                kept.append(dict(row, src=refs, kept_src=rest))
            else:
                removed.append(dict(row, src=refs))
    rels = [r for r in data.get('supersedes') or []
            if isinstance(r, dict) and {r.get('old'), r.get('new')} & {eid}]
    report = {
        'episode': {'id': eid, 'time': episode_time(ep),
                    'summary': ep.get('summary') or '',
                    'channel': ep.get('channel') or '',
                    'favorite': bool(ep.get('favorite'))},
        'remove_items': removed,
        'keep_items': kept,
        'supersedes': len(rels),
        'backup': '',
    }
    if dry_run:
        return report

    report['backup'] = backup() or ''
    for key in CARD_ITEM_KEYS:
        items = []
        for it in card.get(key) or []:
            if not isinstance(it, dict):
                items.append(it)
                continue
            refs = list(it.get('src') or it.get('source_episode_ids') or [])
            if eid not in refs:
                items.append(it)
                continue
            rest = [r for r in refs if r in other_ids]
            if not rest:
                continue
            if 'src' in it:
                it['src'] = rest
            else:
                it['source_episode_ids'] = rest
            items.append(it)
        card[key] = items
    data['episodes'] = [e for e in data.get('episodes') or [] if e.get('id') != eid]
    data['supersedes'] = [r for r in data.get('supersedes') or []
                          if not (isinstance(r, dict)
                                  and {r.get('old'), r.get('new')} & {eid})]
    if data.get('last_induct_episode_id') == eid:
        # 退到比它小的最近一条：中间删除时不能直接退到当前最大 id，
        # 那会把「还没归纳过的更新事件」一起算成已归纳。
        data['last_induct_episode_id'] = max(
            [e.get('id') or 0 for e in data['episodes']
             if (e.get('id') or 0) < eid], default=0)
    archive = data.setdefault('deletions', [])
    archive.append({
        'ts': now_str(),
        'kind': 'manual_episode_deletion',
        'reason': (reason or '')[:200],
        'episodes': [ep],
    })
    data['deletions'] = archive[-MAX_DELETIONS:]
    save(data)
    return report


def restore_episode(ep_id):
    """把归档里的事件放回事件库（卡片条目由下次归纳重建）。"""
    data = load()
    try:
        eid = int(ep_id)
    except (TypeError, ValueError):
        raise ValueError('事件 id 必须是整数：%s' % (ep_id,))
    for rec in data.get('deletions') or []:
        if not isinstance(rec, dict):
            continue
        eps = rec.get('episodes') or []
        for ep in eps:
            if isinstance(ep, dict) and ep.get('id') == eid:
                data['episodes'].append(ep)
                data['episodes'].sort(key=lambda e: e.get('id') or 0)
                rec['episodes'] = [x for x in eps if x is not ep]
                data['deletions'] = [r for r in data['deletions']
                                     if r is not rec or r.get('episodes')]
                save(data)
                return {'id': eid, 'time': episode_time(ep),
                        'summary': ep.get('summary') or ''}
    raise ValueError('归档里没有事件 #%s' % ep_id)


def backfill_times(data=None):
    """给历史数据补时间与来源标记（就地修改，不落盘）。

    - 事件没有 start_time → 用 time 回填；
    - 事件没有 channel → 按 speaker 推断（私聊恒为「你」）；
    - 卡片条目没有 time → 取来源事件里最新的一条时间。
    无来源的老条目无从判断，保持空，由调用方报告出来人工看。
    """
    data = data if data is not None else load()
    eps = data.get('episodes') or []
    ep_time = {}
    start_filled = channel_filled = 0
    for e in eps:
        if not e.get('start_time'):
            e['start_time'] = e.get('time') or ''
            start_filled += 1
        if not e.get('channel'):
            e['channel'] = _round_channel(e)
            channel_filled += 1
        ep_time[e.get('id')] = episode_time(e)
    item_filled = 0
    no_source = []
    for key in CARD_ITEM_KEYS:
        for it in (data.get('card') or {}).get(key) or []:
            if not isinstance(it, dict) or it.get('time'):
                continue
            refs = it.get('src') or it.get('source_episode_ids') or []
            times = [ep_time[r] for r in refs if ep_time.get(r)]
            if times:
                it['time'] = max(times)
                item_filled += 1
            else:
                no_source.append(it.get('text') or '')
    return {
        'events': len(eps),
        'start_time_filled': start_filled,
        'channel_filled': channel_filled,
        'item_time_filled': item_filled,
        'item_no_source': no_source,
    }


# ---------- 话题边界闭合（阶段B） ----------

def is_greeting(user_msg):
    """纯寒暄判定（原逐轮提取的规则前置过滤，抽出复用）。

    只拦"纯寒暄"（无实质内容），有实质内容放行（如"你好，我叫零，是大学生"）。
    不按句长一刀切：短句也可能是实质内容（"明天还能见面吗"只有 7 个字，
    曾因「短于 8 字」被当成寒暄吞掉）。
    """
    s = (user_msg or '').strip()
    stripped = cfg.GREETING_WORDS.sub('', s)
    stripped = re.sub(r'！|!|？|\?|。|\.|\s', '', stripped)
    if len(stripped) < cfg.MIN_SUBSTANCE:
        return True
    return bool(cfg.ONLY_MOOD.search(s))


# ---------- 说话人与语境（群聊：这句话是谁说的、当时群里还在说什么） ----------
# 只做「标注」，不做结构化来源（没有 speakers 数组、没有 source_uid 校验）：
# 目的是让归纳模型看见"谁说的 + 上下文"，避免把群里的对话切碎成无主语碎片。

RECENT_MAX = 4          # 每轮携带的「当时群里」条数
RECENT_ITEM_CHARS = 40  # 单条截断（防 pending 膨胀）
ABOUT_TAG_CHARS = 16    # 「关于他的一句话标签」长度上限（与 member_card.BRIEF_TEXT_CHARS 对齐）


def _norm_recent(recent):
    """把调用方给的最近群聊归一成 [{'uid','nickname','text'}, ...]，最多 RECENT_MAX 条。

    接受三种形状：[{'uid','nickname','text'}...]（新）、[[uid, nick, text]...]、
    [[nick, text]...]（2026-10-28 之前的旧数据）。
    uid 可空——旧数据与私聊语境没有 uid，归属判断时按「未知」处理。
    非法项静默跳过（记忆写入不能因为一个脏参数整轮失败）。
    """
    out = []
    for it in (recent or [])[:RECENT_MAX]:
        try:
            if isinstance(it, dict):
                uid, nick, text = it.get('uid', ''), it.get('nickname', ''), it.get('text', '')
            elif isinstance(it, (list, tuple)) and len(it) >= 3:
                uid, nick, text = it[0], it[1], it[2]
            elif isinstance(it, (list, tuple)) and len(it) >= 2:
                uid, nick, text = '', it[0], it[1]
            else:
                continue
        except (TypeError, IndexError, KeyError):
            continue
        nick = str(nick or '').strip()[:20]
        text = str(text or '').strip()[:RECENT_ITEM_CHARS]
        if nick and text:
            out.append({'uid': str(uid or '').strip(), 'nickname': nick, 'text': text})
    return out


def _recent_line(rounds):
    """取最后一个非空 recent，输出「群友A：我搬家了；小花：搬哪了」（≤120 字）。

    取最后一个而不是全部：连续多轮的群聊背景高度重复，合并会让 prompt 成倍膨胀。
    """
    for r in reversed(rounds or []):
        pairs = _norm_recent(r.get('recent'))
        if pairs:
            return '；'.join('%s：%s' % (p['nickname'], p['text']) for p in pairs)[:120]
    return ''


def _round_speaker(r):
    """这一轮的说话人昵称；旧数据（无 speaker 字段）回落「你」。

    回落值曾是「用户」，但那是模型的泛称而不是称呼：写进归纳 prompt 后，
    模型照着抄出「和用户一起在咖啡店」，最终原样漏进 system。
    """
    return (r.get('speaker') or '').strip() or '你'


def _round_ats(r):
    """这一轮 @ 了谁 → 「（本条 @ 了 群友）」；无则空串。

    2026-10-28：@ 的 qq 号是协议层给的确定信息。把它显式写进判定/归纳的输入，
    模型就不用靠"这人指的是谁"去猜——群聊里指代常常跨轮，猜错就记到别人头上。
    """
    names = [a['nickname'] for a in _norm_ats(r.get('ats')) if a.get('nickname')]
    return '（本条 @ 了 %s）' % '、'.join(names) if names else ''


def _norm_ats(ats):
    """@ 名单归一成 [{'uid','nickname'}...]（最多 3 条）。

    这是「这句话在说谁」的**确定性**依据：@ 的 qq 号来自协议层，不需要模型猜。
    uid 为空时保留昵称（构造消息的测试里可能没有 qq）。
    """
    out, seen = [], set()
    for it in (ats or []):
        if len(out) >= 3:      # 先去重再限长：a,a,2 这种输入不能占满名额
            break
        if not isinstance(it, dict):
            continue
        uid = str(it.get('uid') or '').strip()
        nick = str(it.get('nickname') or '').strip()[:20] or uid
        if not (uid or nick) or (uid or nick) in seen:
            continue
        seen.add(uid or nick)
        out.append({'uid': uid, 'nickname': nick})
    return out


def _round_channel(r):
    """这一轮来自私聊还是群聊。

    新数据直接带 channel；旧缓冲没有该字段时按说话人推断：
    私聊固定回落「你」，群聊带昵称。推断只用于旧数据兼容。
    """
    ch = str((r or {}).get('channel') or '').strip()
    if ch in ('private', 'group'):
        return ch
    return 'group' if (str((r or {}).get('speaker') or '').strip() not in ('', '你')) else 'private'


def _last_round_gap(rounds):
    """本通道最后一轮缓冲距今多少秒；时间缺失/解析失败返回 None。"""
    if not rounds:
        return None
    dt = _parse_time((rounds[-1] or {}).get('time'))
    if dt is None:
        return None
    return (datetime.datetime.now() - dt).total_seconds()


def _flush_idle_span(data, rounds, model, think, channel, scene_hint=''):
    """静默超时：把上一段话题单独结掉，返回提交的 episode（不值得记为 None）。

    与正常闭合不同：这里**必须**分段。模型判 continue 或调用失败时，改用本地兜底
    把旧轮次落成事件——宁可摘要粗糙，也不能让它和新话题并进同一条事件。
    """
    keep = {id(r) for r in rounds}
    data['pending'] = [r for r in data.get('pending') or [] if id(r) not in keep]
    obj = _boundary_check(data, True, model, think, channel, scene_hint, rounds=rounds)
    if obj and obj.get('action') == 'close' and not obj.get('worth'):
        return None
    ep = (obj or {}).get('episode') or {}
    if not ep:
        first = next((r.get('user') for r in rounds if r.get('user')), '')
        ep = {'summary': first[:30], 'importance': 1}
    return _commit_span(data, list(rounds), ep)


def buffer_round(data, user_msg, reply, model=None, think=None, channel='local',
                 speaker='', recent=None, ats=None, scene_hint='', is_group=False):
    """话题边界闭合主入口：追加本轮 → 边界判定 LLM → 话题结束时整段提交为一条事件。

    speaker: 本句说话人昵称（群聊传 msg.nick）。空=回落「你」，GUI/本地聊天不必传。
    recent:  最近几条别人的群聊 [{'uid','nickname','text'}...]，只作语境，不当事实来源。
    ats:     本句 @ 到的其他人 [{'uid','nickname'}...]——归属依据（不含 @她自己）。
    scene_hint: 当前环境（今日日常文本），给边界模型定 episode.scene 用；
                不传则退回旧行为（模型自己猜场景，容易沿用记忆卡里的旧地点）。
    is_group: 本轮来自群聊。私聊/群聊**各自独立结算**：同一个 uid 可能同时挂着一个
              私聊话题和一个群聊话题，群聊消息不能把私聊缓冲一起判掉、也不能并进同一条事件。
    model/think/channel: 同旧 extract_episode（GUI 传当前对话模型与后端）。
    返回 dict（供 rp_gui debug_log）：
      {'action': 'buffered'|'closed'|'dropped', 'rounds': n, 'episode': dict|None, ...}
    - buffered: 话题继续（含纯寒暄、LLM 失败）——缓冲保留，下轮再判
    - closed:   话题结束且值得记 → 整段 span 提交为一条 episode（缓冲已清空）
    - dropped:  不值得记 / 空 span / 超硬上限丢弃（缓冲已清空）

    核心不变量：缓冲 append-only——LLM 失败只延迟决策、不丢数据；
    只有显式 close（或硬上限兜底）才清缓冲。
    """
    pending = data.setdefault('pending', [])
    cur_channel = 'group' if is_group else 'private'
    for r in pending:  # 旧数据没有 channel：先归位，避免和当前通道混在一起判
        if not r.get('channel'):
            r['channel'] = _round_channel(r)
    # 静默超时：上一段话题已经隔了很久，先把旧 span 单独结掉再收新消息。
    # 否则边界模型会看到「16:25 的咖啡店 + 19:09 的读书」两段无关经历同处一个缓冲，
    # 一闭合就并成一条事件（2026-11-05 数值暴跌的事故来源）。
    old_rounds = [r for r in pending if r.get('channel') == cur_channel]
    gap = _last_round_gap(old_rounds)
    flushed = None
    if gap is not None and gap > BUFFER_IDLE_SEC:
        flushed = _flush_idle_span(data, old_rounds, model, think, channel, scene_hint)
        pending = data.setdefault('pending', [])
    pending.append(
        {'user': (user_msg or '')[:300], 'reply': (reply or '')[:300], 'time': now_str(),
         # 空 speaker 回落「你」而不是留空：留空会让事件、归纳、注入三层全部退化成
         # 无主语句子（实测私聊两条事件 speaker=""，归纳出「和用户一起在咖啡店」）。
         'speaker': (speaker or '').strip()[:20] or '你', 'recent': _norm_recent(recent),
         'ats': _norm_ats(ats), 'channel': cur_channel})
    rounds = [r for r in pending if r.get('channel') == cur_channel]
    n = len(rounds)
    # 纯寒暄：不调 LLM，只入缓冲；达上限时修剪末尾寒暄（防纯寒暄缓冲无限涨）
    if is_greeting(user_msg):
        if n >= BUFFER_CAP:
            while rounds and is_greeting(rounds[-1]['user']):
                dead = rounds.pop()
                if dead in pending:
                    pending.remove(dead)
        return {'action': 'buffered', 'rounds': len(rounds), 'flushed': flushed}
    # 硬上限兜底：LLM 持续失败时无条件丢弃（防内存膨胀，debug 可见）
    if n >= BUFFER_HARD_MAX:
        data['pending'] = [r for r in pending if r.get('channel') != cur_channel]
        return {'action': 'dropped', 'rounds': n, 'reason': 'overflow',
                'flushed': flushed}
    force = n >= BUFFER_CAP
    obj = _boundary_check(data, force, model, think, channel, scene_hint, rounds=rounds)
    if obj is None:  # LLM 失败/解析失败：保留缓冲，下轮重试
        return {'action': 'buffered', 'rounds': n, 'llm_failed': True,
                'flushed': flushed}
    if obj.get('action') == 'continue':
        return {'action': 'buffered', 'rounds': n, 'flushed': flushed}
    # close：本通道的整段缓冲即话题 span（另一通道的缓冲原样保留）
    data['pending'] = [r for r in pending if r.get('channel') != cur_channel]
    span = rounds
    if not obj.get('worth'):
        # 兜底可见性：把被丢弃的 span 首句摘要带进返回，post_turn 落 debug 日志，
        # 便于日后一眼看出「哪段共同经历被误判为不值得」。
        head = (span[0].get('user') if span else '')[:30]
        return {'action': 'dropped', 'rounds': n, 'span_head': head,
                'flushed': flushed}
    ep = _commit_span(data, span, obj.get('episode') or {})
    return ({'action': 'closed', 'rounds': n, 'episode': ep, 'flushed': flushed}
            if ep else {'action': 'dropped', 'rounds': n, 'reason': 'empty_span',
                        'flushed': flushed})


def _boundary_check(data, force, model, think, channel, scene_hint='', rounds=None):
    """边界判定 LLM 调用（temp 0，channel 路由同提取/归纳）。失败/解析失败返回 None。

    rounds: 只判本通道的缓冲（私聊/群聊分开结算）；None 时沿用旧行为判全部。
    """
    rounds = data['pending'] if rounds is None else rounds

    def _one(r):
        # 带时间戳：同一缓冲里如果夹着很久以前的话，模型能直接看出间隔，
        # 不会把两段隔了几小时的经历当成一段连续话题。
        t = (r.get('time') or '')
        stamp = '[%s] ' % t[5:16] if len(t) >= 16 else ''
        return '%s%s说：「%s」%s\n%s说：「%s」' % (
            stamp, _round_speaker(r), r['user'][:120], _round_ats(r),
            cfg.self_name(), r['reply'][:120])

    rounds_text = '\n'.join(_one(r) for r in rounds)
    # 群聊语境：只作「她当时在回应什么」的补充，不参与事实判定
    tail = _recent_line(rounds)
    if tail:
        rounds_text += '\n（当时群里还在说：%s）' % tail
    # 外部模型看到的历史卡就是用户的长期画像；脱敏时只给一个占位，
    # 边界判定仍可依据本轮对话与场景完成（代价是可能多记一条重复事件）。
    known = ('（已脱敏：本次不提供历史卡片）' if cfg.should_redact(channel)
             else (format_card(data['card'], superseded=superseded_map(data)) or '（无）'))
    # 最近事件摘要：跨轮承诺（头天晚上约定、第二天兑现）只有带上它们才判得出来。
    # 脱敏时不给——事件摘要同样是长期记忆。
    recent = ''
    if not cfg.should_redact(channel):
        recent = '\n'.join(
            '- [%s] %s' % (str(e.get('time') or '')[:16], (e.get('summary') or '')[:40])
            for e in (data.get('episodes') or [])[-3:]
            if isinstance(e, dict) and not e.get('inactive') and e.get('summary'))
    prompt = cfg.boundary_prompt(rounds_text, known, force, scene=scene_hint,
                                 recent=recent)
    try:
        text = _gen_llm(prompt, model=model, think=think, channel=channel, num_predict=400)
        m = re.search(r'\{.*\}', text or '', re.S)
        if not m:
            return None
        obj = json.loads(m.group(0))
        if obj.get('action') not in ('continue', 'close'):
            return None
        return obj
    except Exception:
        return None


def _match_about(span, about):
    """模型给的 about 昵称 → (uid, 昵称)。对不上返回 ('', '')。

    **只在 span 里 @ 过的名单里找**。这是防串人的关键防线：模型可能把"这人"
    安到一个没被 @ 的人头上（群聊指代本来就模糊），而 @ 的 qq 号是协议层给的确定信息。
    昵称不是稳定标识，所以先精确匹配、再退到子串（群名片常被截断或带后缀）。
    """
    q = (about or '').strip()
    if not q:
        return '', ''
    # 纯数字不是昵称：QQ @ 段缺昵称时协议层会回退成 QQ 号，模型照抄进 about 就会
    # 把数字当成名字写进事件（2026-11-01 实测：原作角色卡的 about 变成 10000003）。
    # @ 名单本身带 uid，不需要靠模型把号码当名字；宁可判不出归属，也不串人。
    if q.isdigit():
        return '', ''
    cands = []
    for r in span or []:
        for a in _norm_ats(r.get('ats')):
            if a.get('uid'):
                cands.append((a['uid'], a['nickname']))
    if not cands:
        return '', ''
    for uid, nick in cands:
        if nick == q:
            return uid, nick
    for uid, nick in cands:
        if nick and (nick in q or q in nick):
            return uid, nick
    return '', ''


def _commit_span(data, span, ep):
    """把已闭合的话题 span 提交为一条事件。返回 episode 或 None（span 无实质内容）。"""
    while span and is_greeting(span[-1]['user']):  # 末尾纯寒暄不进事件
        span.pop()
    if not span:
        return None

    def _join(key):
        parts = [r[key][:100] for r in span]
        return '；'.join(parts)[:300]
    try:
        imp = max(1, min(3, int(ep.get('importance', 1))))
    except (TypeError, ValueError):
        imp = 1
    # 说话人集合（去重保序，最多 3 个）：供归纳时区分「谁说的」。
    # 不建 speakers 数组结构——那属于结构化来源（C 档），这里只留展示用字符串。
    names = []
    for r in span:
        n = (r.get('speaker') or '').strip()
        if n and n not in names:
            names.append(n)
    # 归属：这段话是不是在讲某个被 @ 的群友（不是说话人自己）。
    # about_uid 只可能来自 at 名单，匹配不上就当"说的是说话人自己"。
    about_uid, about_nick = _match_about(span, ep.get('about'))
    about_tag = (ep.get('about_tag') or '').strip()[:ABOUT_TAG_CHARS] if about_uid else ''
    first_time = span[0].get('time') or ''
    ep = {
        'summary': _person((ep.get('summary') or _join('user')[:30] or '（未总结）'))[:80],
        'scene': (ep.get('scene') or '')[:20],
        'emotion': (ep.get('emotion') or '')[:10],
        'importance': imp,
        'tags': [t for t in (ep.get('tags') or [])][:5],
        'user_said': _join('user'),
        'character_said': _join('reply'),
        'time': span[-1].get('time') or now_str(),  # 话题结束那轮的缓冲时间（跨会话闭合仍正确）
        # 话题开始时间：跨零点闭合时 time 已经是第二天凌晨，展示「昨晚」要靠它。
        'start_time': first_time or now_str(),
        # 私聊 / 群聊：同一用户的两种会话来源分开结算（见 buffer_round），
        # 事件本身也要能看出来，否则事件库和归纳都分不清「这句是在群里说的」。
        'channel': span[-1].get('channel') or _round_channel(span[-1]),
        'speaker': '、'.join(names[:3]) or '你',       # 空则回落「你」（私聊/旧数据）
        'context': _recent_line(span),               # 话题结束时的群聊语境（可空）
        'about': about_nick,        # 被谈论者昵称（空=说的就是说话人自己）
        'about_uid': about_uid,     # 被谈论者 uid（只有 @ 命中时才有）
        'about_tag': about_tag,     # 关于他的一句话标签（写进他的 member.briefs）
    }
    add_episode(data, ep)
    return ep


def _as_id_list(value):
    """把模型给的 source_episode_ids 规整成 int 列表（容忍字符串/单值/None）。"""
    if value is None:
        return []
    if isinstance(value, (int, str)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for v in value:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _validate_supersedes(raw, valid_ids):
    """校验模型给出的取代关系：id 必须在场、不能自取代、不能成环。

    任一条不过就**丢弃该条**——宁可两条并存（多一条过期事实），也不可错删
    （错删会让角色坚定地说错话，且用户无法察觉）。
    返回 [{'new': id, 'old': id}]（不含 at，由调用方补时间戳）。
    """
    out = []
    for rel in (raw or []):
        if not isinstance(rel, dict):
            continue
        try:
            new = int(rel.get('new'))
            old = int(rel.get('old'))
        except (TypeError, ValueError):
            continue
        if new == old or new not in valid_ids or old not in valid_ids:
            continue
        out.append({'new': new, 'old': old})
    if not out:
        return []
    # 成环检测：沿 new→old 逐跳下溯，回到起点即成环（模型偶发互相取代）
    old_of = {}
    for r in out:
        old_of.setdefault(r['new'], r['old'])
    for start in old_of:
        seen = set()
        cur = start
        while cur in old_of:
            if cur in seen:
                out = [r for r in out if r['new'] != start]
                break
            seen.add(cur)
            cur = old_of[cur]
    return out


# ---------- 重归纳（阶段B） ----------

def _stat_line(card):
    """【关系现状】三值。**必须让模型看见当前数值**：少了它，模型只能根据事件
    盲猜绝对值——一句「我喜欢你」能猜成 80（当前 30），被"不能跳变"的措辞吓到
    又猜成 5。代码层再怎么限幅，也治不了模型不知道基准这件事。
    """
    return ('【关系现状】好感 %s · 信任 %s · 熟悉 %s（%s）'
            % (card.get('affection', 30), card.get('trust', 30),
               card.get('familiarity', 0), card.get('relationship', '相识')))


def card_digest(card):
    """给归纳用的卡片视图：每条都带「来源事件编号」。

    归纳 prompt 要求 supersedes 的 old 填**旧条目引用的来源事件编号**，
    所以模型必须看得见 src——format_card 是给人/给对话模型看的摘要，不含 src，
    直接喂它会拿不到编号、只能瞎猜（然后被校验全部丢弃）。
    """
    lines = [_stat_line(card)]
    for key, label in (('facts', '事实'), ('impressions', '印象'),
                       ('self_disclosure', '%s自述' % cfg.self_name())):
        for it in (card.get(key) or []):
            # 旧卡里的场景状态不能回喂给归纳模型：它会照着抄，脏数据代代相传。
            if key == 'self_disclosure' and is_scene_status(it.get('text')):
                continue
            src = it.get('src') or []
            lines.append('- [%s] %s（来源事件：%s）'
                         % (label, it.get('text', ''),
                            ','.join(str(s) for s in src) if src else '无'))
    return '\n'.join(lines) if lines else '（空）'

def _judge_profile(channel):
    """当前判官模型若走该 API 通道，返回它自己的档位（否则回落到全局档）。"""
    try:
        import runtime_config as rc
        import rp_core as core
        key = rc.get('judge_model')
        if core.resolve_model(key)[0] == channel:
            return core.model_profile(key)
    except Exception:
        pass
    return None


def _gen_llm(prompt, model=None, think=None, channel='local', num_predict=300,
             profile=None):
    """统一 LLM 调用：'local' 走 Ollama；'api' 走千问；'localapi' 走本地 API。

    返回完整文本（失败返回 ''）。local 时 think 默认关思考链。
    API 通道用面板「判官思考」挡位（judge_thinking）；显式 think 优先。
    profile 指定千问档位，None 时按全局 api_profile。
    """
    if channel in ('api', 'localapi'):
        from rp_common import chat_api
        try:
            import runtime_config as rc
            thinking = rc.get('judge_thinking', cfg.API_THINKING)
        except Exception:
            thinking = cfg.API_THINKING
        if isinstance(think, (bool, str)):
            thinking = think
        default_model = cfg.LOCAL_API_MODEL if channel == 'localapi' else cfg.DAILY_MODEL
        try:
            return chat_api(
                [{'role': 'system', 'content': '只输出 JSON。'},
                 {'role': 'user', 'content': prompt}],
                channel, model or default_model,
                temperature=0, max_tokens=num_predict,
                thinking=thinking, profile=profile or _judge_profile(channel))
        except Exception as e:
            logutil.throttled(
                _LOG, 'gen_llm:%s:%s' % (channel, model or default_model),
                logging.WARNING,
                '记忆 LLM 调用失败（%s / %s）：%s；本次按空结果处理',
                channel, model or default_model, e, exc_info=True)
            return ''
    text = ''
    try:
        for text in stream_chat(model or MODEL,
                                [{'role': 'system', 'content': '只输出 JSON。'},
                                 {'role': 'user', 'content': prompt}],
                                options={'temperature': 0, 'num_predict': num_predict,
                                         'num_ctx': hw.NUM_CTX},
                                think=(False if think is None else think)):
            pass
    except Exception as e:
        logutil.throttled(
            _LOG, 'gen_llm:local:%s' % (model or MODEL), logging.WARNING,
            '记忆 LLM 调用失败（local / %s）：%s；本次按空结果处理',
            model or MODEL, e, exc_info=True)
        return ''
    return text


def _rel_ceiling(card):
    """三项数值**能支撑的**最高关系档位。

    「表白」这类事件会让模型在数值还很生疏时把关系判成熟络——
    但关系不是一句话给的，是积累出来的。所以档位上限由数值决定，
    模型可以在上限以下自由选择（比如数值够熟络但她说相识，尊重她），
    但不能越过数值去。
    """
    aff = _as_int(card.get('affection'), 30)
    tru = _as_int(card.get('trust'), 30)
    fam = _as_int(card.get('familiarity'), 0)
    for name, a, t, f in stat_rules.relation_floor():
        if aff >= a and tru >= t and fam >= f:
            return name
    return '陌生'


def _as_int(v, default):
    """卡片里的数值可能被写成字符串（旧数据/手写），统一转成 int。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _clamp_step(old, new, limit, neg_limit=None):
    """把单次归纳的变动幅度钉在 ±limit（负向另用 neg_limit）内，再夹进 0~100。

    这是**关系只能积累、不能跳变**的硬约束：模型给 80（当前 30）时，
    结果是 30+limit，不是 80。没有这层，一句表白就能让好感从 30 跳到 80。
    负向单独限幅，是因为正向的档位表在「陌生」时只有 +8，用它卡扣分没道理——
    反感不需要攒够档位才扣得动。
    """
    old = max(0, min(100, _as_int(old, 0)))
    new = _as_int(new, old)
    down = limit if neg_limit is None else neg_limit
    step = max(-down, min(limit, new - old))
    return max(0, min(100, old + step))


def _delta_caps(card, big=False):
    """本次归纳三项各自允许的最大涨幅（取归纳前的档位）。

    表白能不能跳，看的是表白之前攒了多少，不是表白之后——所以必须在
    写入新数值之前调用。具体数字来自当前角色的 stats.json。
    """
    rel = card.get('relationship')
    return {k: stat_rules.gain_cap(k, rel, big=big) for k in stat_rules.STAT_KEYS}


def _stat_snapshot(card):
    """三项数值 + 档位的可展示快照（脏数据归一成 int）。"""
    return {
        'affection': _as_int(card.get('affection'), 30),
        'trust': _as_int(card.get('trust'), 30),
        'familiarity': _as_int(card.get('familiarity'), 0),
        'relationship': card.get('relationship') or '相识',
    }


def _record_induction(data, before, episode_ids):
    """记一条数值变化历史（给 UI 看）。无新事件且无变化时不记。"""
    after = _stat_snapshot(data['card'])
    delta = {k: after[k] - before[k] for k in ('affection', 'trust', 'familiarity')}
    changed = any(delta.values()) or after['relationship'] != before['relationship']
    if not episode_ids and not changed:
        return
    hist = data.setdefault('inductions', [])
    if not isinstance(hist, list):
        hist = data['inductions'] = []
    hist.append({
        'ts': now_str(),
        'round': _as_int(data.get('interaction_count'), 0),
        'episode_ids': list(episode_ids),
        'before': before,
        'after': after,
        'delta': delta,
    })
    del hist[:-INDUCT_HISTORY_MAX]


def _item_time(src, ep_time_by_id, old=None):
    """条目日期：取来源事件里最新的一条（事实被重申/合并时的时点）。

    没有来源时沿用旧条目已有的日期；再没有就留空（迁移脚本会给历史条目补）。
    """
    times = [ep_time_by_id.get(i) for i in (src or []) if ep_time_by_id.get(i)]
    if times:
        return max(times)
    return (old or {}).get('time') or ''


def induct(data, model=None, think=None, channel='local'):
    """重归纳：从事件库重建记忆卡（幂等，不漂移）。

    事件较多时取最近 20 条作输入（防上下文膨胀）；只从事件归纳，不编造。
    model/think/channel: 同 buffer_round（GUI 传当前对话模型与后端，避免换模型拖慢）
    """
    episodes = [e for e in data['episodes'] if not e.get('inactive')]
    if not episodes:
        return
    window = episodes[-20:]
    before = _stat_snapshot(data['card'])
    marker = _as_int(data.get('last_induct_episode_id'), 0)
    new_ids = [eid for eid in (_as_int(e.get('id'), 0) for e in window) if eid > marker]
    # 给模型明确的主语信息：说话人/角色说分开（否则模型分不清主语，字段混淆）。
    # **必须带 #id**：模型要据以标注 source_episode_ids 与 supersedes；
    # 不带 id 时源引用必然校验失败（旧版 reflections 恒为空即此因）。
    # 说话人用昵称（旧事件无此字段 → 回落「你」）；context 只作语境，不作事实来源。
    # 回落「用户」会让模型照着抄进条目（「和用户一起在咖啡店」），最后漏进 system。
    # 只有上次归纳之后新闭合的事件才能结算数值；窗口里的旧事件一律标成「已结算」，
    # 只作条目核对用。否则两段相隔几小时的对话会被放进同一次判断里互相牵连，
    # 旧事件的 importance 还会把大事件档位带进新事件的结算。
    new_set = set(new_ids)
    ep_summary = '\n'.join(
        '%s#%s [%s]%s %s说：「%s」 %s说：「%s」%s' % (
            '【本次新事件】' if _as_int(e.get('id'), 0) in new_set else '【历史·已结算】',
            e.get('id', '?'), e.get('time', '')[:10],
            '（群聊）' if e.get('channel') == 'group' else '',
            (e.get('speaker') or '').strip() or '你',
            e.get('user_said', '')[:60], cfg.self_name(), e.get('character_said', '')[:60],
            ('（当时群里：%s）' % e['context'][:40]) if e.get('context') else '')
        for e in window)
    # 归纳需要旧卡避免重复，但旧卡本身就是用户画像；脱敏时只留三值：
    # 数值是 prompt 的绝对基准（没有它模型只能盲猜，见 _stat_line），
    # 条目本身才是画像。把整块换成空占位会让模型每轮都猜一个低值，
    # 再被扣分上限压成稳定的 -3/-2。
    redacted = cfg.should_redact(channel)
    current = (_stat_line(data['card']) + '\n（其余历史条目已脱敏，本次不提供）'
               if redacted else card_digest(data['card']))
    prompt = cfg.induct_prompt(ep_summary, current)
    # 只认**实际喂给模型**的事件 id：引用窗口外的 id 必然是幻觉，不能放行
    valid_ids = {e.get('id') for e in window}
    ep_time_by_id = {e.get('id'): episode_time(e) for e in window}
    old_card = {k: [dict(it) for it in (v or [])] for k, v in (data['card'] or {}).items()
                if isinstance(v, list)}          # 重归纳前的深拷贝（仅用于 src 回填）
    old_by_text = {}
    for key, items in old_card.items():
        for it in items:
            if isinstance(it, dict) and it.get('text'):
                old_by_text.setdefault((key, it['text']), it)
    try:
        text = _gen_llm(prompt, model=model, think=think, channel=channel,
                        num_predict=INDUCT_PREDICT)
        obj = _loads_obj(text)
        if not isinstance(obj, dict):
            # 以前这里是 `if not m: return`，被 400 字截断的输出走到这里就静默丢弃，
            # 外面又有一层 except pass —— 归纳连续失败几个月都不会有人知道。
            _LOG.warning('记忆归纳失败：模型输出不是可用 JSON（%d 字符），本次不改动卡片',
                         len(text or ''))
            return
        card = data['card']
        # 这次归纳**之前**的档位。上限和档位变动都要拿它当基准——
        # 「表白能跳多少」取决于表白之前站在哪一层，不是跳完之后。
        old_rel = card.get('relationship')
        # 单次变动上限：先看这次归纳之前的关系基础，再看**本次新事件**里有没有重大事件。
        # 上限必须在写入新数值**之前**算出来，否则「表白能跳多少」会拿跳完之后
        # 的档位去算，等于自己给自己抬门槛。
        # 只有本次新闭合的事件可以动数值。没有新事件时（周期重归纳）数值一律原样
        # 保留——否则同一段旧对话每 5 轮被重读一次，会持续漂移。
        if new_ids:
            # 大事件档位只看**本次新事件**的 importance。旧窗口里的表白/冲突
            # 不能把新事件的结算抬到大事件档，否则一句普通玩笑也能扣掉一档。
            max_imp = 1
            for e in window:
                if _as_int(e.get('id'), 0) not in new_set:
                    continue
                try:
                    v = int(e.get('importance') or 1)
                except (TypeError, ValueError):
                    v = 1
                max_imp = max(max_imp, v)
            caps = _delta_caps(card, big=(max_imp >= 3))
            downs = {k: stat_rules.down_cap(k) for k in stat_rules.STAT_KEYS}
            # 人设明确反感：模型只报 id + 来源事件，扣分按配置固定结算。
            # 重归纳每 5 轮会重看旧窗口，所以按「反感 id + 事件 id」记账：
            # 同一次冒犯只扣一次，否则同一件事会被反复扣穿。
            hits = stat_rules.aversion_hits(obj.get('aversion_hits'),
                                            valid_episode_ids=valid_ids)
            applied = data.setdefault('applied_aversions', [])
            if not isinstance(applied, list):
                applied = data['applied_aversions'] = []
            done = {}
            for rec in applied:
                if not isinstance(rec, dict):
                    continue
                done.setdefault(str(rec.get('id') or ''), set()).update(
                    _as_int(x, -1) for x in (rec.get('episode_ids') or []))
            fresh = []
            for h in hits:
                eps = h.get('episode_ids') or []
                if eps:
                    # 只结算本次归纳新纳入的事件；旧事件重述不重复扣
                    eps = [e for e in eps if e in valid_ids and e > marker]
                else:
                    # 模型没给来源时，按本轮新纳入的事件结算一次（prompt 已要求只报新行为）
                    eps = list(new_ids)
                already = done.get(h['id']) or set()
                eps = [e for e in eps if e not in already]
                if eps:
                    fresh.append({'id': h['id'], 'episode_ids': eps})
            penalty = stat_rules.aversion_deltas(fresh)
            for k in stat_rules.STAT_KEYS:
                old = max(0, min(100, _as_int(card.get(k), 0)))
                if k in obj:
                    step = _clamp_step(old, obj.get(k), caps[k], neg_limit=downs[k]) - old
                else:
                    step = 0
                if k in penalty:
                    step = max(-downs[k], min(caps[k], step + penalty[k]))
                card[k] = max(0, min(100, old + step))
            for h in fresh:
                applied.append({'id': h['id'], 'episode_ids': h['episode_ids'],
                                'at': now_str()})
            del applied[:-200]
            _LOG.info(
                '[%s] 归纳数值：%s → %s（模型返回 %s，%s）',
                os.path.basename(state_dir()) or 'default',
                {k: before[k] for k in stat_rules.STAT_KEYS},
                {k: card[k] for k in stat_rules.STAT_KEYS},
                {k: obj.get(k) for k in stat_rules.STAT_KEYS},
                '脱敏' if redacted else '完整卡片')
            # 档位不能超过数值支撑：表白不能直接把关系拽过「熟络」。
            rel = obj.get('relationship')
            if rel in RELATIONSHIPS:
                ceiling = _rel_ceiling(card)
                if RELATIONSHIPS.index(rel) > RELATIONSHIPS.index(ceiling):
                    rel = ceiling
                try:
                    old_idx = RELATIONSHIPS.index(old_rel)
                except ValueError:
                    old_idx = -1
                if old_idx >= 0:
                    # 反向也兜：档位是长期积累的东西，不该被一次归纳连降数级。
                    # 数值回落一点点（一次反感最多扣 3）也不该让档位跟着掉——
                    # 只有数值已经撑不住原档位时才许退，且一次仍最多退一档。
                    floor_idx = old_idx
                    ceil_idx = RELATIONSHIPS.index(ceiling)
                    if ceil_idx < old_idx:
                        floor_idx = max(ceil_idx, old_idx - 1)
                    if RELATIONSHIPS.index(rel) < floor_idx:
                        rel = RELATIONSHIPS[floor_idx]
                card['relationship'] = rel
        for key in ('impressions', 'facts', 'self_disclosure'):
            # 输出被截断时这些键可能整个消失。此时**必须跳过**，不能让空列表
            # 覆盖掉卡里原有的条目——归纳只成功了一半，却把记忆全抹了。
            if key not in obj:
                continue
            items = []
            for it in (obj.get(key) or []):
                # 入库即净化：只在注入时洗是治不住根的——旧卡会作为「当前记忆卡」
                # 回喂给下一次归纳，模型照着抄「用户」，脏词就这么一代代传下去。
                t = _person((it.get('text') or '').strip())[:60]
                if t:
                    # 场景状态不是长期记忆：模型偶尔仍会输出「刚挑完钢笔」这类当下动作，
                    # 入库就拦掉，避免它被注入并回喂给下一次归纳。
                    if key == 'self_disclosure' and is_scene_status(t):
                        continue
                    try:
                        imp = max(1, min(3, int(it.get('importance', 1))))
                    except (TypeError, ValueError):
                        imp = 1
                    src = [i for i in _as_id_list(it.get('source_episode_ids'))
                           if i in valid_ids]
                    if not src:
                        # src 回填：重归纳时模型常沿用同一措辞却不重复标注来源。
                        # 只认**文本完全相同**的旧条目（不做模糊匹配——抄错 src 会导致
                        # 该显示的条目被过滤掉，属错删，比残留过期事实严重得多）。
                        src = [s for s in _src_by_text(old_card.get(key), t, valid_ids)]
                    items.append({
                        'text': t, 'importance': imp, 'src': src,
                        # 日期同样按文本从旧条目继承：模型不会输出日期，
                        # 重归纳不能把已经补好的时间抹掉。
                        'time': _item_time(src, ep_time_by_id,
                                           old_by_text.get((key, t))),
                    })
            card[key] = items[:10]
        # 反思层：解析 reflections（绑定源事件，可追溯）
        refls = []
        for it in (obj.get('reflections') or []):
            t = _person((it.get('text') or '').strip())[:60]   # 同 facts：入库即净化
            if not t:
                continue
            try:
                imp = max(1, min(3, int(it.get('importance', 1))))
            except (TypeError, ValueError):
                imp = 1
            src = [i for i in _as_id_list(it.get('source_episode_ids')) if i in valid_ids]
            if not src:
                continue  # 无源事件引用 → 丢弃（防无源推断）
            refls.append({
                'text': t, 'importance': imp, 'source_episode_ids': src,
                'time': _item_time(src, ep_time_by_id,
                                   old_by_text.get(('reflections', t))),
            })
        # 抑制自我强化：importance=1 的反思只在重归纳中保留（不注入），importance>=2 注入；
        # 重归纳天然修正/作废旧反思（新事件矛盾 → 模型不再输出）
        # 键缺失 = 输出被截断，同理不能清空。
        if 'reflections' in obj:
            card['reflections'] = refls[:5]
        # 取代链：**累积合并，不回退**。旧事件可能已滑出 20 条窗口、本次归纳不会重提，
        # 覆盖式赋值会让关系无故消失。真正失效由 _live_supersedes 判定
        # （new 事件被删/淡出 → 关系作废、被取代者自动恢复）。
        merged = [r for r in (data.get('supersedes') or []) if isinstance(r, dict)]
        have = {(r.get('new'), r.get('old')) for r in merged}
        for r in _validate_supersedes(obj.get('supersedes'), valid_ids):
            if (r['new'], r['old']) not in have:
                r['at'] = now_str()
                merged.append(r)
                have.add((r['new'], r['old']))
        data['supersedes'] = merged
        _record_induction(data, before, new_ids)
        if new_ids:
            data['last_induct_episode_id'] = max(new_ids)
    except Exception as e:
        # 不能继续 pass：归纳失败的表现是「数值永远不动」，与「关系本来就稳」无法区分，
        # 极难定位。宁可日志里多一行。
        _LOG.warning('记忆归纳异常，本次不改动卡片：%s: %s', type(e).__name__, e)


def _closers(frag):
    """按栈序补回未闭合的括号（不在字符串内的才算数）。"""
    stack = []
    in_str = esc = False
    for ch in frag:
        if esc:
            esc = False
        elif ch == '\\':
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch in '{[':
                stack.append(ch)
            elif ch in '}]' and stack:
                stack.pop()
    return ''.join('}' if c == '{' else ']' for c in reversed(stack))


def _repair_json(frag):
    """把被截断的 JSON 片段砍成**能解析的最长前缀**。

    截断可能停在字符串中间、键名后面、数组元素之间——猜它停在哪儿是不可靠的
    （我第一版就是猜的，结果 `"b":` 这种断法直接漏网）。
    所以改成**枚举每个可能的切点，让 json 自己判定哪个能解析**，从后往前取最长可用前缀。
    砍掉的必然是残缺的尾部，留下的每个成员都是完整的。
    """
    frag = (frag or '').rstrip()
    # 第一个候选是**整段**：结尾正好停在完整值上时（如 `"trust": 55`），
    # 直接补括号即可，不必退到上一个分隔符，否则会把已经写完的成员一起砍掉。
    for i in range(len(frag), 0, -1):
        if i < len(frag) and frag[i] not in ',{[':
            continue
        head = frag[:i].rstrip().rstrip(',')
        if not head:
            break
        cand = head + _closers(head)
        try:
            json.loads(cand)
            return cand
        except ValueError:
            continue
    return ''


def _loads_obj(text):
    """解析归纳输出；被截断时退到修补后解析。

    关键：**三项数值和 relationship 在 JSON 的最前面**，截断发生在后面的条目上。
    所以宁可丢掉尾部条目，也要把数值抢救出来——否则一次截断就等于整轮归纳白干。
    """
    if not text:
        return None
    m = re.search(r'\{.*\}', text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    m = re.search(r'\{.*', text, re.S)
    if not m:
        return None
    fixed = _repair_json(m.group(0))
    if not fixed:
        return None
    try:
        obj = json.loads(fixed)
    except ValueError:
        return None
    # 修补路径下的**空列表不是「模型说没有」，而是「还没写完就断了」**。
    # 两者分不清，就按不删除处理：宁可留着旧条目，也不能把记忆清空。
    for k in list(obj):
        if isinstance(obj[k], list) and not obj[k]:
            del obj[k]
    return obj


def _src_by_text(items, text, valid_ids):
    """从旧卡片条目里找回与 text 完全相同的条目的 src（仍有效的部分）。

    比对前两边都过一遍 _person：新条目入库时已净化，历史条目可能还带「用户」，
    不规范化会让老条目永远匹配不上，src 回填全线失效（条目全被当无源丢弃）。
    """
    for it in (items or []):
        if _person((it.get('text') or '').strip()) == text:
            return [s for s in (it.get('src') or []) if s in valid_ids]
    return []


def maybe_induct(data, model=None, think=None, channel='local', force=False):
    """重归纳触发：事件闭合时立即结算，或每 REINDUCT_EVERY 轮兜底一次。

    force=True：本轮有话题闭合、刚提交了新事件。闭合即结算，避免把相隔很久的两段
    对话攒成一批再判——那正是数值被无关事件牵连的来源。
    返回是否执行。推进 last_induction_round 在此处（触发器）而非 induct（执行器）：
    即使没有事件可归纳、或归纳失败，也把「这一轮检查过」记下来。
    """
    if not force:
        if data['interaction_count'] - data['last_induction_round'] < REINDUCT_EVERY:
            return False
    data['last_induction_round'] = data['interaction_count']
    induct(data, model=model, think=think, channel=channel)
    save(data)
    return True
