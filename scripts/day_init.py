# -*- coding: utf-8 -*-
"""4:00 初始化流水线：各模块把自己的每日初始化注册成步骤，按顺序跑一次。

为什么要单独一层：日程、日记、好奇清单、书架补货等都要在同一个逻辑日边界
（默认 04:00）切到新的一天。之前由 ticker 分别调用，后面每加一项就得再改一次
ticker，失败重试也各写各的。这里统一成「步骤 + 状态」：

    import day_init
    day_init.register('my_step', run_my_step, order=50, label='我的步骤')

    def run_my_step(now):
        ...
        return True      # 今天做完了；返回 False 表示没做成，过冷却再试

约定：
- 步骤函数在自己的线程里同步执行，内部要幂等（重跑不出问题）。
- 抛异常、或返回 False，都会记 15 分钟冷却，之后再试；连续失败不会卡住别的步骤。
- enabled 回调返回 False 时这一步整天跳过；之后再打开开关，下一轮就会补跑。
- 每个角色各自一份进度：state/personas/<id>/day_init.json。

新增的初始化步骤写在任何模块里都可以，只要在进程启动时 import 到并调用 register。
"""
import json
import logging
import os
import threading
import time

import daily_scene
import day_clock
import day_life
import persona
import rp_config as cfg
import self_state

LOG = logging.getLogger('day_init')

_LOCK = threading.RLock()
_TASK_LOCK = threading.Lock()
_CACHE = {}
_OVERRIDE = None
STEPS = []          # [{name, fn, order, enabled, label}]


def set_path(path=None):
    """测试用：覆盖状态文件；传 None 恢复按角色解析。"""
    global _OVERRIDE
    with _LOCK:
        _OVERRIDE = path or None
        _CACHE.clear()


def _path():
    return _OVERRIDE or os.path.join(persona.state_root(), 'day_init.json')


# ---------- 步骤注册 ----------

def register(name, fn, order=100, enabled=None, label=''):
    """注册一个每日初始化步骤。重复注册同名会覆盖旧实现。"""
    step = {'name': str(name), 'fn': fn, 'order': int(order),
            'enabled': enabled, 'label': label or str(name)}
    with _LOCK:
        STEPS[:] = [s for s in STEPS if s['name'] != step['name']]
        STEPS.append(step)
        STEPS.sort(key=lambda s: (s['order'], s['name']))
    return step


def steps():
    """当前注册表（只读视图，方便面板和自测列出步骤）。"""
    with _LOCK:
        return [{'name': s['name'], 'order': s['order'], 'label': s['label']}
                for s in STEPS]


# ---------- 进度状态 ----------

def _empty(day):
    return {'day': day, 'done': {}, 'failed': {}}


def _load():
    path = _path()
    with _LOCK:
        if path in _CACHE:
            return _CACHE[path]
        data = None
        try:
            with open(path, encoding='utf-8') as f:
                saved = json.load(f)
            if isinstance(saved, dict) and isinstance(saved.get('done'), dict) \
                    and isinstance(saved.get('failed'), dict):
                data = {'day': str(saved.get('day') or ''),
                        'done': saved['done'], 'failed': saved['failed']}
        except (OSError, ValueError):
            data = None
        data = data or _empty('')
        _CACHE[path] = data
        return data


def _save(state):
    path = _path()
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        _CACHE[path] = state
        return True
    except OSError:
        LOG.exception('初始化进度写盘失败：%s', path)
        return False


def _state_for(now):
    day = day_clock.logical_today(now)
    state = _load()
    if state.get('day') != day:
        state = _empty(day)
    return state


def _pending(state, now):
    """今天还没做完、也不在冷却期的步骤：[(step, 'run'|'skip')]。"""
    out = []
    for step in STEPS:
        name = step['name']
        if state['done'].get(name):
            continue
        if now < float(state['failed'].get(name) or 0):
            continue
        mode = 'run'
        if step['enabled'] is not None:
            try:
                if not step['enabled']():
                    mode = 'skip'
            except Exception:
                LOG.exception('初始化步骤开关判断失败：%s', name)
        out.append((step, mode))
    return out


def status(now=None):
    now = time.time() if now is None else float(now)
    with _LOCK:
        state = _state_for(now)
        pending = [s['name'] for s, mode in _pending(state, now) if mode == 'run']
    return {'day': state['day'], 'done': dict(state['done']),
            'failed': dict(state['failed']), 'pending': pending,
            'steps': steps()}


# ---------- 执行 ----------

def run(now=None):
    """同步跑一遍今天还没完成的步骤，返回完成的步骤名。"""
    now = time.time() if now is None else float(now)
    if not day_clock.at_or_after_start(now):
        return []
    with _LOCK:
        state = _state_for(now)
        due = _pending(state, now)
    ran = []
    for step, mode in due:
        name = step['name']
        if mode == 'skip':
            continue
        started = time.time()
        try:
            res = step['fn'](now)
            if res is False:
                state['failed'][name] = time.time() + cfg.SELF_RETRY_SEC
                LOG.info('初始化步骤未完成，稍后重试：%s', name)
                continue
            state['done'][name] = now
            state['failed'].pop(name, None)
            ran.append(name)
            LOG.info('初始化步骤完成：%s（%.1fs）', name, time.time() - started)
        except Exception:
            state['failed'][name] = time.time() + cfg.SELF_RETRY_SEC
            LOG.exception('初始化步骤失败：%s', name)
    with _LOCK:
        _save(state)
    if ran:
        LOG.info('%s 初始化完成：%s', state['day'], '、'.join(ran))
    return ran


def tick(now=None):
    """ticker 每轮调用：到 4:00 且当天还有步骤没做完，就起后台线程跑。"""
    now = time.time() if now is None else float(now)
    if not day_clock.at_or_after_start(now):
        return False
    with _LOCK:
        pending = [s for s, mode in _pending(_state_for(now), now)
                   if mode == 'run']
    if not pending:
        return False
    if not _TASK_LOCK.acquire(False):
        return False
    pid = persona.current()

    def run_it():
        try:
            with persona.context(pid):
                run(now)
        finally:
            _TASK_LOCK.release()

    threading.Thread(target=run_it, name='day-init', daemon=True).start()
    return True


# ---------- 内置步骤 ----------

def _scene_step(now):
    """今日日常：生成失败返回 False，过冷却再试（内部还有自己的失败冷却）。"""
    return bool(daily_scene.ensure_today()) or bool(daily_scene.get_cached())


def _self_state_step(now):
    """日记与好奇清单：按 refresh_day 判断今天是否真的写成了。"""
    self_state.sync_plan(now)
    self_state.refresh(now)
    return self_state.refreshed_today(now)


def _bookshelf_step(now):
    """书架：模块不在或没开启时整步跳过；启用时扫描书库、看看要不要补货。"""
    try:
        import bookshelf
    except Exception:
        return True
    if not bookshelf.enabled():
        return True
    out = bookshelf.daily_init(now)
    return not (out.get('ingest', {}).get('error') or
                out.get('refill', {}).get('error'))


register('daily_scene', _scene_step, order=10, label='今日日常')
register('self_state', _self_state_step, order=20,
         enabled=day_life.enabled, label='日记与好奇')
register('bookshelf', _bookshelf_step, order=30, label='书架')
