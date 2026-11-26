# -*- coding: utf-8 -*-
"""媒体端口：TTS / CG 这些支线能力的唯一入口。

分工（依赖只能向下）：

    L1 桥接   qq_bridge / bridge_commands / bridge_transport
              只调 media.apply_tts_triggers() / speak() / cg_command()，
              不 import cg_gen / tts_client / tts_state 等具体媒体实现。
    L2 支线   media_tts / media_cg
              自己拿 persona / runtime_config / 后端客户端，把结果包成 MediaResult。
    L0 核心   rp_core / memory_* / persona —— 不知道媒体存在。

失败一律降级：MediaResult.ok=False，不往对话流程抛异常。
`NAL_MEDIA=null` 时整体换成 NullMedia，自测/离线可断言"没有媒体也照常回文字"。
"""
import logging
import os

LOG = logging.getLogger('media')


class MediaResult(object):
    """一次媒体操作的结果。path 是 wav / png 的本地路径，text 是给用户的话。"""
    __slots__ = ('ok', 'kind', 'path', 'text', 'reason', 'meta')

    def __init__(self, ok, kind='', path=None, text='', reason='', meta=None):
        self.ok = bool(ok)
        self.kind = kind
        self.path = path
        self.text = text
        self.reason = reason
        self.meta = meta or {}

    def __repr__(self):
        return 'MediaResult(ok=%s, kind=%r, path=%r, reason=%r)' % (
            self.ok, self.kind, self.path, self.reason)


class MediaHost(object):
    """桥接交给媒体的最小能力：发消息、管理员名单、TTS 地址。

    媒体模块不认识 Bridge，只认这三个回调；换传输层（面板 / CLI）时不用改适配器。
    """
    def __init__(self, send, admins=None, tts_url=None):
        self._send = send
        self._admins = admins or (lambda: [])
        self._tts_url = tts_url or (lambda: '')

    def send(self, msg, text, image=None):
        return self._send(msg, text, image=image)

    @property
    def admins(self):
        return [str(a) for a in (self._admins() or [])]

    def tts_url(self):
        return self._tts_url() or ''

    def __repr__(self):
        return 'MediaHost()'


class Media(object):
    """组合 TTS / CG 两个适配器；桥接只跟这一层打交道。"""

    def __init__(self, host):
        from media_tts import TtsAdapter
        from media_cg import CgAdapter
        self.host = host
        self.tts = TtsAdapter(host)
        self.cg = CgAdapter(host)

    # ---- TTS ----
    def apply_tts_triggers(self, msg):
        return self.tts.apply_triggers(msg)

    def handle_tts_command(self, msg, raw):
        return self.tts.handle_command(msg, raw)

    def speak(self, msg, text):
        return self.tts.speak(msg, text)

    def tts_label(self, msg):
        return self.tts.label(msg)

    def tts_diag_lines(self):
        return self.tts.diag_lines()

    def clear_pending_voice(self):
        return self.tts.clear_pending_all()

    # ---- CG ----
    def cg_command(self, msg, args=None):
        return self.cg.command(msg, args)

    def cg_help_text(self):
        return self.cg.help_text()


class NullMedia(object):
    """没有媒体：所有操作降级，指令仍给一句明确回复。"""

    host = None

    def apply_tts_triggers(self, msg):
        return None

    def handle_tts_command(self, msg, raw):
        return True

    def speak(self, msg, text):
        return MediaResult(False, 'record', reason='media disabled')

    def tts_label(self, msg):
        return '关'

    def tts_diag_lines(self):
        return ['TTS：未启用']

    def clear_pending_voice(self):
        return None

    def cg_command(self, msg, args=None):
        return '媒体功能没有启用。'

    def cg_help_text(self):
        return '/cg 画一张新 CG（当前未启用）'


def build(host=None):
    """按环境变量选择实现。默认 tts+cg；`NAL_MEDIA=null` 全部降级。"""
    mode = (os.environ.get('NAL_MEDIA') or '').strip().lower()
    if mode in ('null', 'off', 'none', '0'):
        LOG.info('媒体已关闭（NAL_MEDIA=%s）', mode)
        return NullMedia()
    if host is None:
        raise ValueError('build() 需要 MediaHost')
    return Media(host)
