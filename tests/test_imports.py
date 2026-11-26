# -*- coding: utf-8 -*-
"""冒烟测试：运行时模块全部可导入（不依赖角色卡、模型与外部服务）。

为什么这里要列这么多：`self_state` 这类模块被 `rp_core` 顶层 import，
发布包里少一个文件，整个项目就起不来。所以"能不能导入"是发布完整性的最低门槛。
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

MODULES = [
    # 路径 / 配置 / 角色卡
    'paths',
    'logutil',
    'rp_config',
    'persona',
    'card_source',
    'runtime_config',
    'stat_rules',
    # 记忆
    'memory_util',
    'memory_format',
    'memory_retrieval',
    'memory_card',
    'embedding',
    # 群聊 / 多人
    'member_card',
    'group_activity',
    'group_gate',
    'group_roster',
    'group_scene',
    'group_settings',
    # 日常生成与自我状态
    'daily_scene',
    'day_life',
    'self_state',
    # 人格组装与传输
    'rp_common',
    'rp_core',
    'bridge_message',
    'bridge_commands',
    'bridge_transport',
    'qq_bridge',
    # 检索 / 面板 / 语音
    'lore_rag',
    'corpus_rag',
    'novel_rag',
    'private_topic',
    'admin_payloads',
    'admin_server',
    'cg_profile',
    'tts_state',
]


def test_all_modules_import():
    failed = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:                      # noqa: BLE001
            failed.append('%s: %s: %s' % (name, type(e).__name__, e))
    assert not failed, '导入失败：\n  ' + '\n  '.join(failed)
