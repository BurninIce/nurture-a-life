# -*- coding: utf-8 -*-
"""QQ 桥接的指令层：/relation /remember /forget /status /topic /group /cg /tts /diag。

从 qq_bridge.py 拆出。Bridge 通过 CommandMixin 继承；对话主流程仍留在
qq_bridge（决策、队列、生成、记忆后处理），这里只处理"以 / 开头的指令"。
"""
import datetime
import logging
import threading
import time

import group_activity as activity
import group_gate as gate
import group_settings
import hw_profile as hw
import memory_card as mem
import persona
import private_topic
import rp_config as cfg
import rp_core as core
import runtime_config as rc
from bridge_message import split_reply

LOG = logging.getLogger('qq_bridge')


class CommandMixin(object):
    """Bridge 的指令侧方法。"""

    def handle_command(self, msg, pid=None):
        """处理指令。

        返回 True = 已处理完（消息到此为止）；
        返回 False = 交给正常对话流程继续（语音指令要借对话流程生成回复）。

        角色在入口处固定（pid 可显式指定，默认取当前指针）：指令执行期间即使
        另一条连接切换了角色，本次指令仍按进入时的角色读写记忆，不会一半旧一半新。
        """
        pid = pid or persona.active_id()
        with persona.context(pid):
            return self._dispatch_command(msg)

    def _dispatch_command(self, msg):
        raw = msg.text.strip()
        parts = raw.split()
        cmd = parts[0].lower() if parts else ''
        uid = msg.uid
        reply = ''

        # 私聊里的指令也算「人有动静」：清 pending、重排下一次候选。
        # 只记白名单对象，陌生人的指令不进入主动调度。
        if not msg.is_group:
            allow = [str(a) for a in (rc.get('private_allow', []) or [])]
            if not allow or str(uid) in allow:
                try:
                    private_topic.note_incoming(uid, msg.ts)
                except Exception:
                    LOG.exception('私聊主动状态更新失败（指令）')

        if cmd == cfg.TTS_CMD:
            return self.media.handle_tts_command(msg, raw)

        # `/ <内容>`：只给作者看的旁注（OOC）。
        # 内容不进入对话上下文、记忆、门控和模型，也不回复——后台数值这类话
        # 一旦被模型读到，她就会意识到自己只是模型。只记长度，不落内容。
        if cmd == '/':
            LOG.info('OOC 旁注已丢弃（%d 字）', max(0, len(raw) - 1))
            return True

        if cmd == '/relation':
            reply = core.mem_label(uid) or '还没记下什么。'
        elif cmd == '/remember':
            reply = self._remembered(uid)
        elif cmd == '/forget':
            archived = core.clear_user(uid)
            reply = '你的事，我已经忘了。' + ('（旧记忆已备份）' if archived else '')
        elif cmd == '/status':
            if self.args.admin and str(uid) not in self.args.admin:
                reply = '这个不给你看。'
            else:
                reply = self._status(msg)
        elif cmd == '/topic':
            allow = [str(a) for a in (rc.get('private_allow', []) or [])]
            if self.args.admin and str(uid) not in self.args.admin:
                reply = '这个不给你看。'
            elif msg.is_group:
                reply = '这个话题只在私聊里试。'
            elif not allow or str(uid) not in allow:
                reply = '私聊主动只对白名单里的人开放。'
            elif not private_topic.is_enabled():
                reply = '私聊主动现在是关着的。'
            elif self._trigger_private_proactive(uid, time.time(), manual=True):
                reply = ''
            else:
                reply = '这次没找到合适的开口，晚点再想。'
        elif cmd == '/cg':
            reply = self.media.cg_command(msg, parts[1:])
        elif cmd == '/group':
            reply = self._group_switch(msg, parts[1:])
        elif cmd == '/character':
            reply = self._character_switch(msg, parts[1:])
        elif cmd == '/help':
            reply = self._help_text()
        elif cmd in ('/diag', '/ping'):
            reply = self._diag()
        else:
            return False   # 不是指令，交给正常对话（避免误吞）

        for chunk in split_reply(reply):
            self._send(msg, chunk)
        return True

    def _help_text(self):
        """只列当前角色真正配置了的能力，避免把用户引到无效指令上。"""
        caps = persona.capabilities()
        parts = ['/relation 她与你的关系', '/remember 她记得你的事',
                 '/forget 清空她关于你的记忆', '/topic 让她现在主动找个话题',
                 '/group 本群开关与参数（/group status · set <key> <value> · unset all · copy <gid>，仅管理员）',
                 '/character 切换角色（/character 角色id，仅管理员）']
        if caps.get('cg'):
            parts.append(self.media.cg_help_text())
        if caps.get('tts'):
            parts.append(cfg.TTS_HELP)
        parts.append('/diag 排查链路')
        parts.append('`/ 内容` 只有你自己看，她不会读到也不会回')
        return ' · '.join(parts)

    # ---- 群聊开关 ----
    def _group_switch(self, msg, args=None):
        """本群接管开关与参数：/group [on|off|status|settings|set|unset|copy|list]。

        关闭后本群所有消息都不再处理，但管理员仍可用 /group on 重新打开。
        全局总开关（UI 的「群聊」）优先级更高，关闭时连这里也不会被执行。
        """
        if not msg.is_group:
            return '这个指令只在群里用。'
        admin = [str(a) for a in (getattr(self.args, 'admin', None) or [])]
        if not admin:
            return '群聊开关需要先配置管理员（--admin 或 QQ_ADMIN）。'
        if str(msg.uid) not in admin:
            return '群聊开关只有管理员能改。'
        gid = str(msg.gid)
        parts = list(args or [])
        sub = (parts[0].strip().lower() if parts else '')
        off = [str(g) for g in (rc.get('group_disabled') or [])]
        usage = ('用法：/group on | off | status | settings | '
                 'set <key> <value> | unset <key>|all | copy <gid> | list')
        key_help = '；可设参数：%s' % ', '.join(
            s['key'] for s in group_settings.schema())
        if sub in ('', 'status'):
            on = group_settings.enabled(gid, legacy_off=off)
            n = len(group_settings.overrides(gid))
            return ('本群的群聊现在是%s的；单独覆盖 %d 项。%s'
                    % ('开着' if on else '关着', n,
                       '' if on else '发 /group on 打开。'))
        if sub == 'off':
            if not group_settings.enabled(gid, legacy_off=off):
                return '本群的群聊已经是关着的。'
            group_settings.set_enabled(gid, False)
            return '好，本群我先不接话了；发 /group on 可以叫我回来。'
        if sub == 'on':
            if group_settings.enabled(gid, legacy_off=off):
                return '本群的群聊本来就是开着的。'
            group_settings.set_enabled(gid, True)
            return '好，本群我回来了。'
        if sub == 'settings':
            ov = group_settings.overrides(gid)
            if not ov:
                return '本群没有单独覆盖，全部跟随全局。可用 /group set <key> <value> 覆盖。'
            labels = {s['key']: s['label'] for s in group_settings.schema()}
            rows = ['%s=%s' % (labels.get(k, k), v) for k, v in sorted(ov.items())]
            return '本群单独覆盖 %d 项：%s' % (len(ov), ' · '.join(rows))
        if sub == 'set':
            if len(parts) < 3:
                return usage + key_help
            try:
                group_settings.set_many(gid, {parts[1].strip(): parts[2].strip()})
            except ValueError as e:
                return '没改成：%s' % e
            return '好，本群的 %s 已经单独设为 %s（立即生效）。' % (parts[1], parts[2])
        if sub == 'unset':
            if len(parts) < 2:
                return usage + key_help
            try:
                group_settings.unset(gid, parts[1])
            except ValueError as e:
                return '没改成：%s' % e
            return ('好，本群全部改回跟随全局。' if parts[1].strip().lower() == 'all'
                    else '好，本群的 %s 改回跟随全局。' % parts[1])
        if sub == 'copy':
            if len(parts) < 2:
                return usage
            try:
                group_settings.copy(parts[1].strip(), gid)
            except ValueError as e:
                return '没复制成：%s' % e
            return '好，已把群 %s 的单独设置复制到本群。' % parts[1].strip()
        if sub == 'list':
            rows = []
            for g in group_settings.known_groups():
                on = group_settings.enabled(g, legacy_off=off)
                rows.append('%s(%s)' % (g, '开' if on else '关'))
            return '已知群：%s' % ('、'.join(rows) or '无')
        return usage

    # ---- 角色切换 ----
    def _character_switch(self, msg, args=None):
        """全局角色切换：/character [Character|Alt]。只改指针，不动资产。"""
        admin = [str(a) for a in (getattr(self.args, 'admin', None) or [])]
        if not admin:
            return '切换角色需要先配置管理员（--admin 或 QQ_ADMIN）。'
        if str(msg.uid) not in admin:
            return '切换角色只有管理员能改。'

        current = persona.active_id()
        names = []
        for pid in persona.list_ids():
            ok, _reason = persona.validate(pid, strict=True)
            names.append('%s（%s）%s' % (persona.display_name(pid), pid.capitalize(),
                                        '' if ok else '资产不全'))
        usage = '用法：/character %s' % ' | '.join(
            p.capitalize() for p in persona.list_ids())

        want_text = (args[0].strip() if args else '')
        if not want_text:
            return '当前角色：%s。可用：%s；%s。' % (
                persona.display_name(current), '、'.join(names), usage)
        want = persona.resolve_id(want_text)
        if not want:
            return '没有这个角色。可用：%s；%s。' % ('、'.join(names), usage)
        if want == current:
            return '现在就是%s。' % persona.display_name(want)

        ok, message = persona.activate(want)
        if not ok:
            return '切换失败：%s' % message
        try:
            self._check_persona_switch()
        except Exception:
            LOG.exception('切换角色后清理上下文失败')
        spec, _why = persona.qq_profile(want)
        if spec:
            threading.Thread(target=self.sync_qq_profile, args=(want,),
                             name='qq-profile-sync', daemon=True).start()
        LOG.info('管理员 %s 通过 /character 切换角色：%s → %s',
                 msg.uid, current, want)
        return '角色已切换为 %s（%s）。' % (persona.display_name(want), want)

    def _send_rank_up(self, msg, rel):
        """关系升档提醒：群里 @ 对方发，私聊就在私聊发。"""
        if not rel or not rc.get('cg_enabled', True):
            return
        if not persona.capabilities().get('cg'):
            return
        if msg.is_group:
            text = ('<@%s> 我们的关系好像走到「%s」了。'
                    '想留下这一刻的话，@我 发 /cg，我给你画一张。'
                    % (msg.uid, rel))
            self._send(msg, text, force_ats={msg.uid})
        else:
            text = ('我们的关系好像走到「%s」了。'
                    '想留下这一刻的话，发 /cg，我给你画一张。' % rel)
            self._send(msg, text)

    def _diag(self):
        """`/diag` —— 一次性把整条链路的健康状况报出来。

        「群里发了但没反应」有好几种原因（NapCat 掉线 / 事件没推过来 /
        门禁判了 ignore / 队列堵死 / TTS 或 ComfyUI 没起），挨个查一遍最快。
        """
        lines = ['【链路诊断】']

        # 1) NapCat（反向 WS）
        try:
            status = self.link.call_api('get_status', timeout=6)
            if status is None:
                lines.append('NapCat：❌ 没响应（WS 可能已断或 QQ 掉线）')
            else:
                stat = status.get('stat') or {}
                lines.append('NapCat：✅ 在线（%s）'
                             % (stat.get('network_type') or '连接正常'))
        except Exception as e:
            lines.append('NapCat：❌ 异常 %s' % e)

        # 2) QQ 登录状态
        try:
            info = self.link.call_api('get_login_info', timeout=6)
            if info:
                lines.append('QQ 登录：✅ %s（%s）'
                             % (info.get('nickname') or '?', info.get('user_id') or '?'))
            else:
                lines.append('QQ 登录：❓ 拿不到（可能未登录）')
        except Exception as e:
            lines.append('QQ 登录：❌ %s' % e)

        # 3) bot 自己的队列与统计
        with self.groups_lock:
            groups = list(self.groups.values())
        lines.append('bot：收到事件 %d 条 / 回复 %d / 忽略 %d / 去重 %d'
                     % (self.stats.get('events', 0), self.stats.get('replies', 0),
                        self.stats.get('ignored', 0), self.stats.get('dup', 0)))
        for gs in groups:
            lines.append('  群 %s：队列 %d / 丢弃 %d'
                         % (gs.gid, gs.queue.qsize(), gs.dropped))

        # 4) 媒体（TTS / CG）状态由 media_port 汇报
        lines.extend(self.media.tts_diag_lines())

        return '\n'.join(lines)

    def _remembered(self, uid):
        try:
            with mem.state_scope(core.user_dir(uid)):
                m = mem.load()
        except Exception:
            return '想不起来了。'
        eps = [e for e in (m.get('episodes') or []) if not e.get('inactive')][-5:]
        if not eps:
            return '关于你，我还什么都没记住呢。'
        lines = ['我记得这些：']
        for e in eps:
            lines.append('· %s' % (e.get('summary') or '（片段）')[:60])
        return '\n'.join(lines)

    def _status(self, msg):
        gs = self.groups.get(msg.gid or ('p%s' % msg.uid))
        tts_lab = self.media.tts_label(msg)
        lines = ['角色：%s' % persona.display_name(),
                 '模型：%s' % self.model,
                 '判官/记忆：%s' % self.judge_model,
                 '语音：%s' % tts_lab,
                 hw.summary(),
                 '事件 %d / 回复 %d / 沉默 %d / 去重 %d' % (
                     self.stats['events'], self.stats['replies'],
                     self.stats['ignored'], self.stats['dup'])]
        if gs:
            wait = gate.cooldown_remaining(gs.ctx, msg.uid, time.time())
            lines.append('队列 %d/%d（丢弃 %d）· 冷却还需 %.0fs' % (
                gs.queue.qsize(), cfg.BRIDGE_QUEUE_MAX, gs.dropped, wait))
            lines.append('顶替 %d / 打断 %d / 迟到取消 %d' % (
                self.stats.get('preempted', 0), self.stats.get('interrupted', 0),
                self.stats.get('cancel_late', 0)))
        if msg.gid:
            lines.append(activity.summary(msg.gid))
        else:
            st = private_topic.get_state(msg.uid)
            if st:
                nxt = st.get('next_due_ts') or 0
                when = (datetime.datetime.fromtimestamp(nxt).strftime('%m-%d %H:%M')
                        if nxt else '待定')
                lines.append('私聊主动：%s · 今日 %d/%d · %s · 下次 %s' % (
                    '开' if private_topic.is_enabled() else '关',
                    int(st.get('proactive_today') or 0),
                    int(rc.get('private_proactive_daily_max',
                               cfg.PRIVATE_PROACTIVE_DAILY_MAX)),
                    '等你回复' if st.get('pending') else '可候选', when))
        return '\n'.join(lines)
