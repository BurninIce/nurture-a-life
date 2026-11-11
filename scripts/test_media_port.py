# -*- coding: utf-8 -*-
"""媒体端口离线测试：TTS / CG 适配器、NullMedia 降级、命令与失败语义。

不联网、不写真实 state：
- tts_state / cg_gen 全部走替身或临时目录
- persona 能力位与卡片路径用替身，不钉真实角色卡
"""
import contextlib
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cg_gen  # noqa: E402
import media_port  # noqa: E402
import media_tts  # noqa: E402
import persona  # noqa: E402
import rp_config as cfg  # noqa: E402
import runtime_config as rc  # noqa: E402
import tts_client  # noqa: E402
import tts_state  # noqa: E402

RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok), detail))
    if not ok:
        print('[FAIL] %s  → %s' % (name, str(detail)[:220]))
    return bool(ok)


class Msg(object):
    def __init__(self, uid='1009', text='', gid='123456', is_group=True):
        self.uid = uid
        self.text = text
        self.gid = gid
        self.is_group = is_group
        self.want_tts = False
        self.tts_style = ''


class Host(object):
    """收集媒体发出去的消息。"""

    def __init__(self, admins=None):
        self.sent = []
        self.admins = list(admins or [])

    def send(self, msg, text, image=None):
        self.sent.append((str(msg.uid), text, image))
        return True

    def media_host(self):
        return media_port.MediaHost(
            send=self.send,
            admins=lambda: self.admins,
            tts_url=lambda: 'http://127.0.0.1:6790')


def _wait_image(host, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for row in host.sent:
            if row[2]:
                return row
        time.sleep(0.01)
    return None


def main():
    tmp = tempfile.mkdtemp(prefix='media_test_')
    tts_state.set_root(os.path.join(tmp, 'tts'))

    orig = {
        'caps': persona.capabilities,
        'current': persona.current,
        'active': persona.active_id,
        'context': persona.context,
        'card': persona.card_json_path,
        'rc_get': rc.get,
        'synth': tts_client.synth,
        'health': tts_client.health,
        'claim': cg_gen.claim,
        'gen': cg_gen.generate,
        'anchor': cg_gen.anchor_for,
        'rc_set': rc.set_value,
    }
    caps = {'tts': True, 'cg': True}
    calls = {'synth': [], 'gen': []}

    persona.capabilities = lambda: dict(caps)
    persona.current = lambda: 'kurumi'
    persona.active_id = lambda: 'kurumi'
    persona.context = lambda pid=None: contextlib.nullcontext()
    persona.card_json_path = lambda pid=None: os.path.join(tmp, 'card.json')
    rc.get = (lambda key, default=None: {
        'tts_enabled': True, 'tts_natural': True, 'tts_default_style': 'daily',
        'cg_enabled': True,
    }.get(key, default))
    tts_client.health = lambda url, timeout=4: {'translate': 'off'}
    cg_gen.anchor_for = lambda uid: {}

    def fake_synth(text, style='daily', url='', timeout=75):
        calls['synth'].append({'text': text, 'style': style, 'url': url})
        return os.path.join(tmp, 'a.wav'), 1.5

    def fake_generate(uid, stage=None, **kw):
        calls['gen'].append({'uid': str(uid), 'stage': stage})
        return {'ok': True, 'path': os.path.join(tmp, 'a.png'),
                'stage': stage or '相识'}

    tts_client.synth = fake_synth
    cg_gen.generate = fake_generate

    host = Host(['1001'])
    media = media_port.build(host.media_host())

    try:
        r = media_port.MediaResult(True)
        check('1 MediaResult 默认字段', r.ok and r.kind == '' and r.path is None
              and r.meta == {}, r)

        null = media_port.NullMedia()
        check('2 NullMedia 全部降级',
              null.speak(Msg(), 'x').ok is False
              and null.tts_label(Msg()) == '关'
              and null.cg_command(Msg(), []) == '媒体功能没有启用。'
              and null.tts_diag_lines() == ['TTS：未启用'],
              (null.tts_label(Msg()), null.cg_command(Msg())))

        os.environ['NAL_MEDIA'] = 'null'
        try:
            check('3 NAL_MEDIA=null → NullMedia',
                  isinstance(media_port.build(host.media_host()),
                             media_port.NullMedia))
        finally:
            os.environ.pop('NAL_MEDIA', None)

        check('4 build → TTS/CG 适配器',
              isinstance(media, media_port.Media)
              and media.tts.kind == 'tts' and media.cg.kind == 'cg')

        h = Host([1001, '1002'])
        mh = h.media_host()
        check('5 管理员名单统一成字符串', mh.admins == ['1001', '1002'], mh.admins)

        tts = media.tts
        check('6 自然语言触发判定',
              tts.natural_trigger('测测语音')
              and tts.natural_trigger('用语音回我')
              and not tts.natural_trigger('怎么还有语音了')
              and not tts.natural_trigger('语音指令怎么用'))

        check('7 触发词剥离',
              tts.strip_trigger('用语音说说你今天怎么样') == '说说你今天怎么样'
              and tts.strip_trigger('测测语音') == '')

        key = tts_state.conv_key(True, '123456', '1009')
        tts_state.clear_pending_all()
        tts_state.set_state(key, True, 'battle')
        m = Msg(text='随便聊聊')
        tts.apply_triggers(m)
        check('8 会话常开 → 标记语音并取会话语气',
              m.want_tts is True and m.tts_style == 'battle'
              and m.text == '随便聊聊', (m.want_tts, m.tts_style))
        tts_state.set_state(key, False)

        rc.get = (lambda k, d=None: False if k == 'tts_enabled' else d)
        m2 = Msg(text='测测语音')
        tts.apply_triggers(m2)
        check('9 全局总开关关闭 → 不触发', m2.want_tts is False)
        rc.get = (lambda k, d=None: {
            'tts_enabled': True, 'tts_natural': True,
            'tts_default_style': 'daily', 'cg_enabled': True}.get(k, d))

        tts_state.arm_pending(tts_state.pending_key(key, '1009'))
        m_other = Msg(uid='1010', text='我也说一句')
        m_me = Msg(uid='1009', text='第一条')
        tts.apply_triggers(m_other)
        tts.apply_triggers(m_me)
        check('10 一次性排队只给本人下一条',
              m_other.want_tts is False and m_me.want_tts is True,
              (m_other.want_tts, m_me.want_tts))

        m_speak = Msg(text='x')
        m_speak.tts_style = 'battle'
        res = tts.speak(m_speak, '（轻轻歪头）今天过得怎么样？\n嗯')
        check('11 合成前剥动作、换行转逗号',
              res.ok and res.path and res.meta.get('duration') == 1.5
              and calls['synth'][-1]['text'] == '今天过得怎么样？，嗯'
              and calls['synth'][-1]['style'] == 'battle'
              and calls['synth'][-1]['url'] == 'http://127.0.0.1:6790',
              (res, calls['synth'][-1]))

        tts_client.synth = lambda text, style='daily', url='', timeout=75: None
        res = tts.speak(Msg(), '今天也不错')
        check('12 合成失败 → 降级为一句提示',
              res.ok is False and res.text == cfg.TTS_FAIL_FORMAT
              and not res.meta.get('silent'), res)
        tts_client.synth = fake_synth

        res = tts.speak(Msg(), '（其实我也不确定）')
        check('13 剥完没台词 → 静默跳过',
              res.ok is False and res.text == '' and res.meta.get('silent'),
              res)

        host.sent[:] = []
        tts_state.set_state(key, False)
        handled = tts.handle_command(Msg(text=cfg.TTS_CMD + ' on'),
                                     cfg.TTS_CMD + ' on')
        check('14 /tts on → 常开并回执',
              handled is True and tts_state.is_enabled(key)
              and host.sent and '带一条语音' in host.sent[-1][1],
              (handled, host.sent[-1:]))

        m_cmd = Msg(text=cfg.TTS_CMD + ' battle 上啊')
        cont = tts.handle_command(m_cmd, m_cmd.text)
        check('15 /tts battle 正文 → 借对话流程',
              cont is False and m_cmd.text == '上啊'
              and m_cmd.tts_style == 'battle' and m_cmd.want_tts,
              (cont, m_cmd.text, m_cmd.tts_style))

        caps['tts'] = False
        host.sent[:] = []
        handled = tts.handle_command(Msg(text=cfg.TTS_CMD), cfg.TTS_CMD)
        check('16 角色没配语音 → 直接说明',
              handled is True and '没有配置语音' in host.sent[-1][1],
              host.sent[-1:])
        caps['tts'] = True

        rc.get = (lambda k, d=None: False if k == 'tts_enabled' else d)
        host.sent[:] = []
        tts.handle_command(Msg(text=cfg.TTS_CMD + ' on'), cfg.TTS_CMD + ' on')
        check('17 总开关关闭 → /tts 只指认总开关',
              host.sent and '总开关' in host.sent[-1][1], host.sent[-1:])
        rc.get = (lambda k, d=None: {
            'tts_enabled': True, 'tts_natural': True,
            'tts_default_style': 'daily', 'cg_enabled': True}.get(k, d))

        tts_state.set_state(key, True, 'daily')
        check('18 /status 语音档位',
              tts.label(Msg()) == '常开（语气 日常）', tts.label(Msg()))
        tts_state.set_state(key, False)
        check('19 诊断行含在线与常开计数',
              any('在线' in x for x in tts.diag_lines())
              and any('语音常开' in x for x in tts.diag_lines()),
              tts.diag_lines())

        cg = media.cg
        caps['cg'] = False
        check('20 角色没配 CG → 直接说明',
              '没有配置' in cg.command(Msg(), []))
        caps['cg'] = True

        check('23 /cg new → 非管理员被拒',
              '管理员' in cg.command(Msg(uid='1009'), ['new']))
        check('24 /cg 自定义内容超长 → 拒绝',
              '太长' in cg.command(Msg(uid='1001'), ['长' * (cg_gen.EXTRA_MAX + 1)]))
        caps['cg'] = False
        check('25 角色没配 CG 时切换也不放行',
              '没有配置' in cg.command(Msg(uid='1001'), ['cloud']))
        caps['cg'] = True

        cg_gen.claim = lambda uid, force=False, card=None, pid=None: (
            False, '今天已经画了 5 张，明天再画吧。', {})
        check('26 额度耗尽 → 原样回原因',
              '明天再画' in cg.command(Msg(uid='1009'), []))
        cg_gen.claim = lambda *a, **k: (_ for _ in ()).throw(OSError('disk'))
        check('27 额度申请异常 → 不抛出',
              '没画成' in cg.command(Msg(uid='1009'), []))
        cg_gen.claim = lambda uid, force=False, card=None, pid=None: (
            True, '', {'stage': '相识'})

        host.sent[:] = []
        calls['gen'][:] = []
        receipt = cg.command(Msg(uid='1009'), [])
        img = _wait_image(host)
        check('28 生成成功 → 回执 + 后台回图',
              '等我画' in receipt and img and img[2].endswith('.png')
              and calls['gen'] and calls['gen'][-1]['uid'] == '1009'
              and any('画好了' in row[1] for row in host.sent),
              (receipt, host.sent[-2:]))

        def boom(uid, stage=None, **kw):
            raise RuntimeError('boom')
        cg_gen.generate = boom
        host.sent[:] = []
        cg.command(Msg(uid='1009'), [])
        deadline = time.time() + 2
        while time.time() < deadline and not any(
                '没画成' in row[1] for row in host.sent):
            time.sleep(0.01)
        check('29 生成异常 → 只回一句原因',
              any('没画成' in row[1] for row in host.sent), host.sent[-2:])
        cg_gen.generate = fake_generate
    finally:
        persona.capabilities = orig['caps']
        persona.current = orig['current']
        persona.active_id = orig['active']
        persona.context = orig['context']
        persona.card_json_path = orig['card']
        rc.get = orig['rc_get']
        rc.set_value = orig['rc_set']
        tts_client.synth = orig['synth']
        tts_client.health = orig['health']
        cg_gen.claim = orig['claim']
        cg_gen.generate = orig['gen']
        cg_gen.anchor_for = orig['anchor']
        tts_state.set_root(None)

    failed = [r for r in RESULTS if not r[1]]
    print('=' * 60)
    print('媒体端口：%d/%d 通过' % (len(RESULTS) - len(failed), len(RESULTS)))
    for name, _, detail in failed:
        print('  失败：%s  %s' % (name, str(detail)[:180]))
    print('=' * 60)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
