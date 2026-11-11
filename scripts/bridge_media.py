# -*- coding: utf-8 -*-
"""桥接侧的媒体接线：只调用媒体端口，不含任何 TTS/CG 实现。

Bridge 通过 MediaMixin 继承；`self.media` 由 media_port.build() 组装。
媒体失败只降级——文字照常发，最多少一条语音/一张图。
"""
import logging

import media_port

LOG = logging.getLogger('qq_bridge')


class MediaMixin(object):
    """把桥接能力（发消息、管理员、TTS 地址）交给媒体端口。"""

    def _init_media(self):
        host = media_port.MediaHost(
            send=self._send,
            admins=lambda: getattr(self.args, 'admin', None) or [],
            tts_url=lambda: getattr(self.args, 'tts_url', ''),
        )
        self.media = media_port.build(host)
        return self.media

    def _apply_tts_triggers(self, msg):
        return self.media.apply_tts_triggers(msg)

    def _send_tts(self, msg, text):
        """文字发完后补一条语音；合成失败就回一句失败提示。"""
        res = self.media.speak(msg, text)
        if res.ok:
            LOG.info('语音已合成 %.2fs → %s',
                     (res.meta or {}).get('duration', 0), res.path)
            self._send_record(msg, res.path)
            return True
        if res.text:
            self._send(msg, res.text)
        return False
