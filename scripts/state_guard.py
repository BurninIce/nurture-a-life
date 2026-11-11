# -*- coding: utf-8 -*-
"""离线写盘保护 — 测试子进程禁止写线上 state/。

pytest 与各 `--selftest` 会通过 run_offline.py 拉起；它在这里装一个
audit hook，把落在 `<repo>/state` 下的创建/覆盖/改名/删除**全部拦下**。
套件自己的临时目录、`.probe_tmp` 不受影响；读线上快照仍允许（对拍要用）。

为什么用 audit hook 而不是在每个写函数里加一行：运行态写入点有十几处
（记忆卡、成员卡、场景、活动、配置、心跳、历史……），逐个加漏一个就白做；
audit 钩子覆盖 open/os.open/os.rename/os.remove 等底层入口，新写的模块也自动受保护。

只在离线子进程里 install()，线上进程不装钩子，零开销。
"""
import os
import sys

LIVE_STATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'state')

_WRITE_MODE_CHARS = ('w', 'a', 'x', '+')
_OS_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_DST_EVENTS = ('os.link', 'os.symlink')
_SRC_EVENTS = ('os.remove', 'os.rmdir', 'os.truncate')

_ENABLED = False
_INSTALLED = False


class OfflineWriteError(RuntimeError):
    """离线模式下试图写线上 state/。"""


def _norm(path):
    try:
        p = os.fspath(path)
    except TypeError:
        return ''
    if isinstance(p, bytes):
        p = os.fsdecode(p)
    if not isinstance(p, str) or not p:
        return ''
    try:
        return os.path.normcase(os.path.abspath(p))
    except (OSError, ValueError):
        return ''


def is_live(path):
    """path 是否落在线上 state 根（含根自身）之下。"""
    p = _norm(path)
    if not p:
        return False
    root = _norm(LIVE_STATE)
    return p == root or p.startswith(root + os.sep)


def _writes(mode):
    return isinstance(mode, str) and any(c in mode for c in _WRITE_MODE_CHARS)


def check_write(path):
    """离线模式下手动拦一道；线上模式（未 install）恒放行。"""
    if _ENABLED and is_live(path):
        raise OfflineWriteError('离线运行禁止写线上状态目录：%s' % path)


def _exists(path):
    try:
        return os.path.lexists(path)
    except (OSError, ValueError):
        return True


def _hook(event, args):
    if not _ENABLED or not args:
        return
    if event == 'open':
        path = args[0]
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        write = _writes(mode) if mode is not None else bool(int(flags or 0) & _OS_WRITE_FLAGS)
        if write:
            check_write(path)
    elif event == 'os.rename':
        # 源不存在时 rename 本来就失败，别把 FileNotFoundError 换成我们的错
        if _exists(args[0]):
            check_write(args[0])      # 从 state 里移走同样是改动
            if len(args) > 1:
                check_write(args[1])
    elif event in _DST_EVENTS:
        if len(args) > 1:
            check_write(args[1])
    elif event == 'os.mkdir':
        # makedirs(exist_ok=True) 对已存在目录是空操作，只读路径常会顺带调用
        if not os.path.isdir(args[0]):
            check_write(args[0])
    elif event in _SRC_EVENTS:
        if _exists(args[0]):
            check_write(args[0])


def install():
    """在当前解释器装钩子（幂等）。只由 run_offline.py 调用。"""
    global _ENABLED, _INSTALLED
    _ENABLED = True
    if not _INSTALLED:
        sys.addaudithook(_hook)
        _INSTALLED = True


def reset_for_test():
    """测试用：清掉启用状态（audit hook 本身无法卸载）。"""
    global _ENABLED
    _ENABLED = False