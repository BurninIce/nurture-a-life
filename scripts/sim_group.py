# -*- coding: utf-8 -*-
"""8 人群聊离线模拟器：验证「每人一张卡、记忆不串味、上下文预算」。

用途（不需要 QQ，也不冒封号风险）：
    py -3.13 scripts/sim_group.py            # 结构测试：不调模型（快，秒级）
    py -3.13 scripts/sim_group.py --llm      # 真实链路：走 Ollama（需模型已加载）

为什么先离线：8 人场景决定性的失败模式是
  ① 别人的隐私进了我的记忆卡 ② 认错发言人 ③ 8 张成员卡把上下文挤爆，
这三件在离线阶段就能全部暴露，且不受 QQ 消息频率/封号风险限制。

产出（默认 state/sim_group/，不碰真实 state/）：
    users/<uid>/memory_card.json  每人一份记忆
    users/<uid>/member.json       每人一份成员卡
    trace.jsonl                   每轮：说话人、system 长度与分块、回复、耗时
    systems/NNN.txt               每轮实际 system 原文（可直接翻看排错）

断言：
    A1 每个 uid 的记忆卡只含自己的秘密线索，绝不含别人的
    A2 每人都有 member.json，昵称正确
    A3 群成员注入 ≤ MEMBER_BRIEF_LIMIT 人（默认 4），且当前发言人必在其中
    A4 交叉指代轮：被提到的人也在成员块里（指代可解析）
    A5 system 长度峰值在预算内（默认 8000 字符）
"""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import group_activity as activity
import group_gate as gate
import group_roster
import memory_card as mem
import member_card
import persona
import rp_config as cfg
import rp_core as core

# ---------- 8 位群友（真人设定：只有客观信息，没有人格扮演设定） ----------
# secret: 每人的独有事实线索，用于断言"记忆不串味"
MEMBERS = [
    {'uid': '1001', 'nickname': '群友A', 'identity': '在读研究生',
     'style': '短句，爱反问', 'secret': '我最近在学口琴'},
    {'uid': '1002', 'nickname': '群友C', 'identity': '建筑设计师',
     'style': '说话慢，爱打比方', 'secret': '我养了只叫豆豆的柯基'},
    {'uid': '1003', 'nickname': '群友B', 'identity': '后端程序员',
     'style': '喜欢抬杠，语速快', 'secret': '我下周三要去拔智齿'},
    {'uid': '1004', 'nickname': '小满', 'identity': '医学生',
     'style': '语气温和，爱用书面词', 'secret': '我昨晚值夜班到凌晨两点'},
    {'uid': '1005', 'nickname': '阿泽', 'identity': '健身教练',
     'style': '大嗓门，爱喊口号', 'secret': '我晚上十点必睡'},
    {'uid': '1006', 'nickname': '桃子', 'identity': '插画师',
     'style': '爱用叠词，情绪外放', 'secret': '我在画一套猫的明信片'},
    {'uid': '1007', 'nickname': '阿岚', 'identity': '咖啡师',
     'style': '话少，句子短', 'secret': '我最近在学折纸'},
    {'uid': '1008', 'nickname': '老白', 'identity': '中学老师',
     'style': '爱讲道理，句尾带"啊"', 'secret': '我班上有三十七个学生'},
]
BY_UID = {m['uid']: m for m in MEMBERS}
BY_NICK = {m['nickname']: m for m in MEMBERS}


def build_script(rounds=1):
    """按轮次生成发言脚本。rounds=2 时第二轮加入交叉指代与记忆召回。

    每条：{'nick': 昵称, 'text': 原话}
    """
    script = []
    # 第一轮：各自抛出一条独有事实（记忆写入材料）
    intros = {
        '群友A': '最近忙到不行，我工位在走廊最里头，一坐一整天。',
        '群友C': '画图画到眼花啦。对了，我养了只叫豆豆的柯基，天天拆家。',
        '群友B': '别提了，我下周三要去拔智齿，想着就牙疼。',
        '小满': '刚从医院回来，我昨晚值夜班到凌晨两点，现在脑子是空的。',
        '阿泽': '状态不错！不过我晚上十点必睡，熬夜对肌肉不好。',
        '桃子': '我在画一套猫的明信片～等画完了给你们看呀。',
        '阿岚': '我最近在学折纸，折出来的鹤总是歪的。',
        '老白': '今天又被学生气到了啊，我班上有三十七个学生，一个比一个能闹。',
    }
    for m in MEMBERS:
        script.append({'nick': m['nickname'], 'text': intros[m['nickname']]})

    if rounds >= 2:
        script += [
            {'nick': '群友A', 'text': '@群友C 你刚说的那只柯基叫啥来着？我这记性。'},
            {'nick': '群友C', 'text': '豆豆呀，它今天又把拖鞋叼走了。'},
            {'nick': '群友B', 'text': '@角色 你还记得我刚才说要干嘛吗？'},
            {'nick': '小满', 'text': '群友B要去拔智齿吧，我记得他说过。'},
            {'nick': '桃子', 'text': '角色你觉得画猫的明信片用什么颜色好呀？'},
            {'nick': '阿岚', 'text': '颜色别太亮，纸会吃掉一层。'},
            {'nick': '老白', 'text': '你们年轻人真会玩啊，我只会批作业。'},
            {'nick': '阿泽', 'text': '批作业也是技术活！'},
        ]
    if rounds >= 3:
        script += [
            {'nick': '群友B', 'text': '@角色 你偏心吧，怎么老搭理群友A不搭理我。'},
            {'nick': '群友A', 'text': '我这不是离得近嘛，工位就在走廊那头。'},
            {'nick': '小满', 'text': '话说回来，角色你平时都什么时候休息？'},
            {'nick': '桃子', 'text': '对对对，我也想问这个！'},
            {'nick': '群友C', 'text': '问这么细干嘛，人家有隐私的。'},
            {'nick': '老白', 'text': '群友C这话说得在理啊。'},
            {'nick': '阿泽', 'text': '行了行了，都别追问了。'},
            {'nick': '阿岚', 'text': '我把咖啡端上来了。'},
            # 记忆命中素材：他重复自己说过的事（走廊 → 她的记忆里有）
            {'nick': '群友A', 'text': '走廊那片空调还是没修好，我快热化了。'},
            # 她本行的话题（时间线）：更容易接 + 允许展开
            {'nick': '群友C', 'text': '说真的，时间这种东西要是能倒回去就好了。'},
        ]
    return script


def fake_gen_llm(prompt, model=None, think=None, channel='local', num_predict=300):
    """--no-llm 模式的确定性替身：只模拟"边界判定/重归纳"两个 LLM 调用。

    不伪造内容——事件里的 user_said 来自真实缓冲文本（因此隔离断言依然有效），
    只是把"话题是否结束"和"归档结果"固定下来，好让记忆代码路径真正跑一遍。
    """
    if '话题是否已结束' in prompt or '必须' in prompt:
        return json.dumps({'action': 'close', 'worth': True, 'episode': {
            'summary': '群聊片段（模拟判定）', 'scene': '群聊', 'emotion': '平静',
            'importance': 2, 'tags': ['模拟']}}, ensure_ascii=False)
    if '记忆归档' in prompt:
        return json.dumps({'affection': 40, 'trust': 35, 'familiarity': 10,
                           'relationship': '相识', 'impressions': [], 'facts': [],
                           'self_disclosure': [], 'reflections': []}, ensure_ascii=False)
    return ''


def setup_root(root):
    """把 rp_core/member_card 的写入根目录切到模拟目录（绝不碰真实 state/）。"""
    users = os.path.join(root, 'users')
    os.makedirs(users, exist_ok=True)
    persona.PERSONAS_STATE_ROOT = os.path.join(root, 'personas')
    core.USERS_ROOT = users
    core.STATE_ROOT = root
    member_card.set_users_root(users)
    activity.set_root(os.path.join(root, 'groups'))
    group_roster.set_root(os.path.join(root, 'groups'))
    return users


def recent_speakers(turns, current, limit=None):
    """最近发言者（当前发言人排最前），供成员块做预算裁剪。"""
    limit = limit or cfg.MEMBER_BRIEF_LIMIT
    out = [{'uid': current['uid'], 'nickname': current['nickname']}]
    seen = {current['uid']}
    for t in reversed(turns):
        if t.get('self'):
            continue
        uid = t.get('uid')
        if uid and uid not in seen:
            seen.add(uid)
            out.append({'uid': uid, 'nickname': t.get('nickname')})
        if len(out) >= limit:
            break
    return out


def member_section(system):
    """取出 system 里【群成员】块原文（到下个空行块为止）。"""
    if cfg.MEMBER_HEADER not in system:
        return ''
    return system.split(cfg.MEMBER_HEADER, 1)[1].split('\n\n', 1)[0]


def ollama_alive(timeout=3):
    """预检：Ollama 是否可达（--llm 模式必需）。"""
    import urllib.request

    import paths
    url = 'http://127.0.0.1:%d/api/tags' % paths.PORT_OLLAMA
    try:
        urllib.request.urlopen(url, timeout=timeout).read()
        return True
    except Exception:
        return False


def run(args):
    root = os.path.abspath(args.root)
    if args.llm and not ollama_alive():
        print('Ollama 未运行：先启动（双击 start.bat 会拉起，或运行 ollama.exe serve）')
        print('或去掉 --llm，先跑结构测试（不需要模型）。')
        return 2
    if args.clean and os.path.isdir(root):
        shutil.rmtree(root)
    users = setup_root(root)
    sysdir = os.path.join(root, 'systems')
    os.makedirs(sysdir, exist_ok=True)

    if not args.llm:
        mem._gen_llm = fake_gen_llm  # 确定性替身（真实链路 --llm 时不打桩）

    script = build_script(args.rounds)
    trace = open(os.path.join(root, 'trace.jsonl'), 'w', encoding='utf-8')
    turns = []
    peak = 0
    latencies = []
    checks = []
    system_texts = {}

    print('=== 8 人群聊模拟（%s）===' % ('真实模型链路' if args.llm else '结构测试：不调模型'))
    print('根目录：%s' % root)
    print('脚本轮数：%d 条发言\n' % len(script))

    judge = None
    if args.gate:
        if args.llm:
            judge = gate.make_llm_judge(args.model)
        else:
            # 结构模式下用"挑剔替身"：只接问句/她感兴趣的话题（近似真实模型的选择性）
            def judge(t, n, c):
                like = any(h in t for h in ('吗', '呢', '么', '?', '？')) or \
                    any(w in t for w in persona.gate().get('interest_words', ()))
                return like, ('想接' if like else '与我无关')
        print('决策层：%s（阈值 L1≥%.2f 进终审）\n'
              % ('L1+L2 真实模型终审' if args.llm else 'L1+L2 挑剔替身', cfg.GATE_CONSIDER_AT))

    gctx = {'turns': [], 'relation': '相识'}
    gstats = {}
    gbase = time.time()
    decision_path = os.path.join(root, 'decisions.jsonl')
    replied_uids = set()

    for i, step in enumerate(script, 1):
        m = BY_NICK[step['nick']]
        uid = m['uid']
        now = gbase + (i - 1) * args.gap
        core.note_speaker(uid, m['nickname'], group_id=args.gid)
        activity.record(args.gid, now)  # 群活跃画像（主动开话题的时间约束靠它学习）

        # 决策层：要不要接这一句（沉默的轮次完全不产生记忆——她没参与）
        if args.gate:
            gctx['now'] = now
            # 她对当前发言人的记忆条目 → 决策层判断"这是不是聊过的事"
            gctx['memory_snippets'] = core.memory_snippets(uid)
            hours, ainfo = activity.active_hours(args.gid, now=now)
            gctx['active_hours'] = hours
            gctx['active_source'] = ainfo['source']
            gctx['neglected'] = bool(gctx.get('last_reply_ts')) and \
                now - gctx['last_reply_ts'] > 300
            d = gate.decide(step['text'], uid=uid, nickname=m['nickname'],
                            ctx=gctx, judge=judge, stats=gstats)
            gate.log_decision(decision_path, {
                'turn': i, 'uid': uid, 'nickname': m['nickname'],
                'text': step['text'], 'action': d.action, 'score': d.score,
                'signals': d.signals, 'reason': d.reason})
            gate.note_seen(gctx, uid, step['text'], now)  # 重复检测（重投/刷屏）
            if d.action != gate.REPLY:
                gctx['turns'].append({'nickname': m['nickname'], 'text': step['text'],
                                      'ts': now})
                turns.append({'uid': uid, 'nickname': m['nickname'], 'text': step['text'],
                              'ts': now})
                print('[%02d] %s：%s' % (i, m['nickname'], step['text'][:26]))
                print('     ── 沉默（L1 %.2f / %s）' % (d.score, d.reason))
                continue

        speakers = recent_speakers(turns, {'uid': uid, 'nickname': m['nickname']})
        # @ 提到的人（含自然称呼）：优先注入成员块
        # 真实群聊里 @ 由 OneBot 消息段提供；不带 @ 的称呼靠文本命中（两者都要）
        roster = [{'uid': x['uid'], 'nickname': x['nickname']} for x in MEMBERS]
        mentions = core.mentions_from_text(step['text'], roster)
        blocks = []
        t0 = time.time()
        system = core.build_system(
            step['text'], model_key=args.model, uid=uid, speakers=speakers,
            mentions=mentions, group_note=True, include_daily=False,
            include_rag=args.llm, block_log=blocks,
            prev_reply=core.last_self_text(turns))
        messages = core.build_messages(system, message=step['text'],
                                       nickname=m['nickname'], message_ts=now,
                                       turns=turns)
        if args.llm:
            # 熟悉话题给更长篇幅（与 bridge 一致：展开讨论需要 token 空间）
            reply = core.generate(messages, model_key=args.model,
                                  options=core.build_options(
                                      expert=core.is_expert_topic(step['text'])))
        else:
            reply = '（结构测试回复）'
        dt = (time.time() - t0) * 1000
        latencies.append(dt)
        peak = max(peak, len(system))
        system_texts[i] = system
        with open(os.path.join(sysdir, '%03d.txt' % i), 'w', encoding='utf-8') as f:
            f.write(system)

        # 记忆后处理（真实代码路径；--no-llm 时边界/归纳走确定性替身）
        info = core.post_turn(uid, step['text'], reply, model_key=args.model,
                              speaker=m['nickname'], is_group=True)
        replied_uids.add(uid)

        turns.append({'uid': uid, 'nickname': m['nickname'], 'text': step['text']})
        turns.append({'self': True, 'text': reply})
        if args.gate:
            gctx['turns'].append({'nickname': m['nickname'], 'text': step['text'], 'ts': now})
            gctx['turns'].append({'self': True, 'text': reply, 'ts': now})
            gctx['last_reply_ts'] = now
            gctx['her_last_ts'] = now
            gctx['her_last_text'] = reply
            gctx.setdefault('last_reply_by_uid', {})[uid] = now
            gctx.setdefault('reply_ts_list', []).append(now)
        trace.write(json.dumps({
            'turn': i, 'uid': uid, 'nickname': m['nickname'], 'text': step['text'],
            'reply': reply, 'system_len': len(system), 'blocks': blocks,
            'expert': core.is_expert_topic(step['text']),
            'ms': round(dt), 'action': info.get('action'),
            'episode': info.get('episode'), 'rounds_buffered': info.get('buffer_len'),
        }, ensure_ascii=False) + '\n')
        print('[%02d] %s：%s' % (i, m['nickname'], step['text'][:26]))
        print('     角色：%s' % (reply.replace('\n', ' ')[:50] or '（空）'))
        print('     system %d 字 / %d ms / 记忆动作=%s' % (
            len(system), round(dt), info.get('action')))
    trace.close()

    # ---------- 断言 ----------
    def check(name, cond, detail=''):
        checks.append((name, bool(cond), detail))
        print('[%s] %s%s' % ('PASS' if cond else 'FAIL', name,
                             ('  ← ' + detail) if detail and not cond else ''))

    print('\n=== 断言 ===')
    # A1 记忆隔离。三种检查分开看（gate 模式下"记不记得"取决于她有没有接话）：
    #   a) 被回过的人必须有记忆文件，且其中不含**任何他人**的秘密
    #   b) 至少有人记住了自己的线索（证明记忆链路真的在工作）
    #   c) 没被回过话的人不应产生事件（A6）
    leaks = []
    missing = []
    silent_with_memory = []
    remember_own = []
    for m in MEMBERS:
        p = os.path.join(users, m['uid'], 'memory_card.json')
        touched = (not args.gate) or (m['uid'] in replied_uids)
        if not os.path.exists(p):
            if touched:
                missing.append(m['nickname'])
            continue
        with open(p, encoding='utf-8') as f:
            raw = f.read()
        for other in MEMBERS:
            if other['uid'] != m['uid'] and other['secret'] in raw:
                leaks.append('%s 的记忆里出现了 %s 的「%s」'
                             % (m['nickname'], other['nickname'], other['secret']))
        if not touched:
            try:
                eps = json.loads(raw).get('episodes') or []
            except ValueError:
                eps = []
            if eps:
                silent_with_memory.append('%s(%d 条)' % (m['nickname'], len(eps)))
            continue
        if m['secret'] in raw:
            remember_own.append(m['nickname'])
    check('A1 被回过话的人都有记忆文件', not missing, '; '.join(missing[:3]))
    check('A1 记忆不串味（无他人隐私）', not leaks, '; '.join(leaks[:3]))
    check('A1 至少有人记住自己的线索', bool(remember_own),
          '记住自己线索的人：' + ('、'.join(remember_own) or '无'))
    if args.gate:
        check('A6 没接过话的人不产生记忆', not silent_with_memory,
              '; '.join(silent_with_memory[:3]))

    # A2 成员卡
    bad = [m['nickname'] for m in MEMBERS
           if member_card.load(m['uid']).get('nickname') != m['nickname']]
    check('A2 成员卡昵称正确', not bad, ','.join(bad))

    # A3 成员注入预算 + 当前发言人必在
    over = []
    absent = []
    for i, sys_text in system_texts.items():
        lines = [ln for ln in member_section(sys_text).splitlines() if ln.startswith('- ')]
        if not lines:
            absent.append('第%d轮成员块为空' % i)
        elif len(lines) > cfg.MEMBER_BRIEF_LIMIT:
            over.append('第%d轮注入%d人' % (i, len(lines)))
        elif script[i - 1]['nick'] not in member_section(sys_text):
            absent.append('第%d轮缺当前发言人' % i)
    check('A3 成员注入 ≤ %d 人' % cfg.MEMBER_BRIEF_LIMIT, not over, '; '.join(over[:3]))
    check('A3 当前发言人必在成员块', not absent, '; '.join(absent[:3]))

    # A4 交叉指代轮：被提到的人（@ 或自然称呼）也在成员块（指代可解析）
    cross_bad = []
    for i, s in enumerate(script, 1):
        if i not in system_texts:  # 沉默轮：没构建 system，无从检查
            continue
        mentioned = [n for n in BY_NICK if n in s['text'] and n != s['nick']]
        if not mentioned:
            continue
        sec = member_section(system_texts[i])
        for target in mentioned:
            if target not in sec:
                cross_bad.append('第%d轮 %s 未进成员块' % (i, target))
    check('A4 被提到的人已注入成员块', not cross_bad, '; '.join(cross_bad[:3]))

    # A5 上下文预算
    budget = args.budget
    check('A5 system 峰值 %d 字 < %d' % (peak, budget), peak < budget)

    print('\n=== 统计 ===')
    print('system 峰值：%d 字（预算 %d）' % (peak, budget))
    if args.llm and latencies:
        lat = sorted(latencies)
        print('单轮耗时：中位 %d ms / 最慢 %d ms' % (lat[len(lat) // 2], lat[-1]))
    else:
        print('单轮耗时：中位 %d ms（结构测试，未调模型）'
              % sorted(latencies)[len(latencies) // 2])
    total = len(checks)
    passed = len([c for c in checks if c[1]])
    print('断言：%d/%d 通过' % (passed, total))
    print('群活跃画像：%s' % activity.summary(args.gid))
    print('逐轮 system 原文：%s' % sysdir)
    if not args.keep and passed == total:
        print('（保留本次产物便于查看；如需清理：--clean 重跑）')
    return 0 if passed == total else 1


def main():
    ap = argparse.ArgumentParser(description='8 人群聊离线模拟（验证多用户记忆与上下文预算）')
    ap.add_argument('--llm', action='store_true', help='走真实模型链路（默认只做结构测试）')
    ap.add_argument('--rounds', type=int, default=2, help='脚本轮数 1/2/3（默认 2）')
    ap.add_argument('--model', default=cfg.MODEL_DEFAULT, help='模型注册表 key')
    ap.add_argument('--root', default=os.path.join(core.STATE_ROOT, 'sim_group'),
                    help='模拟根目录（默认 state/sim_group，不碰真实 state）')
    ap.add_argument('--gid', default='900001', help='模拟群号')
    ap.add_argument('--budget', type=int, default=8000, help='system 长度上限（字符）')
    ap.add_argument('--gate', action='store_true',
                    help='启用群发言决策层（不是每条都回；沉默的轮次不写记忆）')
    ap.add_argument('--gap', type=float, default=6.0,
                    help='决策层用的消息间隔秒数（合成时钟，默认 6）')
    ap.add_argument('--clean', action='store_true', help='重跑前清空模拟根目录')
    ap.add_argument('--keep', action='store_true', help='保留产物（默认即保留，此参数仅表意）')
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == '__main__':
    main()
