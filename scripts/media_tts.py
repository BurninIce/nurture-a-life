# -*- coding: utf-8 -*-
"""TTS 支线：触发判定、/tts 指令、台词清理、合成与诊断。

从 qq_bridge / bridge_commands / bridge_transport 收拢过来。桥接侧只调
`media.apply_tts_triggers()` / `handle_tts_command()` / `speak()`；
发消息通过 MediaHost 回调，适配器不认识 OneBot。
"""
import logging
import re

import persona
import rp_config as cfg
import runtime_config as rc
import tts_client
import tts_state
from media_port import MediaResult
from rp_common import strip_for_tts

LOG = logging.getLogger('media.tts')


def _failed(reason, text='', silent=False):
    return MediaResult(False, 'record', text=text, reason=reason,
                       meta={'silent': True} if silent else {})


class TtsAdapter(object):
    kind = 'tts'

    def __init__(self, host):
        self.host = host

    # ---- 地址与健康 ----
    def url(self):
        return (self.host.tts_url() or rc.get('tts_url', '')
                or cfg.TTS_URL_DEFAULT)

    def health(self, timeout=4):
        try:
            return tts_client.health(self.url(), timeout=timeout)
        except Exception:
            LOG.debug('TTS 健康检查失败', exc_info=True)
            return None

    def alive(self, timeout=2.5):
        try:
            return bool(tts_client.health(self.url(), timeout=timeout))
        except Exception:
            LOG.debug('TTS 健康检查失败', exc_info=True)
            return False

    def default_style(self):
        return rc.get('tts_default_style', 'daily')

    def clear_pending_all(self):
        return tts_state.clear_pending_all()

    # ---- 入站：这条回复要不要带语音 ----
    def apply_triggers(self, msg):
        """三个来源，优先级从高到低：常开 → 一次性排队 → 自然语言。

        `/tts` 指令自己标记过的消息不走这里；常开/排队命中时不剥触发词，
        否则「用语音说说你今天怎么样」会被剥成「说说你今天怎么样」。
        """
        if not rc.get('tts_enabled', True):
            return
        key = tts_state.conv_key(msg.is_group, msg.gid, msg.uid)
        if tts_state.is_enabled(key):
            msg.want_tts = True
            msg.tts_style = tts_state.get_style(key)
            return
        if tts_state.take_pending(tts_state.pending_key(key, msg.uid)):
            msg.want_tts = True
            msg.tts_style = tts_state.get_style(key)
            return
        if rc.get('tts_natural', True) and self.natural_trigger(msg.text):
            msg.want_tts = True
            msg.tts_style = ''
            orig = msg.text
            stripped = self.strip_trigger(orig)
            msg.text = stripped or cfg.TTS_NATURAL_FALLBACK
            LOG.info('自然语言触发语音：%r → %r', orig, msg.text)

    @staticmethod
    def natural_trigger(text):
        """自然语言里是不是在「要一条语音」。"""
        t = (text or '').strip()
        if not t:
            return False
        for neg in cfg.TTS_NATURAL_NEGATIVE:
            if neg in t:
                return False
        for kw in cfg.TTS_NATURAL_KEYWORDS:
            if kw in t:
                return True
        return False

    @staticmethod
    def strip_trigger(text):
        """把触发词剥掉，剩下的话作为「要她说的内容」。"""
        t = (text or '').strip()
        for kw in cfg.TTS_NATURAL_KEYWORDS:
            if kw in t:
                t = t.replace(kw, ' ')
        t = re.sub(r'^[，,。.、！!？?：:\s]+', '', t)
        t = re.sub(r'[，,。.、！!？?：:\s]+$', '', t)
        t = t.strip()
        if not t or len(t) < 3:
            return ''
        return t

    # ---- /tts 指令 ----
    def handle_command(self, msg, raw):
        """返回 True = 指令处理完；False = 借正常对话流程生成回复。"""
        rest = raw[len(cfg.TTS_CMD):].strip()
        key = tts_state.conv_key(msg.is_group, msg.gid, msg.uid)
        where = '这个群' if msg.is_group else '我们的私聊'
        if not persona.capabilities().get('tts'):
            self.host.send(msg, '这个角色没有配置语音。')
            return True
        # 全局总开关关着时只指认这一层，别再让用户去查会话级开关。
        if not rc.get('tts_enabled', True):
            self.host.send(msg, '（语音总开关现在在面板里关着呢……打开后我才能出声）')
            return True

        tokens = rest.split(None, 1)
        sub = tokens[0].lower() if tokens else ''
        tail = tokens[1].strip() if len(tokens) > 1 else ''
        style = tts_state.get_style(key) or self.default_style()

        if sub in (cfg.TTS_SUB_ON, cfg.TTS_SUB_OFF):
            self.host.send(msg, self._always_command(
                key, where, sub == cfg.TTS_SUB_ON))
            return True

        if sub in cfg.TTS_STYLES:
            tts_state.set_state(key, tts_state.is_enabled(key), sub)
            style = sub
            if not tail:
                self.host.send(msg, '好，%s的语音换成「%s」语气。'
                               % (where, cfg.TTS_STYLE_LABELS[sub]))
                return True
            content = tail
        elif not rest:
            if tts_state.is_enabled(key):
                self.host.send(msg, '%s已经常开了，不用单独点。' % where)
            else:
                tts_state.arm_pending(tts_state.pending_key(key, msg.uid))
                self.host.send(msg, '好，接下来我说给你的那条带语音。')
            return True
        else:
            content = rest

        msg.text = content
        msg.want_tts = True
        msg.tts_style = style
        LOG.info('语音指令：style=%s text=%s', style, content[:30])
        return False

    def _always_command(self, key, where, on):
        """`/tts on|off`：本会话常开/关闭，语气设置保留。"""
        tts_state.clear_pending_in_conv(key)
        if not on:
            changed = tts_state.set_state(key, False)
            return ('好，%s以后就不带语音了。' % where if changed
                    else '%s本来就是关着的。' % where)
        tts_state.set_state(key, True)
        style = tts_state.get_style(key) or self.default_style()
        label = cfg.TTS_STYLE_LABELS.get(style, '日常')
        notice = ''
        if not self.alive():
            notice = ('（不过 TTS 服务现在没起：起来之前每条回复只会跟一句'
                      '失败提示，想先停就发 %s %s）'
                      % (cfg.TTS_CMD, cfg.TTS_SUB_OFF))
        return ('好，%s我说的话都带一条语音（语气：%s）。想停发 %s %s。%s'
                % (where, label, cfg.TTS_CMD, cfg.TTS_SUB_OFF, notice))

    # ---- 合成 ----
    def speak(self, msg, text):
        """把回复台词合成语音。成功返回 wav 路径，失败返回要发的话。"""
        text = strip_for_tts((text or '').strip())
        # 换行在语音里是硬切句，换成逗号更自然；剥掉括注后可能留下连逗号。
        text = re.sub(r'\s*\n+\s*', '，', text)
        text = re.sub(r'[，,]{2,}', '，', text).strip('，, \t')
        if not text:
            LOG.info('语音跳过：剥掉动作括注后没有台词')
            return _failed('empty text', silent=True)
        if not rc.get('tts_enabled', True):
            return _failed('disabled', silent=True)
        style = (getattr(msg, 'tts_style', '') or self.default_style())
        try:
            res = tts_client.synth(text, style=style, url=self.url())
        except Exception:
            LOG.exception('TTS 合成异常')
            res = None
        if not res:
            return _failed('synth failed', text=cfg.TTS_FAIL_FORMAT)
        path, dur = res
        return MediaResult(True, 'record', path=path, meta={'duration': dur})

    # ---- 展示 ----
    def label(self, msg):
        """`/status` 里的语音档位。"""
        key = tts_state.conv_key(msg.is_group, msg.gid, msg.uid)
        if not persona.capabilities().get('tts'):
            return '未配置'
        if tts_state.is_enabled(key):
            style = (tts_state.get_style(key) or self.default_style())
            return '常开（语气 %s）' % cfg.TTS_STYLE_LABELS.get(style, '日常')
        if tts_state.has_pending(tts_state.pending_key(key, msg.uid)):
            return '下一条带语音'
        return '关'

    def diag_lines(self):
        url = self.url()
        try:
            h = tts_client.health(url, timeout=4)
            if h:
                lines = ['TTS：✅ 在线（翻译 %s）' % (h.get('translate') or '无')]
            else:
                lines = ['TTS：❌ 不在（%s）' % url]
        except Exception as e:
            lines = ['TTS：❌ %s' % e]
        lines.append('语音常开：%d 个会话（/tts on、/tts off 逐群逐人切换）'
                     % tts_state.enabled_count())
        return lines
