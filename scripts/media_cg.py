# -*- coding: utf-8 -*-
"""CG 支线：/cg 指令、配额 claim、后台生成与回图。

从 bridge_commands 收拢过来。生成本身仍在 cg_gen（提示词 / ComfyUI 后端），
这里只负责「要不要画、画完怎么回」；发消息通过 MediaHost 回调。
"""
import logging
import threading

import cg_gen
import persona
import runtime_config as rc

LOG = logging.getLogger('media.cg')


class CgAdapter(object):
    kind = 'cg'

    def __init__(self, host):
        self.host = host

    def help_text(self):
        return ('/cg 画一张新 CG（/cg new 强制新图，'
                '/cg 内容 加自定义要求；后两种仅管理员）')

    def command(self, msg, args=None):
        """处理一条 /cg，返回要立刻发出去的回执（生成在后台线程里跑）。"""
        args = [a for a in (args or []) if a.strip()]
        sub = (args[0].strip().lower() if args else '')
        if not persona.capabilities().get('cg'):
            return '这个角色没有配置 CG 能力。'
        if not rc.get('cg_enabled', True):
            return 'CG 生成现在是关着的。'

        force = False
        extra = ''
        if sub:
            admins = self.host.admins
            if not admins:
                return '自定义内容需要先在配置里设管理员。'
            if str(msg.uid) not in admins:
                if sub == 'new':
                    return '强制新图只有管理员能用；发 /cg 就能画。'
                return '自定义内容只有管理员能用；发 /cg 就能画。'
            force = True
            extra = ' '.join(args[1:] if sub == 'new' else args).strip()
            if len(extra) > cg_gen.EXTRA_MAX:
                return ('内容太长了，最多 %d 字；简化一下再发。'
                        % cg_gen.EXTRA_MAX)

        pid = persona.current()
        try:
            ok, why, meta = cg_gen.claim(msg.uid, force=force,
                                         card=persona.card_json_path(pid),
                                         pid=pid)
        except Exception:
            LOG.exception('CG 额度申请异常')
            return '这次没画成，稍后再试。'
        if not ok:
            return why
        stage = meta.get('stage') or cg_gen.DEFAULT_STAGE
        try:
            threading.Thread(target=self._worker,
                             args=(msg, stage, extra, pid),
                             name='cg-%s' % msg.uid, daemon=True).start()
        except Exception:
            cg_gen.release()
            LOG.exception('CG 生成线程启动失败')
            return '这次没画成，稍后再试。'
        anchor = cg_gen.anchor_for(msg.uid)
        bits = ['ComfyUI', stage]
        if extra:
            bits.append('自定义要求')
        elif force:
            bits.append('强制新图')
        if anchor and anchor.get('scene'):
            bits.append(anchor['scene'])
        return '好，等我画一会儿……（%s）' % '·'.join(bits)

    def _worker(self, msg, stage, extra='', pid=None):
        """后台生成并回图。失败只回一句原因，不重试、不占用对话队列。"""
        pid = pid or persona.active_id()
        try:
            with persona.context(pid):
                try:
                    res = cg_gen.generate(
                        msg.uid, stage=stage, extra=extra,
                        card=persona.card_json_path(pid), pid=pid)
                except Exception as e:
                    LOG.exception('CG 生成异常')
                    res = {'ok': False, 'error': str(e)}
        finally:
            # claim() 设置的忙碌位必须由这里兜底清掉：generate 若连本地锁都没拿到
            # 会提前返回，自己不会 release。
            cg_gen.release()
        if persona.active_id() != pid:
            LOG.info('角色已切换，丢弃本次 CG：%s', msg.uid)
            return
        if not res.get('ok'):
            LOG.warning('CG 生成失败（%s）：%s', msg.uid, res.get('error'))
            self.host.send(msg, '这次没画成……（%s）'
                           % cg_gen.short_error(res.get('error')))
            return
        LOG.info('CG 已生成（%s）：%s', msg.uid, res.get('path'))
        self.host.send(msg, '画好了。（ComfyUI · %s）' % (res.get('stage') or stage),
                       image=res.get('path'))
