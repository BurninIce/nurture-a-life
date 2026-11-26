# -*- coding: utf-8 -*-
"""进程心跳与状态文件的唯一读写入口。

为什么单独一个模块：桥接写心跳、守护进程读心跳并写自己的状态、管理面板
两者都读。路径与原子写逻辑只能有一份，否则一边 os.replace、另一边直接
open('w')，面板就会偶尔读到半截 JSON。

约束：
  - 只在调用 write 时创建目录，import 无副作用；
  - 读失败一律返回 None，可观测性不能反过来拖垮主流程；
  - 写入用临时文件 + os.replace，Windows 上同目录替换是原子的。
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE_DIR = os.path.join(ROOT, 'state')

BRIDGE_PATH = os.path.join(STATE_DIR, 'bridge_heartbeat.json')
SUPERVISOR_PATH = os.path.join(STATE_DIR, 'supervisor.json')


def write(path, data):
    """原子写 JSON。失败静默返回 False（调用方都是后台线程/守护循环）。"""
    tmp = path + '.tmp'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def read(path):
    """读 JSON；不存在/损坏/不是对象都返回 None。"""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def age(data, now=None):
    """心跳年龄（秒）。没有 ts 时返回 None。"""
    if not isinstance(data, dict):
        return None
    try:
        ts = float(data.get('ts') or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return max(0.0, (now if now is not None else time.time()) - ts)


def fresh(data, max_age=45.0, now=None):
    """心跳是否仍在有效期内。"""
    a = age(data, now)
    return a is not None and a <= max_age
