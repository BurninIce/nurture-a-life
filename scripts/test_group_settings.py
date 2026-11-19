# -*- coding: utf-8 -*-
"""群参数（每群独立覆盖）离线回归。

跑法：
    .venv/Scripts/python.exe scripts/test_group_settings.py
    .venv/Scripts/python.exe scripts/test_group_settings.py -v

覆盖：
  - schema 合法（键唯一、类型/范围可用），发言门不进按群 schema
  - 覆盖值的存取、校验、清除、复制
  - 生效顺序：本群 > runtime_config > rp_config
  - rc.group_on 读本群开关，并兼容旧的 group_disabled 名单
  - 发言门（权重/冷却）忽略按群设置，旧文件里的残留键不生效
  - 主动开话题读本群参数
  - 上下文预算（历史条数 / 单条字数 / 成员摘要）读本群参数
  - 工具开关读本群参数
  - /group 指令的 status/set/settings/unset/copy/list
  - 配置页发言门数据与保存（保留触发动词、拒绝未知/空值）
全部离线：临时目录 + 替身，不碰线上 state/。
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import group_gate as gate            # noqa: E402
import group_scene                   # noqa: E402
import group_settings                # noqa: E402
import persona                       # noqa: E402
import rp_core as core               # noqa: E402
import rp_config as cfg              # noqa: E402
import runtime_config as rc          # noqa: E402
from tools import router             # noqa: E402
import bridge_commands               # noqa: E402

VERBOSE = False
RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok), detail))
    if VERBOSE or not ok:
        print('%s %s%s' % ('[PASS]' if ok else '[FAIL]', name,
                           ('  → ' + str(detail)[:160]) if detail else ''))
    return ok


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except ValueError:
        return True
    except Exception:
        return False
    return False


class _Msg(object):
    is_group = True
    gid = '111'
    uid = '1'


class _Cmd(object):
    args = argparse.Namespace(admin=['1'])
    _group_switch = bridge_commands.CommandMixin._group_switch


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument('-v', '--verbose', action='store_true')
    VERBOSE = ap.parse_args().verbose

    root = tempfile.mkdtemp(prefix='groupsettings_')
    saved = (group_settings.set_root(os.path.join(root, 'groups')),
             group_scene.set_root(os.path.join(root, 'groups')),
             persona.PERSONAS_STATE_ROOT, rc.STATE_PATH)
    try:
        persona.PERSONAS_STATE_ROOT = os.path.join(root, 'personas')
        rc.STATE_PATH = os.path.join(root, 'runtime_config.json')
        rc.load()
        rc._DATA.update({'group_enabled': True, 'group_disabled': [],
                         'tools_enabled': True, 'tool_router_group_at': False,
                         'group_turn_limit': 20, 'group_msg_max_chars': 200,
                         'member_brief_limit': 5})

        print('\n=== 1 schema ===')
        rows = group_settings.schema()
        keys = [s['key'] for s in rows]
        check('1.1 键唯一', len(keys) == len(set(keys)), keys)
        check('1.2 每项都有类型与分组',
              all(s.get('type') in ('bool', 'int', 'float') and s.get('group')
                  for s in rows))
        for must in ('enabled', 'proactive_enabled', 'tools_enabled',
                     'tool_router_group_at', 'gate_idle_sec',
                     'gate_proactive_daily_max', 'group_turn_limit',
                     'group_msg_max_chars', 'member_brief_limit'):
            check('1.3 包含 %s' % must, must in keys)
        check('1.4 发言门不进按群 schema',
              not any(s['group'] == '发言门' for s in rows)
              and not {'consider_at', 'w_question', 'group_cooldown',
                       'max_per_min'} & set(keys))

        print('\n=== 2 存取与校验 ===')
        check('2.1 初始没有覆盖', group_settings.overrides('111') == {})
        group_settings.set_many('111', {'gate_idle_sec': 900,
                                        'group_turn_limit': 12, 'enabled': True})
        ov = group_settings.overrides('111')
        check('2.2 写入覆盖', ov.get('gate_idle_sec') == 900
              and ov.get('group_turn_limit') == 12 and ov.get('enabled') is True, ov)
        check('2.3 读回单个值', group_settings.get('111', 'gate_idle_sec') == 900)
        check('2.4 没设过的键返回 None',
              group_settings.get('111', 'member_brief_limit') is None)
        check('2.5 未知键报错', raises(group_settings.set_many, '111', {'nope': 1}))
        check('2.6 非法值报错',
              raises(group_settings.set_many, '111', {'gate_idle_sec': 'abc'}))
        check('2.7 越界值夹紧',
              group_settings.set_many('111', {'group_turn_limit': 1})['group_turn_limit'] == 4)
        check('2.8 布尔字符串可解析',
              group_settings.set_many('111', {'enabled': 'off'})['enabled'] is False)
        check('2.9 非数字群号拒绝', raises(group_settings.set_many, 'private:1',
                                           {'enabled': True}))
        check('2.9b 发言门键不可按群设置',
              raises(group_settings.set_many, '111', {'group_cooldown': 90}))
        check('2.10 清除单个键', 'group_turn_limit'
              not in group_settings.unset('111', ['group_turn_limit']))
        group_settings.unset('111', 'all')
        check('2.11 清除全部后文件消失',
              group_settings.overrides('111') == {}
              and not os.path.exists(group_settings.path_for('111')))

        print('\n=== 3 复制 ===')
        group_settings.set_many('111', {'gate_idle_sec': 300, 'member_brief_limit': 3})
        group_settings.copy('111', '222')
        check('3.1 复制后目标一致',
              group_settings.overrides('222') == group_settings.overrides('111'),
              group_settings.overrides('222'))
        group_settings.set_many('222', {'gate_idle_sec': 60})
        check('3.2 复制后各自独立',
              group_settings.get('111', 'gate_idle_sec') == 300
              and group_settings.get('222', 'gate_idle_sec') == 60)
        check('3.3 不能复制给自己', raises(group_settings.copy, '111', '111'))

        print('\n=== 4 开关生效顺序 ===')
        check('4.1 默认跟随全局开启', group_settings.enabled('333') is True)
        group_settings.set_enabled('333', False)
        check('4.2 本群关闭后 group_on=False',
              group_settings.enabled('333') is False and rc.group_on('333') is False)
        rc._DATA['group_disabled'] = ['333']
        check('4.3 旧关闭名单仍生效（没有覆盖时）',
              group_settings.enabled('444', legacy_off=['444']) is False
              and group_settings.enabled('444') is True)
        group_settings.set_enabled('333', True)
        check('4.4 本群覆盖优先于旧名单',
              rc.group_on('333') is True)
        rc._DATA['group_disabled'] = []
        rc._DATA['group_enabled'] = False
        check('4.5 全局总开关优先', rc.group_on('333') is False)
        rc._DATA['group_enabled'] = True

        print('\n=== 5 全局回退值与生效值 ===')
        check('5.1 未覆盖时用全局',
              group_settings.effective('555', 'group_turn_limit') == 20)
        group_settings.set_many('555', {'group_turn_limit': 12})
        check('5.2 覆盖后生效值变化',
              group_settings.effective('555', 'group_turn_limit') == 12)
        check('5.3 其他项仍跟随全局',
              group_settings.effective('555', 'group_msg_max_chars') == 200)
        lim = group_settings.limits('555')
        check('5.4 limits 三项齐全',
              lim == {'turn_limit': 12, 'msg_max_chars': 200,
                      'member_brief_limit': 5}, lim)
        check('5.5 私聊 gid 不读群设置',
              group_settings.limits('private:1')['turn_limit'] == 20)

        print('\n=== 6 发言门（所有群共用）与主动 ===')
        now = time.time()
        ctx = {'gid': '111', 'now': now, 'last_reply_ts': now - 1,
               'reply_ts_list': [now - 1], 'turns': [{'ts': now - 1}],
               'last_proactive_ts': now - 3600, 'proactive_today': 0}
        # 旧版本可能把发言门写进 settings.json；现在这些键必须被忽略。
        os.makedirs(group_settings.group_dir('111'), exist_ok=True)
        with open(group_settings.path_for('111'), 'w', encoding='utf-8') as f:
            json.dump({'group_cooldown': 120, 'w_question': 0.33}, f)
        check('6.1 旧文件里的发言门键被忽略',
              group_settings.overrides('111') == {}, group_settings.overrides('111'))
        tune = persona.gate_tuning()
        want_cd = tune.get('group_cooldown', cfg.GATE_GROUP_COOLDOWN)
        wait = gate.cooldown_remaining(ctx, 'u1', now)
        check('6.2 冷却用角色卡/全局值，不看本群文件',
              abs(wait - max(0, want_cd - 1)) < 1.5, (wait, want_cd))
        hit = gate.cooldown_hit(ctx, 'u1', now)
        check('6.3 冷却命中用共用值', hit == ('group_cooldown' if want_cd > 1 else None),
              hit)
        check('6.4 权重用角色卡值',
              gate._tune_val('w_question', cfg.GATE_W_QUESTION)
              == tune.get('w_question', cfg.GATE_W_QUESTION))
        check('6.5 门槛用角色卡值',
              gate._tune_val('consider_at', cfg.GATE_CONSIDER_AT)
              == tune.get('consider_at', cfg.GATE_CONSIDER_AT))
        group_settings.set_many('111', {'gate_idle_sec': 9999})
        ok, why = gate.proactive_due(ctx)
        check('6.6 静默阈值读本群值', not ok and why == 'not_idle', (ok, why))
        ctx2 = dict(ctx, turns=[{'ts': now - 3600}], last_proactive_ts=now - 7200)
        group_settings.set_many('111', {'gate_idle_sec': 60,
                                        'gate_proactive_daily_max': 0})
        ok, why = gate.proactive_due(ctx2)
        check('6.7 每日上限读本群值', not ok and why == 'daily_quota', (ok, why))

        print('\n=== 7 上下文预算 ===')
        turns = [{'nickname': '甲', 'text': '一' * 300, 'ts': 0}]
        msgs = core.build_messages('S', turns=turns, limit=3, max_chars=80)
        check('7.1 单条消息按本群字数截断',
              len(msgs[1]['content']) < 120 and msgs[1]['content'].endswith('…'),
              msgs[1]['content'][:20])
        msgs_default = core.build_messages('S', turns=turns, limit=3)
        check('7.2 未传时用全局字数',
              len(msgs_default[1]['content']) < cfg.GROUP_MSG_MAX_CHARS + 30,
              len(msgs_default[1]['content']))
        many = [{'nickname': '甲', 'text': str(i), 'ts': i} for i in range(10)]
        check('7.3 历史条数按传入上限',
              len(core.build_messages('S', turns=many, limit=4)) == 5)

        print('\n=== 8 工具开关 ===')
        orig_enabled = router._tool_enabled
        router._tool_enabled = lambda name: True
        try:
            group_settings.set_many('111', {'tools_enabled': False})
            check('8.1 本群关工具后不路由',
                  router.route('帮我查一下今天天气', is_group=True,
                               at_self=True, gid='111') is None)
            group_settings.set_many('111', {'tools_enabled': True,
                                            'tool_router_group_at': False})
            check('8.2 本群旁听不路由',
                  router.route('帮我查一下今天天气', is_group=True,
                               at_self=False, gid='111') is None)
            check('8.3 本群 @ 时路由',
                  router.route('帮我查一下今天天气', is_group=True,
                               at_self=True, gid='111') is not None)
            check('8.4 其他群跟随全局',
                  router.route('帮我查一下今天天气', is_group=True,
                               at_self=True, gid='999') is not None)
        finally:
            router._tool_enabled = orig_enabled

        print('\n=== 9 /group 指令 ===')
        group_settings.unset('111', 'all')
        group_settings.unset('222', 'all')
        group_settings.set_many('222', {'gate_idle_sec': 300})
        obj = _Cmd()
        msg = _Msg()
        r = obj._group_switch(msg, ['set', 'gate_idle_sec', '120'])
        check('9.1 set 写入覆盖',
              group_settings.get('111', 'gate_idle_sec') == 120, r)
        r = obj._group_switch(msg, ['settings'])
        check('9.2 settings 列出覆盖', '静默阈值=120' in r, r)
        r = obj._group_switch(msg, ['unset', 'gate_idle_sec'])
        check('9.3 unset 恢复跟随',
              group_settings.get('111', 'gate_idle_sec') is None, r)
        r = obj._group_switch(msg, ['copy', '222'])
        check('9.4 copy 复制其他群',
              group_settings.get('111', 'gate_idle_sec') == 300, r)
        r = obj._group_switch(msg, ['set', 'nope', '1'])
        check('9.5 set 未知键有回执', '未知参数' in r, r)
        r = obj._group_switch(msg, ['set'])
        check('9.5b set 缺参列出可设键名',
              '可设参数' in r and 'gate_idle_sec' in r, r)
        r = obj._group_switch(msg, ['list'])
        check('9.6 list 列出已知群', '111' in r and '222' in r, r)

        print('\n=== 10 面板数据 ===')
        import admin_payloads
        page = admin_payloads._group_settings_payload('111')
        check('10.1 参数页含 schema/生效值/覆盖',
              page['gid'] == '111' and page['schema']
              and page['values'].get('gate_idle_sec') == 300
              and page['overrides'].get('gate_idle_sec') == 300, page.get('gid'))
        check('10.2 参数页拒绝非数字群号',
              raises(admin_payloads._group_settings_payload, 'abc'))
        check('10.2b 参数页不含发言门',
              not any(s['group'] == '发言门' for s in page['schema']))
        groups = admin_payloads._groups_payload()['groups']
        check('10.3 群列表含覆盖项数',
              any(g['gid'] == '111' and g['count'] >= 1 for g in groups),
              groups[:3])
        gp = admin_payloads._group_payload('111')
        check('10.4 群氛围按 gid 读取', gp.get('gid') == '111', gp.get('gid'))

        print('\n=== 11 面板保存入口 ===')
        import admin_server

        class _Stub(object):
            _read_body = lambda self: json.dumps(self.body).encode('utf-8')
            _send = lambda self, code, obj: setattr(self, 'sent', (code, obj))
            _group_settings_save = admin_server.Handler._group_settings_save

        stub = _Stub()
        stub.body = {'gid': '111', 'set': {'gate_idle_sec': 300}}
        stub._group_settings_save()
        check('11.1 PUT set 落盘并回新参数页',
              stub.sent[0] == 200
              and stub.sent[1]['overrides'].get('gate_idle_sec') == 300,
              stub.sent[0])
        stub.body = {'gid': '111', 'unset': 'all'}
        stub._group_settings_save()
        check('11.2 PUT unset all 清空',
              stub.sent[0] == 200 and stub.sent[1]['overrides'] == {},
              stub.sent[0])
        stub.body = {'gid': 'abc', 'set': {'gate_idle_sec': 1}}
        stub._group_settings_save()
        check('11.3 PUT 非数字群号 400', stub.sent[0] == 400, stub.sent)
        stub.body = {'gid': '111'}
        stub._group_settings_save()
        check('11.4 PUT 缺动作 400', stub.sent[0] == 400, stub.sent)
        stub.body = {'gid': '111', 'copy_from': '222'}
        stub._group_settings_save()
        check('11.5 PUT copy_from 生效',
              stub.sent[0] == 200
              and stub.sent[1]['overrides'].get('gate_idle_sec') == 300,
              stub.sent[0])

        print('\n=== 12 发言门（配置页）===')
        # 发布版本版不含「发言门移入配置页」（属于开发版 11-11 的第二批），
        # 发言门仍由角色卡 gate 调参维护，故这里跳过对应断言。
        print('  [skip] 发布版暂未提供 /api/gate 配置页')

    finally:
        group_settings.set_root(saved[0])
        group_scene.set_root(saved[1])
        persona.PERSONAS_STATE_ROOT = saved[2]
        rc.STATE_PATH = saved[3]
        rc.load()
        shutil.rmtree(root, ignore_errors=True)

    failed = [x for x in RESULTS if not x[1]]
    print('\n群参数测试：%d/%d 通过' % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        for name, _, detail in failed:
            print('  - %s %s' % (name, detail))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
