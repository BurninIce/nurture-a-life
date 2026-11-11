# -*- coding: utf-8 -*-
"""媒体链路自测：CG / TTS。由 qq_bridge.selftest 在临时目录里调用。

单独成文件是解耦的一部分：qq_bridge.py 不再出现 cg_gen / tts_client 这类媒体实现，
媒体实现与它的测试都留在支线层。
"""
import os
import time

import cg_gen
import rp_config as cfg
import runtime_config as rc
import tts_client
import tts_state
from rp_common import strip_for_tts


def setup(root):
    """自测隔离：TTS 会话状态与 CG 账本都写临时目录。"""
    tts_state.set_root(os.path.join(root, 'tts'))
    cg_gen.set_state_root(os.path.join(root, 'cg'))
    cg_gen.reset_for_test()


def run_cg(br, link, args, root, Msg, group_ev, drain, check):
    """6b*：/cg 指令链路、配额与回图（不真连 ComfyUI）。"""
    import comfy_client

    fake_img = os.path.join(root, 'fake_cg.png')
    with open(fake_img, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + b'0' * 20000)
    orig_cg_generate = cg_gen.generate
    orig_cg_describe = comfy_client.describe
    orig_cg_stage = cg_gen.stage_for
    cg_calls = []

    def _fake_cg(uid, stage=None, **kw):
        try:
            cg_calls.append({'uid': str(uid), 'stage': stage,
                             'extra': kw.get('extra') or ''})
            return {'ok': True, 'path': fake_img, 'stage': stage or '相识'}
        finally:
            cg_gen.release()

    def _send_cg(uid, nick, mid, text='/cg'):
        before = len(link.sent)
        br.on_event({'post_type': 'message', 'message_type': 'private',
                     'self_id': 999999, 'user_id': uid, 'message_id': mid,
                     'time': time.time(), 'sender': {'nickname': nick},
                     'message': [{'type': 'text', 'data': {'text': text}}]})
        deadline = time.time() + 3
        got = []
        while time.time() < deadline:
            drain()
            got = link.sent[before:]
            if any(len(x) > 4 and x[4] for x in got):
                break
            time.sleep(0.01)
        return got

    def _clear_quota(uid):
        """自测里连发多条 /cg，清掉冷却额度，避免被限额拦住。"""
        rows = cg_gen._load_ledger()
        rows.pop(str(uid), None)
        cg_gen._save_ledger()

    cg_gen.generate = _fake_cg
    comfy_client.describe = lambda: {
        'url': 'http://127.0.0.1:8188',
        'workflow': {'path': '<selftest>', 'exists': True}}
    try:
        args.admin = ['1001']
        cg_gen.reset_for_test()
        _clear_quota(1009)
        got = _send_cg(1009, '私聊的人', 1071)
        check('6b /cg → 先回执再回图',
              any(len(x) > 4 and x[4] == fake_img for x in got), got)

        _clear_quota(1009)
        got = _send_cg(1009, '私聊的人', 1072)
        check('6b2 再次 /cg → 重新生成而非复用',
              len(cg_calls) == 2
              and any(len(x) > 4 and x[4] == fake_img for x in got), got)

        _clear_quota(1009)
        cg_gen.stage_for = lambda uid: '熟络'
        got = _send_cg(1009, '私聊的人', 1073)
        check('6b3 跃升后 → 按新阶段重画',
              cg_calls[-1]['stage'] == '熟络'
              and any(len(x) > 4 and x[4] == fake_img for x in got),
              cg_calls[-1])

        cg_gen.stage_for = lambda uid: '相识'
        _clear_quota(1001)
        before = len(link.sent)
        br.on_event(group_ev(1001, '群友A', '/cg', 1074, at=True))
        deadline = time.time() + 3
        got = []
        while time.time() < deadline:
            drain()
            got = link.sent[before:]
            if any(len(x) > 4 and x[4] for x in got):
                break
            time.sleep(0.01)
        check('6b4 群里 /cg 只画发言者自己的图',
              cg_calls[-1]['uid'] == '1001'
              and any(len(x) > 4 and x[4] == fake_img for x in got),
              cg_calls[-1])

        cg_gen.reset_for_test()
        ledger = cg_gen._load_ledger()
        ledger['1001'] = [time.time()]
        cg_gen._save_ledger()
        got = _send_cg(1001, '管理员', 1077, '/cg new')
        check('6b5 /cg new → 管理员强制新图并跳过冷却',
              cg_calls[-1]['extra'] == ''
              and any(len(x) > 4 and x[4] == fake_img for x in got), got)

        got = _send_cg(1001, '管理员', 1078, '/cg 她回头看镜头')
        check('6b6 /cg <内容> → 作为额外要求传给生成层',
              cg_calls[-1]['extra'] == '她回头看镜头'
              and any(len(x) > 4 and x[4] == fake_img for x in got), got)

        before = len(link.sent)
        br.handle_command(Msg('1001', '管理员', '', '/cg ' + '长' * 401,
                              1079, is_group=False))
        too_long = link.sent[before:]
        check('6b7 /cg <内容> 超长 → 直接拒绝',
              any('太长' in str(x) for x in too_long), too_long)
        args.admin = []
        _clear_quota(1001)
    finally:
        args.admin = []
        cg_gen.generate = orig_cg_generate
        comfy_client.describe = orig_cg_describe
        cg_gen.stage_for = orig_cg_stage
        cg_gen.reset_for_test()


def run_tts(br, link, check, Msg, drain):
    """15/16*：/tts 指令、触发优先级与会话隔离。"""
    _rc = rc
    # 15/16 语音指令：/tts xxx 要借正常对话生成回复，并标记要发语音
    # TTS 服务在不在不该影响指令逻辑，且真发 HTTP 会让自测看本机状态变绿变红
    orig_health = tts_client.health
    tts_client.health = lambda url, timeout=4: {'ok': True}
    tts_msg = Msg(uid='1009', nick='阿澈', gid='123456',
                  text='%s 给我说句话' % cfg.TTS_CMD, mid='tts-1', at_self=True)
    ret = br.handle_command(tts_msg)
    check('15 /tts 标记 want_tts 且借正常对话',
          ret is False and tts_msg.want_tts is True
          and tts_msg.text == '给我说句话',
          (ret, tts_msg.want_tts, tts_msg.text))

    # 语气子参数是英文；中文只当正文（指令层不留中文别名）
    tts_msg2 = Msg(uid='1009', nick='阿澈', gid='123456',
                   text='%s battle 上啊' % cfg.TTS_CMD, mid='tts-2', at_self=True)
    br.handle_command(tts_msg2)
    check('16 /tts battle 可指定战斗语气且吃掉子参数',
          tts_msg2.tts_style == 'battle' and tts_msg2.text == '上啊',
          (tts_msg2.tts_style, tts_msg2.text))
    tts_msg3 = Msg(uid='1009', nick='阿澈', gid='123456',
                   text='%s 战斗开始了' % cfg.TTS_CMD, mid='tts-3', at_self=True)
    br.handle_command(tts_msg3)
    check('16b 「战斗」不再是指令关键字：整句当正文',
          tts_msg3.text == '战斗开始了', (tts_msg3.tts_style, tts_msg3.text))

    g_key = tts_state.conv_key(True, '123456', '1009')
    p_key = tts_state.conv_key(False, '', '1009')
    # 16c 只换语气：吃掉整条消息，不出声
    br.handle_command(Msg(uid='1009', nick='阿澈', gid='123456',
                          text='%s daily' % cfg.TTS_CMD,
                          mid='tts-style', at_self=True))
    check('16c /tts daily 只设语气，不常开也不出声',
          tts_state.get_style(g_key) == 'daily'
          and not tts_state.is_enabled(g_key), tts_state.load(g_key))

    # 16d 常开：/tts on 之后本会话每条回应都带语音，取会话语气
    ret_on = br.handle_command(Msg(uid='1009', nick='阿澈', gid='123456',
                                   text='%s on' % cfg.TTS_CMD,
                                   mid='tts-on-1', at_self=True))
    check('16d /tts on 常开本会话并吃掉指令',
          ret_on is True and tts_state.is_enabled(g_key)
          and tts_state.get_style(g_key) == 'daily', tts_state.load(g_key))
    m_always = Msg(uid='1009', nick='阿澈', gid='123456',
                   text='今天过得怎么样', mid='tts-always-1', at_self=True)
    br._apply_tts_triggers(m_always)
    check('16e 常开会话的普通回应带语音、取会话语气',
          m_always.want_tts is True and m_always.tts_style == 'daily'
          and m_always.text == '今天过得怎么样',
          (m_always.want_tts, m_always.tts_style, m_always.text))
    m_nat = Msg(uid='1009', nick='阿澈', gid='123456',
                text='用语音说说你今天怎么样', mid='tts-always-2',
                at_self=True)
    br._apply_tts_triggers(m_nat)
    check('16f 常开时不再剥自然语言触发词',
          m_nat.want_tts is True and m_nat.text == '用语音说说你今天怎么样',
          (m_nat.want_tts, m_nat.text))
    m_dup = Msg(uid='1009', nick='阿澈', gid='123456',
                text=cfg.TTS_CMD, mid='tts-always-3', at_self=True)
    br.handle_command(m_dup)
    check('16g 已常开时光发 /tts 只提示、不再排队',
          not tts_state.has_pending(tts_state.pending_key(g_key, '1009')),
          tts_state.has_pending(tts_state.pending_key(g_key, '1009')))

    # 16h 关掉：语气保留，下次 on 回到上次的设置
    br.handle_command(Msg(uid='1009', nick='阿澈', gid='123456',
                          text='%s battle' % cfg.TTS_CMD,
                          mid='tts-style2', at_self=True))
    br.handle_command(Msg(uid='1009', nick='阿澈', gid='123456',
                          text='%s off' % cfg.TTS_CMD,
                          mid='tts-off-1', at_self=True))
    m_after = Msg(uid='1009', nick='阿澈', gid='123456',
                  text='再说一句', mid='tts-always-4', at_self=True)
    br._apply_tts_triggers(m_after)
    check('16h /tts off 关掉常开但保留语气',
          not tts_state.is_enabled(g_key)
          and tts_state.get_style(g_key) == 'battle'
          and m_after.want_tts is False,
          (tts_state.load(g_key), m_after.want_tts))

    # 16i 一次性：光发 /tts → 只有排队的这个人下一条带语音
    br.handle_command(Msg(uid='1009', nick='阿澈', gid='123456',
                          text=cfg.TTS_CMD, mid='tts-arm', at_self=True))
    m_other = Msg(uid='1010', nick='别人', gid='123456',
                  text='我也说一句', mid='tts-pend-0', at_self=True)
    br._apply_tts_triggers(m_other)
    m_pend1 = Msg(uid='1009', nick='阿澈', gid='123456',
                  text='第一条', mid='tts-pend-1', at_self=True)
    br._apply_tts_triggers(m_pend1)
    m_pend2 = Msg(uid='1009', nick='阿澈', gid='123456',
                  text='第二条', mid='tts-pend-2', at_self=True)
    br._apply_tts_triggers(m_pend2)
    check('16i 光发 /tts 只让本人下一条带语音（用会话语气、不被别人接话吃掉）',
          m_other.want_tts is False
          and m_pend1.want_tts is True and m_pend1.tts_style == 'battle'
          and m_pend2.want_tts is False,
          (m_other.want_tts, m_pend1.want_tts, m_pend1.tts_style,
           m_pend2.want_tts))

    # 16j 会话隔离：群里的设置不带到私聊，反之亦然
    m_p = Msg(uid='1009', nick='阿澈', gid='', text='%s on' % cfg.TTS_CMD,
              mid='tts-on-2', is_group=False)
    br.handle_command(m_p)
    m_pd = Msg(uid='1009', nick='阿澈', gid='', text='随便聊聊',
               mid='tts-always-5', is_group=False)
    br._apply_tts_triggers(m_pd)
    m_gd = Msg(uid='1009', nick='阿澈', gid='123456', text='随便聊聊',
               mid='tts-always-6', at_self=True)
    br._apply_tts_triggers(m_gd)
    check('16j 私聊常开不泄漏到群（群也已有待用不串）',
          m_pd.want_tts is True and not tts_state.is_enabled(g_key)
          and m_gd.want_tts is False,
          (m_pd.want_tts, tts_state.load(g_key), m_gd.want_tts))

    # 16k 全局总开关关着：/tts 与 /tts on 给同一句指引（不互相指认）
    drain()   # 前面的 /tts 借对话流程发了异步回复，先清干净再数新增
    orig_tts_switch = _rc._DATA.get('tts_enabled')
    _rc._DATA['tts_enabled'] = False
    before = len(link.sent)
    br.handle_command(Msg(uid='1009', nick='阿澈', gid='654321',
                          text='%s on' % cfg.TTS_CMD, mid='tts-global-1',
                          at_self=True))
    br.handle_command(Msg(uid='1009', nick='阿澈', gid='654321',
                          text='%s 说句话' % cfg.TTS_CMD, mid='tts-global-2',
                          at_self=True))
    told = link.sent[before:]
    m_blk = Msg(uid='1009', nick='阿澈', gid='', text='随便聊聊',
                mid='tts-always-7', is_group=False)
    br._apply_tts_triggers(m_blk)
    if orig_tts_switch is None:
        _rc._DATA.pop('tts_enabled', None)
    else:
        _rc._DATA['tts_enabled'] = orig_tts_switch
    check('16k 总开关关闭时不常开、不出声，且指引一致',
          all('总开关' in (t[2] or '') for t in told if t[2])
          and len(told) == 2 and m_blk.want_tts is False
          and not tts_state.is_enabled(tts_state.conv_key(True, '654321', '1009')),
          (told, m_blk.want_tts))

    # 16l TTS 服务没起：/tts on 仍然开，但明确告知会刷失败提示
    tts_client.health = lambda url, timeout=4: None
    before = len(link.sent)
    br.handle_command(Msg(uid='1009', nick='阿澈', gid='777777',
                          text='%s on' % cfg.TTS_CMD, mid='tts-down-1',
                          at_self=True))
    told = link.sent[before:]
    tts_client.health = orig_health
    check('16l 服务离线时 /tts on 仍生效并附带提醒',
          tts_state.is_enabled(tts_state.conv_key(True, '777777', '1009'))
          and len(told) == 1 and '没起' in (told[0][2] or ''), told)
    tts_state.set_state(tts_state.conv_key(True, '777777', '1009'), False)
    tts_state.set_state(p_key, False)


def run_text(br, check):
    """17a/17b：台词清理与自然语言触发。"""
    # 17a TTS 只念台词：括号里的动作/旁白不能念出来（私聊展示保留动作）
    tts_cases = [
        ('（轻轻歪头）今天过得怎么样？', '今天过得怎么样？'),
        ('嗯（笑），那就好', '嗯，那就好'),
        ('（沉默片刻）(小声)我明白了', '我明白了'),
        ('（其实我也不确定）', ''),
    ]
    bad3 = [(src, strip_for_tts(src), want) for src, want in tts_cases
            if strip_for_tts(src) != want]
    check('17a TTS 剥掉括号里的动作/旁白', not bad3, bad3)

    # 17b 自然语言触发语音：群里大家是这么试的（「测测语音」「说句话」），
    #      只认 /tts 指令会让人以为功能坏了。但「在聊语音这件事」不该触发。
    nat_hit = ['测测语音', '发个语音听听', '说句话', '念一句', '用语音回我']
    nat_miss = ['刚才连发了两个/tts指令', '怎么还有语音了', '为什么还不发语音',
                '语音功能怎么打开', '语音指令怎么用']
    bad = [t for t in nat_hit if not br.media.tts.natural_trigger(t)]
    bad2 = [t for t in nat_miss if br.media.tts.natural_trigger(t)]
    check('17b 自然语言能触发语音',
          not bad and not bad2,
          ('漏触发=%s 误触发=%s' % (bad, bad2)))

