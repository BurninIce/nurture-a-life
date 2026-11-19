# -*- coding: utf-8 -*-
"""QQ 接入层（OneBot v11 反向 WebSocket）：只做传输与调度，人格/记忆全在 rp_core。

分层：
    NapCat（登录 QQ，把事件推到本服务）
        → 本文件：解析事件 / 去重 / 群发言决策 / 单群串行队列 / 指令
        → rp_core：system 组装 + 生成
        → memory_card / member_card / group_activity：每人一份记忆与成员卡

本文件**不含任何人格文案**，只负责"谁说了什么、要不要回、什么时候回、回完记什么"。

启动（先在 NapCat 里加"反向 WebSocket"，地址 ws://127.0.0.1:6700/onebot/v11/ws）：
    py -3.13 scripts/qq_bridge.py --group 123456789 --admin 10001
    py -3.13 scripts/qq_bridge.py --dry-run          # 只打印不发送（首次联调建议）
    py -3.13 scripts/qq_bridge.py --selftest         # 离线自测，不需要 QQ/Ollama/websockets

依赖：websockets（pip install websockets）。只有真正连 QQ 时才需要；--selftest 不需要。

指令（群聊/私聊都可用，作用域只到自己）：
    /relation  她与你的关系数值
    /remember  她记得你的最近几件事
    /forget    清空她关于你的记忆（可撤销：备份仍在 state/personas/<id>/users/<uid>/backups/）
    /status    队列/冷却/活跃时段（仅 --admin 指定的人）
    /topic     让她现在主动找个话题（仅 --admin 指定的人，私聊）
    /tts       语音：`/tts <内容>` 只这一句带语音 · `/tts on|off` 本会话常开/关 ·
               `/tts daily|battle` 换语气 · 光发 `/tts` 下一条带语音
    /group     本群开关：`/group off` 本群不接话、`/group on` 叫回来、
               `/group` 看状态（仅管理员；全局开关仍以 UI 的「群聊」为准）
    /cg        用本地 ComfyUI 画一张；`/cg new` 强制新图；
               `/cg <内容>` 追加自定义画面要求（后两者仅管理员）
    /          `/ <内容>` OOC 旁注：只有你自己看，不进入上下文/记忆，也不回复
    /help      指令列表
    /diag      链路诊断

语音需要另行启动 GPT-SoVITS 服务（默认 127.0.0.1:9880，见 README「语音」）；
服务没起时 /tts 只发文字，不报错。
"""
import argparse
import datetime
import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import daily_scene
import heartbeat
import lifecycle
import group_activity as activity
import group_gate as gate
import group_roster
import group_scene as group_scene_mod
import group_settings
import hw_profile as hw
import logutil
import media_understand
import memory_card as mem
import member_card
import persona
import paths
import private_topic
import rp_config as cfg
import rp_core as core
import runtime_config as rc
import self_state
import tools
from rp_common import read_key_file
from bridge_commands import CommandMixin
from bridge_media import MediaMixin
from bridge_transport import (OneBotLink, TransportMixin,
                              strip_at_tags, _num, _resolve_at,
                              _normalize_bare_ats, _text_segments, _file_uri,
                              _segments, _AT_TAG, _BARE_AT)

from bridge_message import (
    Msg, PLACEHOLDER_IMAGE, PLACEHOLDER_FACE,
    parse_message, has_real_text, _turns_for_reply,
    _repeats_self, _claims_resend, split_bubbles, split_reply,
)

LOG = logging.getLogger('qq_bridge')

# ---------- 群状态 ----------

class Job(object):
    __slots__ = ('msg', 'kind', 'created', 'cancel', 'replaced_by', 'sent', 'done',
                 'ctx', 'judge', 'decided', 'replaced_ctx', 'replaced_judge',
                 'replaced_direct', 'direct')

    def __init__(self, msg, kind='reply', ctx=None, judge=False, direct=False):
        self.msg = msg
        self.kind = kind      # reply / proactive
        self.created = time.time()
        self.cancel = threading.Event()
        self.replaced_by = None   # 顶替它的那条新消息（preempt）；有值才重跑
        self.sent = 0             # 已发出的块数：0 = 尚未产生外部输出
        self.done = False
        self.direct = direct      # 入队时的 @ / 点名：队列满时优先保住
        # 群发言决策延后到 worker：ctx 是这条消息**到达时**的快照，
        # 排队期间的新消息只改 gs.ctx，不会污染这条的判断上下文。
        self.ctx = ctx
        self.judge = judge        # 是否需要 L2 模型终审（群聊回复为 True）
        self.decided = False      # 仅供排查（决策在 worker 内完成）
        self.replaced_ctx = None
        self.replaced_judge = False
        self.replaced_direct = False


class GroupState(object):
    """一个群的会话状态：最近对话、冷却、串行队列。"""

    def __init__(self, gid, maxsize=None):
        self.gid = gid
        self.turns = []                     # [{uid,nickname,text,ts,ats}] 最多 48 条
        self.speakers = deque(maxlen=8)     # 最近发言者 [(uid, nick)]
        self.ctx = {'turns': [], 'now': 0, 'reply_ts_list': [],
                    'last_reply_by_uid': {}, 'last_text_by_uid': {},
                    'last_reply_ts': 0, 'last_proactive_ts': 0,
                    'proactive_today': 0, 'proactive_day': None}
        self.queue = queue.Queue(maxsize=maxsize or cfg.BRIDGE_QUEUE_MAX)
        self.dropped = 0
        self.worker = None
        self.inflight = None                # 正在跑的那一轮（preempt/interrupt 的目标）
        self.lock = threading.Lock()

    # ---- 上下文 ----
    def snapshot_ctx(self):
        """决策用的上下文快照（到达时刻）。

        worker 里判一条消息时，gs.ctx 可能已经混进了后面排队的消息；
        这里把会变的部分拷一份，保证每条消息按它到达时的语境判断。
        """
        c = dict(self.ctx)
        c['turns'] = [dict(t) for t in (self.ctx.get('turns') or [])]
        c['reply_ts_list'] = list(self.ctx.get('reply_ts_list') or [])
        c['last_reply_by_uid'] = dict(self.ctx.get('last_reply_by_uid') or {})
        c['last_text_by_uid'] = dict(self.ctx.get('last_text_by_uid') or {})
        # 记忆/场景片段由 worker 现取，快照里不带旧列表
        c.pop('memory_snippets', None)
        c.pop('scene_snippets', None)
        return c

    def add_incoming(self, msg):
        # mid 必存：_run_job 构造 messages 时要按它剔除"当前这条"在 turns 里的副本，
        # 否则同一句话会连着出现两遍（见 _turns_for_reply）。
        turn = {'uid': msg.uid, 'mid': msg.mid, 'nickname': msg.nick, 'text': msg.text,
                'ts': msg.ts, 'ats': list(getattr(msg, 'ats', None) or []),
                'images': list(getattr(msg, 'images', None) or [])}
        self.turns.append(turn)
        self.ctx['turns'].append(dict(turn))
        self.speakers.appendleft((msg.uid, msg.nick))
        self._trim()

    def patch_incoming(self, mid, text):
        """识别/抓取完成后补写回上下文。

        图片描述与链接摘要是网络调用，已经挪到 worker 里做；但消息在入队前就
        进了 turns，不补写的话模型看到的是 [图片] 占位符。
        """
        if not mid:
            return
        for bucket in (self.turns, self.ctx['turns']):
            for turn in reversed(bucket):
                if turn.get('mid') and str(turn['mid']) == str(mid):
                    turn['text'] = text
                    break

    def add_self(self, text, ts=None, to=''):
        ts = ts or time.time()
        turn = {'self': True, 'text': text, 'ts': ts, 'to': to}
        self.turns.append(dict(turn))
        self.ctx['turns'].append(dict(turn))
        self._trim()

    def _trim(self):
        # 群聊上下文只要最近两屏；留 2 倍是因为 decision 与 messages 各用一份。
        # 条数按本群设置（未覆盖则跟随全局），改完立刻生效。
        keep = int(group_settings.limits(self.gid)['turn_limit']) * 2
        if len(self.turns) > keep:
            self.turns = self.turns[-keep:]
        if len(self.ctx['turns']) > keep:
            self.ctx['turns'] = self.ctx['turns'][-keep:]

    def speaker_list(self, limit=None):
        out, seen = [], set()
        for uid, nick in self.speakers:
            if uid in seen:
                continue
            seen.add(uid)
            out.append({'uid': uid, 'nickname': nick})
            if limit and len(out) >= limit:
                break
        return out

    # ---- 队列 ----
    def submit(self, job):
        """入队；满了优先丢**非 @** 的最旧任务，全是 @ 才丢最旧那条。

        为什么改了：决策挪到 worker 后，闲聊消息也会进队。若仍按纯 FIFO 丢最旧，
        一波刷屏就能把排在前面的 @ 顶掉。这里只换"丢谁"，不改变有界队列本身。
        """
        try:
            self.queue.put_nowait(job)
            return True
        except queue.Full:
            pass
        with self.queue.mutex:
            q = self.queue.queue
            # deque 支持按下标赋值：原地换掉一条，不动 unfinished_tasks / 条件变量
            idx = next((i for i, j in enumerate(q)
                        if not getattr(j, 'direct', False)), None)
            if idx is None:
                if not getattr(job, 'direct', False):
                    self.dropped += 1      # 全是 @，新来的旁听消息自己让路
                    return False
                idx = 0                     # 全是 @：仍然丢最旧（与原策略一致）
            old = q[idx]
            q[idx] = job
        self.dropped += 1
        LOG.warning('群 %s 队列已满，丢弃过期任务（%s）', self.gid,
                    (old.msg.text or '')[:20])
        return True


# ---------- 主桥接 ----------

_AT_UID_RE = re.compile(r'<@!?(\d+)>')


def _reply_target(reply, msg, gs, proactive=False):
    """她这句说给谁：明确的 <@uid> 优先，否则就是本轮说话人；主动开口没有对象。

    只认显式 @：从正文猜人名会把昵称里的常用词误命中，记错收件人比不记更糟——
    system 里会直接告诉她「上一条是对 X 说的」。
    """
    if proactive or not msg.is_group:
        return ''
    m = _AT_UID_RE.search(reply or '')
    if m:
        uid = m.group(1)
        for sp in gs.speaker_list():
            if str(sp.get('uid')) == uid and (sp.get('nickname') or '').strip():
                return sp['nickname'].strip()
        name = (member_card.load(uid).get('nickname') or '').strip()
        if name:
            return name
    return (msg.nick or '').strip()


class Bridge(TransportMixin, CommandMixin, MediaMixin):

    def __init__(self, args):
        self.args = args
        # 默认模型：命令行 > 运行时配置（热生效）> QQ_MODEL > MODEL_DEFAULT
        self.model = (args.model or rc.get('gen_model', '')
                      or getattr(cfg, 'QQ_MODEL', '') or cfg.MODEL_DEFAULT)
        # 判断/记忆用独立模型：character 会演戏但不会做二分类，
        # 实测把「@角色 你在吗」判成「不想参与」。分开后互不牵制。
        self.judge_model = (getattr(args, 'judge_model', None)
                            or rc.get('judge_model', '')
                            or getattr(cfg, 'QQ_JUDGE_MODEL', '') or self.model)
        # 若判官是 API、主模型是本地，仍按本地并发；反之按 API 并发
        kind = core.resolve_model(self.model)[0]
        if kind == 'local':
            conc = hw.CONCURRENCY
        elif kind == 'localapi':
            conc = int(getattr(cfg, 'LOCAL_API_CONCURRENCY', 2))
        else:
            conc = int(getattr(cfg, 'API_CONCURRENCY', 4))
        self.kind, self.conc = kind, conc
        self.self_id = str(getattr(args, 'self_id', '') or '')   # 预置 bot QQ，首条消息前就能识别 @
        self.groups = {}
        self.groups_lock = threading.Lock()
        self._persona_pid = persona.active_id()
        self._persona_lock = threading.Lock()
        self._profile_lock = threading.Lock()
        self.sem = threading.Semaphore(conc)   # 本地看显存 / API 看配额，见上
        self.our_mids = deque(maxlen=200)
        self.seen = deque(maxlen=3000)
        self.seen_set = set()
        # 图片识别在消息到达时就跑（见 _prefetch_images），回复前再等它落地。
        # 单独两条线程：识图是网络调用，不能占用生成并发，也不能堵 OneBot 读线程。
        self._vision_pool = ThreadPoolExecutor(max_workers=2,
                                               thread_name_prefix='vision')
        self._vision_futures = {}
        self._vision_lock = threading.Lock()
        self.link = None           # set_link() 后可用；selftest 里换成 FakeLink
        self._judge = None
        self._pjudge = None
        self._ppjudge = None
        self.stats = {'events': 0, 'replies': 0, 'ignored': 0, 'dup': 0,
                      'private': 0, 'errors': 0, 'memory_errors': 0,
                      'private_proactive': 0,
                      'preempted': 0, 'interrupted': 0, 'cancel_late': 0}
        # 媒体（TTS/CG）全部经 media_port，桥接只调用，不 import 任何支线实现。
        self._init_media()

    # ---- 生命周期 ----
    def set_link(self, link):
        self.link = link

    def _sync_models(self):
        """面板切生成/判官模型后热生效（启动时只读一次，不重读就永远用旧档）。

        命令行显式 -m / --judge-model 优先，不被面板覆盖。
        """
        try:
            want = (self.model if getattr(self.args, 'model', None)
                    else rc.get('gen_model', '') or self.model)
            judge = (self.judge_model if getattr(self.args, 'judge_model', None)
                     else rc.get('judge_model', '') or self.judge_model)
        except Exception:
            return
        if want != self.model:
            self.model = want
            self.kind = core.resolve_model(want)[0]
            LOG.info('生成模型已热切换：%s', want)
        if judge != self.judge_model:
            self.judge_model = judge
            self._judge = self._pjudge = self._ppjudge = None
            LOG.info('判官模型已热切换：%s', judge)

    def judge(self):
        if self.args.no_judge:
            return None
        if self._judge is None:
            self._judge = gate.make_llm_judge(self.judge_model)
        return self._judge

    def _proactive_judge(self):
        if self.args.no_judge:
            return lambda text, ctx: (True, 'no_judge')
        if self._pjudge is None:
            self._pjudge = gate.make_proactive_judge(self.judge_model)
        return self._pjudge

    def _private_proactive_judge(self):
        if self.args.no_judge:
            return lambda text, ctx: (True, 'no_judge')
        if self._ppjudge is None:
            self._ppjudge = private_topic.make_judge(self.judge_model)
        return self._ppjudge

    def warm_rosters(self):
        """连接建立后预热群名册（独立线程：call_api 不能跑在 WS 读线程里）。"""
        gids = set(self.args.group or [])
        with self.groups_lock:
            gids.update(self.groups.keys())
        for gid in gids:
            if not str(gid).isdigit():   # 私聊会话键是 private:<uid>，不是群号
                continue
            try:
                group_roster.refresh_async(gid, self.link, min_gap=0)
            except Exception:
                LOG.debug('预热群名册失败：%s', gid)

    def group_state(self, gid):
        with self.groups_lock:
            gs = self.groups.get(gid)
            if gs is None:
                gs = GroupState(gid)
                self.groups[gid] = gs
                gs.worker = threading.Thread(target=self._worker_loop, args=(gs,),
                                             name='grp-%s' % gid, daemon=True)
                gs.worker.start()
                LOG.info('群 %s 已建立会话（独立串行队列）', gid)
            return gs

    # ---- 事件入口 ----
    def on_event(self, ev):
        """处理一条 OneBot 事件（WS 线程 / selftest 都走这里）。"""
        pt = ev.get('post_type')
        if pt == 'meta_event':
            sid = ev.get('self_id')
            if sid and not self.self_id:
                self.self_id = str(sid)
                LOG.info('bot QQ = %s', self.self_id)
            return
        if pt == 'message_sent':
            # 她自己发出的消息：记下 message_id，用于识别"回复她"
            if ev.get('message_id'):
                self.our_mids.append(str(ev['message_id']))
            return
        if pt != 'message':
            return

        self.stats['events'] += 1
        msg = parse_message(ev, self.self_id, set(self.our_mids))
        if not msg:
            LOG.debug('不是可处理的消息事件：%s', ev.get('message_type'))
            return

        # 自消息回流（有的实现会把自己的消息也推回来）：必须挡住，否则自问自答
        if self.self_id and msg.uid == self.self_id:
            LOG.debug('忽略自己的消息：%s', msg.text[:30])
            return
        if self._is_dup(msg):
            self.stats['dup'] += 1
            LOG.debug('重复消息已忽略：%s', msg.text[:30])
            return
        # 无文字内容的消息：纯图片/纯表情。含图片或链接的消息即使没有文字
        # 也值得处理（后面会识别图片/抓取链接，让模型「看懂」）。
        if not has_real_text(msg.text) and not msg.images and not msg.links:
            LOG.debug('跳过无文字内容的消息：%r', msg.text)
            return
        if msg.is_group and self.args.group and msg.gid not in self.args.group:
            LOG.debug('群 %s 不在守候列表，忽略', msg.gid)
            return   # 只守指定群（留空=所有群）

        # 群聊总开关关闭：整条群消息不接管——不记活跃、不登记成员/气泡、不走指令、不回应
        if msg.is_group and not rc.get('group_enabled', getattr(cfg, 'QQ_GROUP_ENABLED', True)):
            LOG.debug('群聊已关闭，忽略群消息：%s', msg.text[:30])
            return
        # 群级关闭：本群不接管，但管理员可以发 /group on 把自己叫回来，
        # 否则关掉之后在群里就再也打不开了。全局角色切换同理。
        if msg.is_group and not rc.group_on(msg.gid):
            cmd_text = re.sub(r'^@[^/\s]+\s*', '', (msg.text or '').strip())
            admin = [str(a) for a in (getattr(self.args, 'admin', None) or [])]
            head = cmd_text.lower().split(None, 1)[0] if cmd_text else ''
            if head in ('/group', '/character') and str(msg.uid) in admin:
                msg.text = cmd_text
                if self.handle_command(msg) is True:
                    return
            LOG.debug('群 %s 已单独关闭，忽略群消息：%s', msg.gid, msg.text[:30])
            return

        activity.record(msg.gid or ('p%s' % msg.uid), msg.ts)
        core.note_speaker(msg.uid, msg.nick, group_id=msg.gid)
        # 群体层：把发言人登记进群聊气泡的成员列表（只记昵称，风格由归纳后写回）
        if msg.is_group and msg.gid:
            try:
                group_scene_mod.set_member_style(msg.gid, msg.uid, msg.nick, '')
                # 立即落盘：这个成员列表是名册拉取失败时的兜底，重启后还要用
                group_scene_mod.flush(msg.gid)
            except Exception as e:
                logutil.throttled(LOG, 'group_scene_snapshot', logging.WARNING,
                                  '群成员快照写入失败（不影响回复）：%s', e)

        # 群里 @她 再打指令时，文本是「@角色/relation」（NapCat 拼接 at 段和 text 段时
        # @ 后面未必有空格），不以 '/' 开头，指令会被当普通消息吞掉。
        # 判断前先把 @ 占位符剥掉——注意 @ 后不能跨 '/'，否则会把指令本身吃掉。
        cmd_text = re.sub(r'^@[^/\s]+\s*', '', (msg.text or '').strip())
        if cmd_text.startswith('/'):
            msg.text = cmd_text
            if self.handle_command(msg) is True:
                return
            # 返回 False = 语音指令，借正常对话流程生成回复，文字发完再补语音

        # 语音触发判定（/tts 已在指令层标记过 want_tts，这里只处理其余情况）
        if not msg.want_tts:
            self._apply_tts_triggers(msg)

        if msg.is_group:
            self.handle_group(msg)
        else:
            allow = rc.get('private_allow', []) or []
            if allow and str(msg.uid) not in [str(a) for a in allow]:
                return   # 私聊白名单开启且该号不在名单：静默不回
            self.stats['private'] += 1
            self.handle_private(msg)

    def _is_dup(self, msg):
        """按 message_id 去重（断线重连会重投同一事件）。"""
        key = msg.mid or '%s|%s|%s' % (msg.uid, msg.ts, msg.text)
        if key in self.seen_set:
            return True
        if len(self.seen) == self.seen.maxlen:
            old = self.seen[0]
            self.seen_set.discard(old)
        self.seen.append(key)
        self.seen_set.add(key)
        return False

    # ---- 群聊 ----
    def handle_group(self, msg):
        gs = self.group_state(msg.gid)
        gs.ctx['now'] = msg.ts
        gs.ctx['at_bot'] = bool(msg.at_self)          # 每条都要刷新（不能残留上一条）
        gs.ctx['reply_to_her'] = bool(msg.reply_to_her)
        hours, ainfo = activity.active_hours(msg.gid, now=msg.ts)
        gs.ctx['active_hours'] = hours
        gs.ctx['active_source'] = ainfo['source']
        gs.ctx['neglected'] = bool(gs.ctx.get('last_reply_ts')) and \
            msg.ts - gs.ctx['last_reply_ts'] > 300

        # 发言决策（L1 相似度要 embedding、L2 是一次模型调用）整体放到每组串行
        # worker：放在 OneBot 读线程里会把群里后续所有事件一起堵住。这里只留一份
        # 到达时的 ctx 快照，再交给队列；@ / 点名在入队时标出来，队列满时优先保住。
        ctx = gs.snapshot_ctx()
        gs.add_incoming(msg)
        self._attach_quoted_images(gs, msg)
        self._prefetch_images(gs, msg)
        gate.note_seen(gs.ctx, msg.uid, msg.text, msg.ts)

        # 图片识别/链接抓取也在 worker（见 _run_job）：它们是网络调用，
        # 放在接收线程里会堵住后续事件。判定用占位符文本即可。
        self._dispatch(gs, msg, ctx=ctx, judge=True,
                       direct=bool(msg.want_tts or gate.is_direct(msg.text, ctx)))

    def handle_private(self, msg):
        """私聊：不需要发言决策，直接回（但仍走同一个 worker 串行队列）。"""
        # 用户有动静：清掉「上次主动未回复」并重排下一次候选时间
        try:
            private_topic.note_incoming(msg.uid, msg.ts)
        except Exception:
            LOG.exception('私聊主动状态更新失败（不影响回复）')
        gs = self.group_state('private:%s' % msg.uid)
        gs.ctx['now'] = msg.ts
        gs.ctx['at_bot'] = bool(msg.at_self)          # 每条都要刷新（不能残留上一条）
        gs.ctx['reply_to_her'] = bool(msg.reply_to_her)
        gs.add_incoming(msg)
        self._attach_quoted_images(gs, msg)
        self._prefetch_images(gs, msg)
        self._dispatch(gs, msg)

    def _dispatch(self, gs, msg, ctx=None, judge=False, direct=False):
        """投递一轮回复：能顶替正在跑的那一轮就顶替，否则排队。

        投递语义（对齐 Cortico 的 trigger）：
          preempt   —— 上一轮还没发出任何内容：取消它，改用这条消息重跑，只发一条回复。
          interrupt —— 上一轮已经发出过内容：只停它的后续块，这条照常排队。
          debounce  —— 其余情况就是排队（本队列本来就是合批的最小实现）。
        """
        if self._try_preempt(gs, msg, ctx=ctx, judge=judge, direct=direct):
            return
        self._try_interrupt(gs, msg)
        ok = gs.submit(Job(msg, 'reply', ctx=ctx, judge=judge, direct=direct))
        if not ok:
            LOG.warning('群 %s 队列拒绝任务（已丢弃 %d 条）', gs.gid, gs.dropped)

    def _should_preempt(self, job, msg):
        """这条消息有没有资格顶替 job。

        只认两种：job 是她自己起的头（人一说话就该让位）、同一个人紧接着补的话。
        群友交替发言不顶替——那是正常插话，应该排队而不是互相取消。

        间隔按**两条消息自己的时间**算（QQ 服务器时间），不按本地处理耗时：
        入口要做 embedding 检索，消息从到达处理完可能要好几秒，用本地时钟会把
        真正的纠正误判成过期。
        """
        if job.kind == 'proactive':
            return True
        base = job.replaced_by or job.msg
        if not msg.is_group and base.is_group:
            return False
        if base.uid != msg.uid:
            return False
        window = float(getattr(cfg, 'BRIDGE_PREEMPT_WINDOW', 3.0))
        gap = abs(float(msg.ts or 0) - float(base.ts or 0))
        if gap <= window:
            return True
        # 带纠正词的同一人补话放宽一点，但仍要近期才算（不当成十分钟后的置顶）
        return gap <= 15 and any(w in (msg.text or '') for w in
                                 getattr(cfg, 'BRIDGE_PREEMPT_WORDS', ()))

    def _try_preempt(self, gs, msg, ctx=None, judge=False, direct=False):
        """尚未产生外部输出 → 取消当前这轮，改由这条消息重跑。返回是否接手。"""
        if not getattr(cfg, 'BRIDGE_PREEMPT', True):
            return False
        with gs.lock:
            job = gs.inflight
            if job is None or job.done or job.sent > 0:
                return False
            if not self._should_preempt(job, msg):
                return False
            job.replaced_by = msg
            job.replaced_ctx = ctx
            job.replaced_judge = judge
            job.replaced_direct = direct
            job.cancel.set()
        self.stats['preempted'] = self.stats.get('preempted', 0) + 1
        LOG.info('[群 %s] 顶替上一轮未发出的生成：%s', gs.gid, (msg.text or '')[:24])
        return True

    def _try_interrupt(self, gs, msg):
        """已经发出过内容 → 只停后续块，这条消息照常排队。"""
        if not getattr(cfg, 'BRIDGE_PREEMPT', True):
            return False
        with gs.lock:
            job = gs.inflight
            if job is None or job.done or job.sent == 0 or job.cancel.is_set():
                return False
            if not self._should_preempt(job, msg):
                return False
            job.cancel.set()
        self.stats['interrupted'] = self.stats.get('interrupted', 0) + 1
        LOG.info('[群 %s] 打断已开口的那一轮（%d 块后）：%s', gs.gid, job.sent,
                 (msg.text or '')[:24])
        return True

    # ---- 执行 ----
    def _worker_loop(self, gs):
        while True:
            job = gs.queue.get()
            try:
                # 被顶替的任务返回一条新的（preempt），就地继续跑，不重排队：
                # 重排队会再等一轮，纠正消息的意义就没了。
                while job is not None:
                    with gs.lock:
                        gs.inflight = job
                    nxt = self._run_job(gs, job)
                    job.done = True
                    job = nxt
            except Exception:
                self.stats['errors'] += 1
                LOG.exception('处理消息失败：%r', job.msg if job else None)
            finally:
                with gs.lock:
                    gs.inflight = None
                gs.queue.task_done()

    def _enhance_text(self, msg, images_only=False):
        """识别图片 / 抓取链接，把结果拼进 msg.text，让模型「看懂」非文字内容。

        全部失败静默：识别不出就保留 [图片] 占位符，抓不到链接就保留原链接。
        images_only: 后台预识别只做图，不抓链接（链接等真要回时再抓，省额度）。
        """
        try:
            do_img = bool(rc.get('image_understand', True))
            do_link = bool(rc.get('link_fetch', True))
        except Exception:
            do_img = do_link = True

        # 图片：逐个识别，把 [图片] 占位符替换成 [图片：<描述>]
        if do_img and msg.images:
            max_img = int(getattr(cfg, 'IMAGE_MAX_PER_MSG', 3))
            for u in msg.images[:max_img]:
                res = tools.run('describe_image',
                                {'uid': msg.uid, 'gid': msg.gid}, image_url=u)
                d = res.text if res.ok else ''
                if not d:
                    continue
                if PLACEHOLDER_IMAGE in msg.text:
                    msg.text = msg.text.replace(PLACEHOLDER_IMAGE,
                                                '[图片：%s]' % d, 1)
                else:
                    # 占位符已被消耗/不存在，追加到末尾（纯图片无文字时兜底）
                    msg.text = (msg.text + ' [图片：%s]' % d).strip()

        # 链接：抓取网页标题/摘要，附在链接后面
        if do_link and msg.links and not images_only:
            max_link = int(getattr(cfg, 'LINK_MAX_PER_MSG', 2))
            for u in msg.links[:max_link]:
                res = tools.run('fetch_link',
                                {'uid': msg.uid, 'gid': msg.gid}, url=u)
                if res.ok and res.text:
                    msg.text = msg.text.replace(u, '%s（链接内容：%s）' % (u, res.text), 1)

    # ---- 图片预识别：不回复也要看懂 ----
    def _attach_quoted_images(self, gs, msg):
        """引用别人的图再问「这张图」：引用段只给 message_id，图要从本地缓存里取。

        缓存就是我们自己的 turns（最多两屏）；被引用那条已经被裁掉时取不到，
        只能在上下文里退回到当时的文字（通常是「[图片]」）。
        """
        if not msg.reply_mid:
            return
        for t in reversed(list(gs.turns)):
            if str(t.get('mid') or '') == msg.reply_mid:
                for u in t.get('images') or []:
                    if u not in msg.images:
                        msg.images.append(u)
                break

    def _prefetch_images(self, gs, msg):
        """消息一到就先认图，不等发言决策。

        门控判 ignore 的消息原本不会走 _enhance_text，图就永远只有 [图片]；
        两分钟后别人问「她怎么看这幅画」，上下文里只剩占位符。
        """
        if not msg.images:
            return
        try:
            if not bool(rc.get('image_understand', True)):
                return
        except Exception:
            pass
        fut = self._vision_pool.submit(self._describe_images, gs, msg)
        with self._vision_lock:
            self._vision_futures[id(msg)] = fut

    def _describe_images(self, gs, msg):
        """（识图线程）认图并把描述写回文本与群上下文；只认一次。"""
        try:
            self._enhance_text(msg, images_only=True)
            gs.patch_incoming(msg.mid, msg.text)
        except Exception:
            LOG.exception('图片预识别失败（不影响主流程）')
        finally:
            # worker 回复时不再重复调视觉模型（失败也不重试，避免一串卡 20 秒）
            msg.images = []

    def _await_images(self, msg):
        """回复前等后台识图落地；通常入队时已经跑完。"""
        with self._vision_lock:
            fut = self._vision_futures.pop(id(msg), None)
        if fut is None:
            return
        try:
            fut.result(timeout=20)
        except Exception:
            LOG.warning('图片预识别等待超时，本轮先不带图片描述')

    def _tool_block(self, msg):
        """按需检索：router 命中才调工具，返回可注入 system 的【外部信息】块。

        默认全关（runtime_config.tools_enabled=False），关着时零行为变化。
        群聊默认只在被 @ 时路由——旁听消息联网会显著拉长响应时间。
        """
        r = tools.router.route(msg.text, is_group=msg.is_group,
                               at_self=bool(msg.at_self), gid=msg.gid)
        if not r:
            return ''
        res = tools.run(r.tool,
                        {'uid': msg.uid, 'gid': msg.gid, 'is_group': msg.is_group},
                        **r.args)
        LOG.info('工具：%s（%s）→ %s', r.tool, r.reason,
                 'ok' if res.ok else res.error)
        return tools.render_block(res)

    def _recent_group_turns(self, gs, exclude='', limit=4):
        """最近几条「别人说的」群聊，给记忆归纳当语境。

        只取非 self 的发言、跳过与当前这条完全相同的文本（当前句已单独作为 user_msg 传入）。
        返回 [{'uid','nickname','text'}...] 时间正序；私聊不调用。
        uid 是 2026-10-28 加的：没有它就只能靠昵称反查，而群昵称会改
        （实测同一人用过 示例角色/某角色名/另一个昵称 三个名字），反查必错。
        """
        out = []
        for t in reversed([x for x in (gs.turns or []) if not x.get('self')]):
            nick = (t.get('nickname') or '').strip()
            text = (t.get('text') or '').strip()
            if not nick or not text or text == exclude:
                continue
            out.append({'uid': str(t.get('uid') or ''), 'nickname': nick, 'text': text[:40]})
            if len(out) >= limit:
                break
        return list(reversed(out))

    def _replacement(self, job):
        """被顶替的那轮返回新 job：带上新消息自己的到达快照。"""
        return Job(job.replaced_by, 'reply', ctx=job.replaced_ctx,
                   judge=job.replaced_judge, direct=job.replaced_direct)

    def _decide(self, gs, job):
        """在 worker 里判定这条群消息要不要接（L1 相似度 + L2 终审）。

        跑在全局信号量内：embedding 与判官模型都可能吃显存，和生成共用并发上限。
        返回 Decision；调用方按 action 决定继续生成还是沉默。
        """
        self._sync_models()
        msg = job.msg
        ctx = dict(job.ctx or {})
        # 记忆/场景片段现取：它们是磁盘 + embedding，不该占 OneBot 读线程。
        ctx['memory_snippets'] = core.memory_snippets(msg.uid)
        ctx['scene_snippets'] = group_scene_mod.snippets(msg.gid)
        # 排队期间已经发出去的回复要算进冷却/延续；turns 与 last_text 保持到达时快照，
        # 否则会把后面排队的消息当成"刚刚说过的话"，也会把本条自己判成重复。
        for k in ('last_reply_ts', 'reply_ts_list', 'last_reply_by_uid',
                  'her_last_ts', 'her_last_text'):
            if k in gs.ctx:
                ctx[k] = gs.ctx[k]
        d = gate.decide(msg.text, uid=msg.uid, nickname=msg.nick, ctx=ctx,
                        judge=self.judge(), model_key=self.judge_model,
                        direct=bool(job.direct))
        job.decided = True
        if self.args.decision_log:
            gate.log_decision(self.args.decision_log,
                              {'gid': msg.gid, 'uid': msg.uid, 'nickname': msg.nick,
                               'text': msg.text, 'action': d.action, 'score': d.score,
                               'signals': d.signals, 'reason': d.reason})
        LOG.info('[群 %s] %s：%s → %s(%.2f) %s', msg.gid, msg.nick,
                 msg.text[:24], d.action, d.score, d.reason)
        return d

    def _check_persona_switch(self):
        """面板切换角色后，丢掉旧角色的短期上下文与排队中的语音请求。"""
        pid = persona.active_id()
        with self._persona_lock:
            prev = self._persona_pid
            if pid == prev:
                return pid
            self._persona_pid = pid
            self.media.clear_pending_voice()
        with self.groups_lock:
            states = list(self.groups.values())
        for gs in states:
            gs.turns = []
            gs.speakers.clear()
            gs.ctx['turns'] = []
            gs.ctx['reply_ts_list'] = []
            gs.ctx['last_reply_by_uid'] = {}
            gs.ctx['last_text_by_uid'] = {}
            gs.ctx['last_reply_ts'] = 0
            gs.ctx['her_last_ts'] = 0
            gs.ctx['her_last_text'] = ''
        LOG.info('角色切换为 %s：已清空短期上下文（%d 个会话）',
                 persona.display_name(pid), len(states))
        return pid

    def _profile_journal(self, rec):
        path = os.path.join(core.STATE_ROOT, 'persona_profile.jsonl')
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            line = json.dumps(rec, ensure_ascii=False) + '\n'
            with self._profile_lock:
                with open(path, 'a', encoding='utf-8') as f:
                    f.write(line)
        except OSError:
            LOG.exception('QQ 档案同步日志写入失败')

    def _api_raw(self, action, params, timeout):
        """调用 NapCat 并保留 retcode；旧 link 没有 raw 方法时按 data 兜底。"""
        if hasattr(self.link, 'call_api_raw'):
            raw = self.link.call_api_raw(action, params, timeout=timeout)
        else:
            data = self.link.call_api(action, params, timeout=timeout)
            raw = {'retcode': 0 if data is not None else -1, 'data': data}
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _api_ok(raw):
        if not raw:
            return False
        if 'retcode' in raw:
            return raw.get('retcode') == 0
        return str(raw.get('status') or 'ok') == 'ok'

    def sync_qq_profile(self, pid=None):
        """按角色清单可选地同步 QQ 昵称/头像（严格校验后执行，写审计日志）。"""
        pid = pid or persona.active_id()
        spec, why = persona.qq_profile(pid)
        if not spec:
            return False, why or '该角色未启用 QQ 档案同步'
        if not self.link:
            return False, 'NapCat 未连接'
        rec = {'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'pid': pid, 'ok': False}
        try:
            raw = self._api_raw('set_qq_profile',
                                {'nickname': spec['nickname']}, 10)
            rec['nickname'] = {
                'ok': self._api_ok(raw),
                'value': spec['nickname'],
                'retcode': raw.get('retcode'),
                'status': raw.get('status'),
            }
            if spec.get('avatar'):
                avatar = spec['avatar'].replace('\\', '/')
                raw = self._api_raw('set_qq_avatar', {'file': avatar}, 20)
                rec['avatar'] = {
                    'ok': self._api_ok(raw),
                    'file': avatar,
                    'retcode': raw.get('retcode'),
                    'status': raw.get('status'),
                }
            info = self.link.call_api('get_login_info', {}, timeout=5)
            rec['verify'] = info if isinstance(info, dict) else None
            got = str((info or {}).get('nickname') or '')
            if got:
                rec['nickname']['verified'] = got == spec['nickname']
                if got != spec['nickname']:
                    rec['nickname']['ok'] = False
                    rec['error'] = '昵称未生效（当前：%s）' % got[:40]
            rec['ok'] = bool(rec['nickname']['ok']
                             and (not rec.get('avatar') or rec['avatar']['ok']))
        except Exception as e:
            rec['error'] = str(e)[:200]
            LOG.exception('QQ 档案同步失败')
        self._profile_journal(rec)
        if rec['ok']:
            LOG.info('QQ 档案已同步：%s / %s', spec['nickname'], pid)
            return True, ''
        LOG.warning('QQ 档案同步未确认：%s', rec.get('error') or rec)
        return False, rec.get('error') or 'QQ 档案同步未确认'

    def _run_job(self, gs, job):
        # 角色在任务开始时固定：在途任务不会因为面板切了指针而中途换人；
        # 真切换了就在发送前丢弃本轮（见下方 epoch 检查）。
        pid = self._check_persona_switch()
        with persona.context(pid):
            return self._run_job_inner(gs, job)

    def _run_job_inner(self, gs, job):
        msg = job.msg
        proactive = (job.kind == 'proactive')
        t0 = time.time()
        # 本群的上下文预算（未覆盖则跟随全局）；私聊不读群设置。
        limits = (group_settings.limits(msg.gid) if msg.is_group
                  else {'turn_limit': None, 'msg_max_chars': None,
                        'member_brief_limit': None})
        # 排队期间群聊被关掉：队列里剩下的群任务直接作废，不生成也不发送
        if msg.is_group and not rc.group_on(msg.gid):
            return None
        # 排队期间就被顶替了：直接换人，不必再花一次模型调用。
        if job.cancel.is_set() and job.replaced_by is not None:
            return self._replacement(job)
        with self.sem:            # 全局并发上限由 hw_profile 按显存决定
            # 等信号量期间也可能被顶替（8 人同时 @ 时这里会排一会儿）
            if job.cancel.is_set() and job.replaced_by is not None:
                return self._replacement(job)
            # 发言决策放在 worker：判明不回就不用再做图片/链接识别。
            if not proactive and job.judge:
                d = self._decide(gs, job)
                if job.cancel.is_set() and job.replaced_by is not None:
                    return self._replacement(job)
                if d.action != gate.REPLY:
                    self.stats['ignored'] += 1
                    return None
            # 图片识别 / 链接抓取是网络调用，放在 worker 里做：在接收线程里做会把
            # 群里后续所有事件一起堵住。做完补写回上下文的占位符。
            if not proactive:
                self._await_images(msg)
                self._enhance_text(msg)
                gs.patch_incoming(msg.mid, msg.text)
            # 私聊主动的素材：从她的记忆事件里挑一件最近、重要、没主动提过的事。
            private_seed = None
            if proactive and not msg.is_group:
                try:
                    private_seed = private_topic.pick_seed(
                        msg.uid, core.snapshot(msg.uid), allow_curiosity=True)
                except Exception:
                    LOG.exception('私聊主动素材挑选失败（不影响其他流程）')
            # 人物卡：群聊冷场主动时没有"当前发言人"，不注入；私聊主动有明确对象，照常注入。
            identity = ('' if proactive and msg.is_group
                        else core.identity_for(msg.uid, group=msg.is_group))
            # 群名册：让没在最近发言者里的群友（含重启前的）也能被认出来。
            # 首次同步拉一次（本地 WS），之后按 TTL 后台更新。
            if msg.is_group:
                try:
                    group_roster.ensure(msg.gid, self.link)
                except Exception:
                    LOG.exception('群名册刷新失败（不影响回复）')
            # 成员块的「其他人」附录：没在本轮发言的群友只报名字。
            # 否则她只认识最近 4 个说话的人，其他人一问就「不认识」。
            group_members = []
            if msg.is_group:
                try:
                    group_members = group_roster.known_members(
                        msg.gid, exclude=(str(self.self_id or ''),))
                except Exception:
                    LOG.exception('群友名录读取失败（不影响回复）')
            # 群体层：群聊时喂「群里最近」的低分辨率认知（氛围 + 无人称事件 + 成员模糊画像）
            group_scene = ('' if not msg.is_group
                           else group_scene_mod.to_inject(msg.gid))
            # 工具层：命中检索需求才联网，结果作为「事实参考」块注入
            tool_block = ''
            if not proactive:
                try:
                    tool_block = self._tool_block(msg)
                except Exception:
                    LOG.exception('工具调用失败（不影响对话）')
            # 她上一句说给谁：模型需要知道「刚才那句是对 A 说的，现在 B 插话了」。
            last_to = core.last_self_to(gs.turns) if msg.is_group else {}
            system = core.build_system(
                '' if proactive else msg.text, model_key=self.model, uid=msg.uid,
                identity=identity,
                speakers=gs.speaker_list(),
                mentions=([] if proactive else self._mentions_for(msg, gs)),
                group_members=group_members,
                group_scene=group_scene,
                group_note=msg.is_group,
                include_daily=True, include_rag=True,
                prev_reply=core.last_self_text(gs.turns),
                tool_block=tool_block,
                speaker='' if proactive else msg.nick,
                bubble_note=persona.bubble_limit() > 1,
                last_to=last_to.get('to', ''), last_to_ts=last_to.get('ts'),
                member_limit=limits['member_brief_limit'])
            if proactive:
                # 起话头：不加"当前这条消息"，只把最近对话给她
                if msg.is_group:
                    system += '\n\n' + cfg.GROUP_PROACTIVE_HINT
                    user_line = '（群里已经安静了一会儿）'
                else:
                    hint = private_topic.build_hint(
                        msg.uid, private_seed, bool(daily_scene.get_cached()))
                    if not hint:
                        LOG.info('私聊 %s 主动开口缺少素材，跳过', msg.uid)
                        private_topic.postpone(msg.uid, time.time(), 3600)
                        return
                    system += '\n\n' + hint
                    user_line = ('（对方已经有一阵没说话了；这是你自己想找他说话，'
                                 '不是回复他。）')
                messages = core.build_messages(system, turns=gs.turns,
                                               limit=limits['turn_limit'],
                                               max_chars=limits['msg_max_chars'],
                                               private=not msg.is_group)
                messages.append({'role': 'user', 'content': user_line})
            else:
                # 用 _turns_for_reply：turns 里已含当前这句（add_incoming 在 submit 前），
                # 不剔除会让模型读到重复的一问，进而回「你连发两次」。
                messages = core.build_messages(system, message=msg.text,
                                               nickname=msg.nick, message_ts=msg.ts,
                                               limit=limits['turn_limit'],
                                               max_chars=limits['msg_max_chars'],
                                               turns=_turns_for_reply(gs.turns, msg),
                                               private=not msg.is_group)
            options = core.build_options(expert=core.is_expert_topic(msg.text))
            try:
                # 群聊只发纯台词：（轻笑）这类短动作括注在清洗层硬兜底
                reply = core.generate(messages, model_key=self.model,
                                      options=options, group=msg.is_group,
                                      should_stop=job.cancel.is_set)
            except Exception as e:
                self.stats['errors'] += 1
                LOG.error('生成失败：%s', e)
                if not proactive:
                    self._send(msg, cfg.MODEL_CONNECT_FAIL_FORMAT % e)
                return
            # 生成期间被顶替：这段内容一个字都没发出去，直接丢弃重来。
            # API 是非流式，请求发出后停不下来，只能等它返回 —— 记一笔迟到取消，
            # 不把它算成"成功取消"。
            if job.cancel.is_set():
                if job.replaced_by is not None:
                    if self.kind in ('api', 'localapi'):
                        self.stats['cancel_late'] = self.stats.get('cancel_late', 0) + 1
                    LOG.info('  本轮作废，改为回应最新那条：%s',
                             (job.replaced_by.text or '')[:24])
                    return self._replacement(job)
                LOG.info('  本轮被取消，丢弃未发出的内容')
                return
            reply = (reply or '').strip()
            if not reply:
                LOG.warning('空回复，跳过：%r', msg.text[:30])
                return

            # 复读/幻觉拦截：① 沿用自己刚说过的模板；② 断言"对方发了几次"。
            # 2026-10-28 事故：她说过一次「连发两次……是怕我装作没看见吗」，
            # 这句在 assistant 历史里被自己继承，之后每句 @ 都套同一句（16 分钟 7 次），
            # 群友追问时还编出「刚才那条消息重复发送了两次」当理由。
            # 只重试一次，仍不合格就照发——宁可偶尔啰嗦，也不要为了不重复而不回应。
            if not proactive and (_repeats_self(reply, gs.turns) or _claims_resend(reply)):
                why = ('复读自己' if _repeats_self(reply, gs.turns) else '议论发送次数')
                retry_msgs = messages + [{'role': 'system', 'content': cfg.NO_REPEAT_HINT}]
                try:
                    alt = (core.generate(retry_msgs, model_key=self.model,
                                         options=options, group=msg.is_group) or '').strip()
                except Exception as e:
                    alt = ''
                    LOG.warning('复读重生成失败：%s', e)
                if alt and not (_repeats_self(alt, gs.turns) or _claims_resend(alt)):
                    LOG.info('  拦截（%s）：换了一种说法', why)
                    reply = alt
                else:
                    LOG.info('  拦截（%s）：重生成仍不合格，照原样发出', why)

            if proactive:
                # 自己起的话头也要过一遍终审：不像人话就不发
                judge = (self._proactive_judge() if msg.is_group
                         else self._private_proactive_judge())
                ok, why = judge(reply, dict(gs.ctx))
                if not ok:
                    LOG.info('  主动开口被终审否掉（%s）：%s', why,
                             reply.replace('\n', ' ')[:30])
                    if not msg.is_group:
                        private_topic.postpone(msg.uid, time.time(), 3600)
                    return

            # 正文版：记忆/上下文只留台词，不把 <@…> 内部标记写进去。
            if persona.active_id() != persona.current():
                LOG.info('  角色已切换，丢弃本轮未发出的回复')
                return
            display_reply = self._display_text(msg, reply)
            chunks = []
            for bubble in split_bubbles(reply, limit=persona.bubble_limit()):
                chunks.extend(split_reply(bubble))
            sent_chunks = []
            send_failed = False
            for i, chunk in enumerate(chunks):
                if job.cancel.is_set():
                    break
                if i and cfg.BRIDGE_BUBBLE_DELAY > 0:
                    time.sleep(cfg.BRIDGE_BUBBLE_DELAY)
                    if job.cancel.is_set():
                        break
                ok = self._send(msg, chunk)
                if not ok:
                    # 发送失败：后面的块不要再硬发（链接多半已断），
                    # 也不能把它们算成已发出——记忆只记真正落地的内容。
                    send_failed = True
                    break
                job.sent += 1
                sent_chunks.append(chunk)
            if not sent_chunks:
                if job.replaced_by is not None:
                    return self._replacement(job)
                return
            interrupted = bool(job.cancel.is_set()) or send_failed
            if interrupted:
                # 已经说出口的部分不能再收回：记忆里记的必须是实际发出去的那段，
                # 没发出去的块和语音一起停掉。
                self.stats['interrupted'] = self.stats.get('interrupted', 0) + 1
                reply = '\n'.join(sent_chunks)
                display_reply = self._display_text(msg, reply)
                if send_failed:
                    LOG.warning('  发送失败，只发出 %d/%d 块，剩余块与语音跳过',
                                len(sent_chunks), len(chunks))
                else:
                    LOG.info('  发出 %d/%d 块后被打断，剩余块与语音跳过',
                             len(sent_chunks), len(chunks))
            if msg.want_tts and not proactive and not interrupted:
                # 文字已经发出去了，再补一条语音（各一条，不合并）
                self._send_tts(msg, display_reply)
            self.stats['replies'] += 1
            now = time.time()
            gs.ctx['last_reply_ts'] = now
            gs.ctx['reply_ts_list'] = (gs.ctx.get('reply_ts_list') or [])[-20:] + [now]
            gs.ctx.setdefault('last_reply_by_uid', {})[msg.uid] = now
            gs.ctx['her_last_ts'] = now
            gs.ctx['her_last_text'] = display_reply
            gs.add_self(display_reply, now,
                        to=_reply_target(display_reply, msg, gs, proactive))
            LOG.info('  %s %d 字 / %.1fs：%s',
                     ('主动开口（私聊）' if not msg.is_group else '主动开口')
                     if proactive else '回复',
                     len(display_reply), time.time() - t0,
                     display_reply.replace('\n', ' ')[:40])

            if proactive:
                if not msg.is_group:
                    private_topic.mark_sent(msg.uid, display_reply, private_seed, now)
                    if private_seed and private_seed.get('curiosity_id') is not None:
                        try:
                            self_state.mark_asked(private_seed['curiosity_id'], now)
                        except Exception:
                            LOG.exception('好奇清单标记失败（不影响主动开口）')
                    self.stats['private_proactive'] = \
                        self.stats.get('private_proactive', 0) + 1
                return   # 她起的话头不是"某人说的话"，不该写进任何人的记忆
            # 记忆后处理：与回复同一个 worker 内串行，保证同一用户的事件顺序。
            # 用 judge_model（max）而不是说话模型：归纳要的是「抽事实、判边界」，
            # 角色扮演模型容易把事件摘要写成戏文，导致记忆卡漂移。
            try:
                info = core.post_turn(msg.uid, msg.text, display_reply,
                                      model_key=self.judge_model,
                                      learn_alias=bool(msg.is_group),
                                      is_group=bool(msg.is_group),
                                      speaker=(msg.nick if msg.is_group else ''),
                                      recent=(self._recent_group_turns(gs, msg.text)
                                              if msg.is_group else None),
                                      ats=(msg.ats if msg.is_group else None))
                LOG.debug('  记忆：%s', info)
                # 今天的安排：记一条事件日志；私聊里出现记忆事件时由判官调整日程。
                try:
                    self_state.note_turn(
                        msg.uid, msg.text, display_reply, info=info,
                        model_key=self.judge_model,
                        speaker=(msg.nick if msg.is_group else ''),
                        is_group=bool(msg.is_group))
                except Exception:
                    LOG.exception('自我状态记录失败（不影响记忆）')
                if info.get('rank_up'):
                    try:
                        self._send_rank_up(msg, info.get('relationship_after'))
                    except Exception:
                        LOG.exception('关系跃升提醒发送失败（不影响记忆）')
                if info.get('call_name'):
                    LOG.info('  记下称呼：%s → 「%s」', info.get('uid'), info['call_name'])
                if info.get('call_name_rejected'):
                    rj = info['call_name_rejected']
                    LOG.info('  拒绝称呼：%s 想记「%s」，但这个名字属于 %s',
                             info.get('uid'), rj.get('name'), rj.get('owner'))
                # 气泡模型晋升：归纳后判定「是否够格脱离群聊气泡」（仅群聊，已晋升幂等）
                if msg.is_group:
                    try:
                        pr = core.maybe_promote(msg.uid, judge_model=self.judge_model)
                        if pr.get('promoted'):
                            LOG.info('  晋升人物气泡：%s（%s）', msg.uid, pr.get('why'))
                        elif pr.get('checked'):
                            LOG.debug('  晋升判定：否（%s）', pr.get('why'))
                    except Exception:
                        LOG.exception('晋升判定失败（不影响记忆）')
            except Exception:
                self.stats['errors'] += 1
                self.stats['memory_errors'] += 1
                LOG.exception('记忆更新失败（不影响已发出的回复）')

    # ---- 主动开话题 ----
    def tick_proactive(self, now=None):
        """定时调用：群冷场 + 私聊空闲，两条线各自判断。"""
        now = now or time.time()
        self._check_persona_switch()
        self._tick_group_proactive(now)
        self._tick_private_proactive(now)

    def _tick_group_proactive(self, now):
        self._sync_models()
        if not rc.get('group_enabled', getattr(cfg, 'QQ_GROUP_ENABLED', True)):
            return   # 群聊已关闭：不接管群消息，也不主动开口
        for gid, gs in list(self.groups.items()):
            if gid.startswith('private:') or not rc.group_on(gid):
                continue   # 这个群单独关着：不主动开口
            day = datetime.date.today().isoformat()
            if gs.ctx.get('proactive_day') != day:
                gs.ctx['proactive_day'] = day
                gs.ctx['proactive_today'] = 0
            gs.ctx['now'] = now
            gs.ctx['gid'] = gid
            if not gs.turns:
                continue
            ok, why = gate.proactive_due(gs.ctx)
            if not ok:
                LOG.debug('群 %s 不主动：%s', gid, why)
                continue
            gs.ctx['last_proactive_ts'] = now
            gs.ctx['proactive_today'] = int(gs.ctx.get('proactive_today') or 0) + 1
            # 用最近一位发言者作为"记忆视角"：她对他有印象，说话才不空
            uid, nick = (gs.speakers[0] if gs.speakers else (self.self_id or '0', '群友'))
            job = Job(Msg(uid=uid, nick=nick, gid=gid, text='', ts=now,
                          mid='proactive-%d' % int(now), is_group=True),
                      kind='proactive')
            gs.submit(job)
            LOG.info('群 %s 触发主动开口（%s）', gid, why)

    def _tick_private_proactive(self, now):
        """私聊主动：只对白名单里的对象生效。白名单为空时绝不主动。"""
        self._sync_models()
        if not private_topic.is_enabled():
            return
        allow = [str(x).strip() for x in (rc.get('private_allow', []) or [])
                 if str(x).strip()]
        if not allow:
            # 留空 = 任何人可以私聊；主动开口不适用于陌生人
            return
        for uid in allow:
            ok, why = private_topic.due(uid, now)
            if not ok:
                LOG.debug('私聊 %s 不主动：%s', uid, why)
                continue
            self._trigger_private_proactive(uid, now)

    def _private_proactive_material(self, uid):
        seed = None
        try:
            seed = private_topic.pick_seed(uid, core.snapshot(uid))
        except Exception:
            LOG.exception('私聊主动素材挑选失败')
        return seed, daily_scene.get_cached()

    def _trigger_private_proactive(self, uid, now, manual=False):
        """把一次私聊主动任务投进 worker 队列。返回是否已入队。"""
        if not private_topic.is_enabled():
            return False
        gs = self.group_state('private:%s' % uid)
        if gs.inflight is not None or gs.queue.qsize() > 0:
            return False
        seed, daily = self._private_proactive_material(uid)
        if not seed and not daily:
            private_topic.postpone(uid, now, 3600)
            LOG.info('私聊 %s 主动开口缺少素材，推迟 1 小时', uid)
            return False
        private_topic.mark_attempt(uid, now)
        job = Job(Msg(uid=uid, nick='', gid='', text='', ts=now,
                      mid='private-proactive-%d' % int(now), is_group=False),
                  kind='proactive')
        ok = gs.submit(job)
        if ok:
            LOG.info('私聊 %s 触发主动开口（%s）', uid,
                     'manual' if manual else 'tick')
        return ok


# ---------- WS 传输 ----------

# ---------- selftest（无 QQ / 无 Ollama / 无 websockets） ----------

class FakeLink(object):
    def __init__(self):
        self.sent = []
        self.ats = []       # 与 sent 同步记录每条群消息里的 at 目标

    def send_group_msg(self, gid, text, reply_to=None, record=None, image=None,
                       at_lookup=None, allowed_ats=None):
        segs = _segments(text, reply_to, record, image,
                         at_lookup=at_lookup, allowed_ats=allowed_ats)
        ats = [s['data']['qq'] for s in segs if s.get('type') == 'at']
        self.sent.append(('group', gid, text, record, image))
        self.ats.append((gid, ats))

    def send_private_msg(self, uid, text, record=None, image=None):
        self.sent.append(('private', uid, text, record, image))

    def call_api(self, action, params=None, timeout=8):
        """替身：假装 NapCat 在线，供 /diag 用。"""
        if action == 'get_status':
            return {'stat': {'network_type': 'fake'}}
        if action == 'get_login_info':
            return {'nickname': 'fake', 'user_id': '0'}
        return {}

    def call_api_raw(self, action, params=None, timeout=8):
        return {'status': 'ok', 'retcode': 0,
                'data': self.call_api(action, params, timeout=timeout)}


def _write_selftest_persona(root):
    """自测专用角色卡：仓库不带任何真实角色数据，这里现写一份最小可用卡。"""
    folder = os.path.join(root, persona.DEFAULT_ID)
    os.makedirs(folder, exist_ok=True)
    card = {
        'version': 1,
        'character': 'Demo',
        'preamble': 'You are a generic test companion. Keep replies short.',
        'static': {
            'Identity': 'A generic companion used only by the offline self-test.',
            'Personality': 'Calm, curious, and easy to talk to.',
            'Speech': 'Plain conversational sentences, no catchphrases.',
            'Behavior': 'Answer what is asked; ask at most one follow-up question.',
            'Rule': 'Never invent facts about the person you are talking to.',
        },
        'relationship': {
            'header': '[Relationship]',
            'group_note': 'In a group, speak to the person being addressed.',
            'stats': [
                {'key': 'familiarity', 'label': 'Familiarity', 'tiers': [
                    {'id': 'F1', 'min': 0, 'speech': 'slightly formal',
                     'behavior': 'assume no shared history'},
                    {'id': 'F2', 'min': 20, 'speech': 'use their name',
                     'behavior': 'may recall one past exchange'},
                    {'id': 'F3', 'min': 40, 'speech': 'relaxed',
                     'behavior': 'shared references are natural'},
                ]},
                {'key': 'affection', 'label': 'Affection', 'tiers': [
                    {'id': 'A1', 'min': 0, 'speech': 'polite',
                     'behavior': 'keep a comfortable distance'},
                    {'id': 'A2', 'min': 30, 'speech': 'warmer',
                     'behavior': 'show mild personal interest'},
                    {'id': 'A3', 'min': 60, 'speech': 'open',
                     'behavior': 'offer honest reactions'},
                ]},
                {'key': 'trust', 'label': 'Trust', 'tiers': [
                    {'id': 'T1', 'min': 0, 'speech': 'guarded',
                     'behavior': 'avoid personal topics'},
                    {'id': 'T2', 'min': 30, 'speech': 'candid',
                     'behavior': 'share small opinions'},
                    {'id': 'T3', 'min': 60, 'speech': 'frank',
                     'behavior': 'say what you actually think'},
                ]},
            ],
            'bridges': [
                {'when': {'affection': [3], 'trust': [1]}, 'text':
                 'You enjoy the company but are not ready to open up.'},
            ],
        },
        'lore': {'header': '[Lore]', 'note': '', 'entries': []},
        'outfit': {
            'line': "Today's outfit: {outfit}",
            'season_band': {str(m): 'mild' for m in range(1, 13)},
            'rain_words': ['rain'], 'snow_words': ['snow'],
            'umbrella': 'carrying an umbrella',
            'bands': [{
                'id': 'mild', 'below': None,
                'require_any': [], 'forbid': [],
                'fallback': ['a plain shirt and trousers'],
            }],
        },
    }
    path = os.path.join(folder, 'card.json')
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    with open(os.path.join(folder, 'system_en.txt'), 'w',
              encoding='utf-8', newline='\n') as f:
        f.write('You are a generic test companion named Demo. '
                'Keep replies short and natural.\n')
    # 可选资产：自测也要覆盖 /cg /tts 两条指令链路，所以写最小可用的侧车。
    cg = {
        'name_zh': '示例',
        'city_zh': '示例市',
        'eye_lock_zh': '双眼同为深棕色',
        'identity_zh': '一位普通人类，外貌与年龄描述保持中性',
        'identity_zh_back': '同一位普通人类，背面同发型同服装',
        'back_focus_zh': '背影',
        'back_subject_zh': '背对镜头',
        'outfits_zh': {s: '白色衬衫配深色长裤' for s in ('相识', '熟络', '亲密', '倾心')},
    }
    with open(os.path.join(folder, 'cg.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(cg, f, ensure_ascii=False, indent=2)
    with open(os.path.join(folder, 'tts.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump({'enabled': True}, f, ensure_ascii=False, indent=2)
    return root


def selftest():
    import shutil
    import tempfile
    import selftest_media
    from sim_group import fake_gen_llm

    # 自测钉死 Character，并把角色运行态也放进临时根目录，绝不写真实 state。
    persona.active_id = lambda: persona.DEFAULT_ID

    root = tempfile.mkdtemp(prefix='bridgetest_')
    users = os.path.join(root, 'users')
    os.makedirs(users, exist_ok=True)
    orig_personas_state = persona.PERSONAS_STATE_ROOT
    persona.PERSONAS_STATE_ROOT = os.path.join(root, 'personas')
    orig_personas_root = persona.PERSONAS_ROOT
    persona.PERSONAS_ROOT = _write_selftest_persona(
        os.path.join(root, 'personas_src'))
    core.USERS_ROOT = users
    core.STATE_ROOT = root
    member_card.set_users_root(users)
    activity.set_root(os.path.join(root, 'groups'))
    group_roster.set_root(os.path.join(root, 'groups'))
    group_scene_mod.set_root(os.path.join(root, 'groups'))
    mem._gen_llm = fake_gen_llm
    private_topic.set_path(os.path.join(root, 'private_topic.json'))
    selftest_media.setup(root)

    # 自测用的是虚构 uid，不能被本机私聊白名单挡住（白名单是运行配置，不是逻辑）
    try:
        import runtime_config as _rc
        _rc._DATA['private_allow'] = []
    except Exception:
        pass

    args = argparse.Namespace(group=[], admin=[], model='qwen3.5', dry_run=False,
                              no_judge=True, state_root=root, decision_log=None)
    br = Bridge(args)
    br.self_id = '999999'
    link = FakeLink()
    br.set_link(link)

    orig_generate = core.generate
    core.generate = (lambda messages, model_key=None, options=None, group=False,
                     should_stop=None: '（自测回复）')
    fast_gen = core.generate       # 各段测试之间还原用：不能还原成真实模型调用
    # 自检不能依赖本地运行时配置。群聊总开关 / 主动开口（GUI 里都能关）一旦是
    # false，消息会在 on_event 被静默丢弃、冷场也不会主动说话 —— 自检从第 1 步
    # 开始全挂，看起来像代码坏了，实际只是本机把开关关了。只在进程内掰开，不写盘。
    orig_rc_get = rc.get
    _force_on = {'group_enabled', 'proactive_enabled', 'private_proactive_enabled'}
    rc.get = (lambda key, default=None: True if key in _force_on
              else orig_rc_get(key, default))
    results = []

    def check(name, ok, detail=''):
        results.append((name, bool(ok)))
        print('%s %s%s' % ('[PASS]' if ok else '[FAIL]', name,
                           ('  → ' + str(detail)[:100]) if detail else ''))

    def group_ev(uid, nick, text, mid, at=False):
        segs = []
        if at:
            segs.append({'type': 'at', 'data': {'qq': '999999'}})
        segs.append({'type': 'text', 'data': {'text': text}})
        return {'post_type': 'message', 'message_type': 'group', 'self_id': 999999,
                'group_id': 123456, 'user_id': uid, 'message_id': mid,
                'time': time.time(),
                'sender': {'user_id': uid, 'nickname': nick, 'card': nick},
                'message': segs}

    def drain():
        for gs in list(br.groups.values()):
            gs.queue.join()
        time.sleep(0.05)

    def user_file(uid, name):
        return os.path.join(core.user_dir(uid), name)

    def read_json_file(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)

    try:
        # 1 @ 必回
        br.on_event(group_ev(1001, '群友A', ' 在吗', 101, at=True))
        drain()
        check('1 被 @ → 发出回复', len(link.sent) == 1, link.sent)

        # 2 无人 @ 的闲聊 → 沉默
        before = len(link.sent)
        br.on_event(group_ev(1002, '群友C', '今天好累', 102))
        drain()
        check('2 无人 @ 的闲聊 → 不发言', len(link.sent) == before, link.sent[before:])

        # 3 同一条消息重投 → 去重
        before = len(link.sent)
        br.on_event(group_ev(1001, '群友A', ' 在吗', 101, at=True))
        drain()
        check('3 重投同一 message_id → 去重', len(link.sent) == before, br.stats['dup'])

        # 4 自消息回流 → 忽略
        before = len(link.sent)
        br.on_event(group_ev(999999, '角色', '我自己说的话', 104))
        drain()
        check('4 自消息回流 → 忽略', len(link.sent) == before)

        # 5 纯图片（无 url）→ 不崩、不进上下文
        before_turns = len(br.groups['123456'].turns)
        br.on_event({'post_type': 'message', 'message_type': 'group',
                     'self_id': 999999, 'group_id': 123456, 'user_id': 1003,
                     'message_id': 105, 'time': time.time(),
                     'sender': {'nickname': '群友B'},
                     'message': [{'type': 'image', 'data': {'file': 'a.jpg'}}]})
        drain()
        check('5 纯图片消息(无url) → 不崩且不进上下文',
              len(br.groups['123456'].turns) == before_turns)

        # 5b 带 url 的图片 → 识别出内容并触发回复（mock 视觉，不真调 API）
        orig_desc = media_understand.describe_image
        media_understand.describe_image = lambda u, timeout=None: '一只猫'
        try:
            before = len(link.sent)
            br.on_event({'post_type': 'message', 'message_type': 'group',
                         'self_id': 999999, 'group_id': 123456, 'user_id': 1003,
                         'message_id': 110, 'time': time.time(),
                         'sender': {'nickname': '群友B'},
                         'message': [{'type': 'image', 'data': {'url': 'https://x/a.jpg'}},
                                     {'type': 'text', 'data': {'text': '看这个'}}]})
            drain()
            check('5b 带url图片 → 识别并注入', len(link.sent) == before, link.sent[before:])
        finally:
            media_understand.describe_image = orig_desc

        # 5c 带链接的消息 → 抓取摘要（mock 抓取，不真访问网络）
        orig_fetch = media_understand.fetch_link
        media_understand.fetch_link = lambda u, timeout=8: '测试网页标题'
        try:
            before = len(link.sent)
            br.on_event(group_ev(1001, '群友A', '看看这个 https://example.com/x', 111, at=True))
            drain()
            # 链接抓取可能因无真实网络而失败，但至少不崩；不强制断言回复
            check('5c 带链接消息 → 不崩', True)
        finally:
            media_understand.fetch_link = orig_fetch

        # 6 指令：/relation
        before = len(link.sent)
        br.on_event(group_ev(1001, '群友A', '/relation', 106))
        drain()
        check('6 /relation 有回复', len(link.sent) == before + 1, link.sent[before:])

        selftest_media.run_cg(br, link, args, root, Msg, group_ev, drain, check)

        # 6c/6d 关系跃升提醒：群里 @ 对方，私聊就在私聊发
        before = len(link.sent)
        br._send_rank_up(Msg(1001, '群友A', '123456', 'x', 'm'), '熟络')
        got = link.sent[before:]
        check('6c 群聊跃升 → 群内 @ 本人',
              len(got) == 1 and got[0][0] == 'group'
              and link.ats[-1][1] == [1001], got)
        before = len(link.sent)
        br._send_rank_up(Msg(1009, '私聊的人', '', 'x', 'm', is_group=False), '亲密')
        got = link.sent[before:]
        check('6d 私聊跃升 → 私聊提醒',
              len(got) == 1 and got[0][0] == 'private', got)

        # 7 私聊 → 直接回
        before = len(link.sent)
        br.on_event({'post_type': 'message', 'message_type': 'private',
                     'self_id': 999999, 'user_id': 1009, 'message_id': 107,
                     'time': time.time(), 'sender': {'nickname': '私聊的人'},
                     'message': [{'type': 'text', 'data': {'text': '你好'}}]})
        drain()
        got = link.sent[before:]
        check('7 私聊消息 → 直接回', len(got) == 1 and got[0][0] == 'private', got)

        # 8 长回复切分
        chunks = split_reply('这是一句话。' * 200)
        check('8 长回复按句切分且不超限',
              len(chunks) > 1 and all(len(c) <= cfg.BRIDGE_MAX_MSG_CHARS for c in chunks),
              [len(c) for c in chunks])

        # 9 队列满丢旧任务（用独立 GroupState：不能把测试任务塞进真队列，
        #    否则常驻 worker 会把它取走执行，后面的用例时序全乱）
        gq = GroupState('queue-test', maxsize=1)
        ok1 = gq.submit(Job(Msg(1, 'a', 'queue-test', 'x', 'm1')))
        ok2 = gq.submit(Job(Msg(2, 'b', 'queue-test', 'y', 'm2')))
        check('9 队列满时丢旧保新', ok1 and ok2 and gq.queue.qsize() == 1)

        # 9b~9e 事件投递语义：纠正可以顶替**尚未发话**的那一轮；已发话只打断
        calls = {'n': 0}

        def _slow_gen(messages, model_key=None, options=None, group=False,
                      should_stop=None):
            calls['n'] += 1
            if calls['n'] == 1:
                for _ in range(300):
                    if should_stop is not None and should_stop():
                        return '（半截）'
                    time.sleep(0.01)
                return '（慢回复）'
            return '这次说清楚了吗'

        core.generate = _slow_gen
        before = len(link.sent)
        pre_before = br.stats.get('preempted', 0)
        gs = br.groups['123456']
        br.on_event(group_ev(1001, '群友A', '在不在', 301, at=True))
        # 等 worker 真正接手这一轮（构 system 要一两秒，固定 sleep 不可靠）；
        # 纠正消息必须在 inflight 已出现、且还没发出任何块的窗口内到达。
        deadline = time.time() + 5
        while time.time() < deadline and gs.inflight is None:
            time.sleep(0.005)
        br.on_event(group_ev(1001, '群友A', '不对，我是说明天', 302, at=True))
        drain()
        got = link.sent[before:]
        check('9b 纠正顶替未发话的那轮 → 只发一条',
              len(got) == 1 and got[0][2] == '这次说清楚了吗', got)
        check('9c 顶替计数 +1',
              br.stats.get('preempted', 0) == pre_before + 1, br.stats)
        core.generate = fast_gen

        n2 = {'n': 0}

        def _slow_gen2(messages, model_key=None, options=None, group=False,
                       should_stop=None):
            n2['n'] += 1
            time.sleep(0.3)
            return '第%d条' % n2['n']

        core.generate = _slow_gen2
        before = len(link.sent)
        br.on_event(group_ev(1004, '小王', '有空吗', 303, at=True))
        deadline = time.time() + 5
        while time.time() < deadline and br.groups['123456'].inflight is None:
            time.sleep(0.01)
        br.on_event(group_ev(1005, '小李', '忙不忙', 304, at=True))
        drain()
        got = link.sent[before:]
        check('9d 不同人插话不顶替 → 两条都回', len(got) == 2, got)
        core.generate = fast_gen

        gsx = br.group_state('555')
        jx = Job(Msg(1, 'a', '555', 'x', 'mx'))
        jx.sent = 1                      # 已经发出去一块
        with gsx.lock:
            gsx.inflight = jx
        int_before = br.stats.get('interrupted', 0)
        hit = br._try_interrupt(gsx, Msg(1, 'a', '555', '不对', 'my'))
        with gsx.lock:
            gsx.inflight = None
        check('9e 已开口后只打断、不整轮重跑',
              hit and jx.cancel.is_set()
              and br.stats.get('interrupted', 0) == int_before + 1)

        # 10 状态指令权限
        args.admin = ['1001']
        reply_admin = br._status(Msg('1001', 'x', '123456', '', '1'))
        check('10 /status 输出队列与活跃画像', '队列' in reply_admin and '模型' in reply_admin,
              reply_admin[:60])

        # 11 冷场主动开口：不发 reply 段，且不写任何人的记忆
        pg = br.group_state('777')
        now = time.time()
        pg.turns.append({'nickname': '群友C', 'text': '那我先走了', 'ts': now - 900})
        pg.ctx['turns'] = list(pg.turns)
        pg.ctx['active_hours'] = set(range(0, 24))
        pg.ctx['last_proactive_ts'] = 0
        pg.ctx['proactive_today'] = 0
        pg.speakers.appendleft(('1010', '群友C'))
        before = len(link.sent)
        br.tick_proactive(now)
        drain()
        got = link.sent[before:]
        check('11 冷场 → 主动开口一次', len(got) == 1 and got[0][1] == '777', got)
        no_mem = True
        if os.path.exists(user_file('1010', 'memory_card.json')):
            no_mem = not (read_json_file(user_file('1010', 'memory_card.json'))
                          .get('episodes') or [])
        check('12 主动开口不写进任何人的记忆', no_mem)

        # 29~32 今日日常：跨天刷新、拒收无效输出、最近场景去重
        ds_path = os.path.join(root, 'daily_scene.json')
        daily_scene.set_path(ds_path)
        old_ds_gen = daily_scene._generate
        ds_calls = []

        def fake_ds(recent=None, place_type=None):
            ds_calls.append(list(recent or []))
            return ('示例市的清晨带着一点凉意，你在旧货市场挑到一张老唱片，'
                    '摊主说是今早刚收来的。')

        daily_scene._generate = fake_ds
        daily_scene._fail_until = 0.0
        yesterday = (datetime.date.today()
                     - datetime.timedelta(days=1)).isoformat()
        with open(ds_path, 'w', encoding='utf-8') as f:
            json.dump({'date': yesterday,
                       'text': '昨天的场景是书店，翻了一下午旧书。',
                       'recent': [{'date': yesterday,
                                   'text': '昨天的场景是书店，翻了一下午旧书。'}]},
                      f, ensure_ascii=False)
        got_ds = daily_scene.ensure_today()
        check('29 今日日常跨天会重新生成',
              bool(got_ds) and daily_scene.get_cached() == got_ds, got_ds)
        check('30 昨天场景进过去重提示',
              bool(ds_calls) and any('书店' in t for t in ds_calls[-1]),
              ds_calls[-1])
        with open(ds_path, encoding='utf-8') as f:
            ds_cache = json.load(f)
        check('30b 缓存保留最近场景',
              ds_cache.get('date') == datetime.date.today().isoformat()
              and len(ds_cache.get('recent') or []) >= 2, ds_cache)
        legacy = daily_scene._recent_texts(
            {'date': yesterday, 'text': '昨天的场景是书店。'})
        check('30c 老缓存没有 recent 也参与去重',
              legacy == ['昨天的场景是书店。'], legacy)

        daily_scene._fail_until = 0.0
        with open(ds_path, 'w', encoding='utf-8') as f:
            json.dump({'date': yesterday, 'text': '旧场景'}, f, ensure_ascii=False)
        daily_scene._generate = (lambda recent=None, place_type=None:
                                 '由于您未提供具体的今日天气实况数据，请补充后我再生成。')
        got_bad = daily_scene.ensure_today()
        check('31 拒绝文本不写进今日日常',
              got_bad == '' and daily_scene.get_cached() == '', got_bad)

        daily_scene._fail_until = 0.0
        daily_scene._generate = (lambda recent=None, place_type=None:
                                 '示例市傍晚的河边起了点风，你蹲下来系紧松开的鞋带，'
                                 '顺手把掉在石阶上的银杏叶夹进了书里。')
        daily_scene.refresh_async()
        deadline = time.time() + 3
        while time.time() < deadline and not daily_scene.get_cached():
            time.sleep(0.05)
        check('32 ticker 后台跨天刷新',
              bool(daily_scene.get_cached()), daily_scene.get_cached())
        daily_scene._generate = old_ds_gen

        # 23~28 私聊主动：白名单、待回复不追发、不写记忆、/topic
        if True:
            _rc._DATA['private_allow'] = ['1008']
            _rc._DATA.update({
                'private_proactive_idle_min': 600,
                'private_proactive_idle_max': 600,
                'private_proactive_min_gap': 0,
                'private_proactive_daily_max': 1,
                'private_proactive_hour_start': 0,
                'private_proactive_hour_end': 24,
                'private_proactive_recent_days': 3,
            })
            uid_p = '1008'
            with mem.state_scope(core.user_dir(uid_p)):
                md = mem.load()
                mem.add_episode(md, {'summary': '上次约好周末去旧书店',
                                     'importance': 2, 'time': '2026-10-31 21:00'})
                mem.add_episode(md, {'summary': '聊到她喜欢的咖啡豆',
                                     'importance': 1, 'time': '2026-11-01 09:00'})
                mem.save(md)
            with mem.state_scope(core.user_dir(uid_p)):
                count_before = mem.load()['interaction_count']
            pnow = time.time()
            private_topic.note_incoming(uid_p, pnow - 7200, next_due=pnow - 1)
            before = len(link.sent)
            br.tick_proactive(pnow)
            drain()
            got = link.sent[before:]
            check('23 私聊空闲到期 → 主动发一条',
                  len(got) == 1 and got[0][0] == 'private' and got[0][1] == uid_p,
                  got)
            st = private_topic.get_state(uid_p) or {}
            check('24 私聊主动后进入待回复状态', st.get('pending') is True, st)
            with mem.state_scope(core.user_dir(uid_p)):
                count_after = mem.load()['interaction_count']
            check('25 私聊主动不写记忆', count_after == count_before,
                  (count_before, count_after))
            before = len(link.sent)
            br.tick_proactive(pnow + 10)
            drain()
            check('26 未回复不追发', len(link.sent) == before, link.sent[before:])
            _rc._DATA['private_allow'] = []
            private_topic.note_incoming(uid_p, pnow - 7200, next_due=pnow - 1)
            before = len(link.sent)
            br.tick_proactive(pnow + 20)
            drain()
            check('27 白名单为空 → 私聊不主动', len(link.sent) == before,
                  link.sent[before:])
            # 27b 空白名单时 /topic 也不能绕过白名单去私戳陌生人
            args.admin = ['1001', uid_p]
            before = len(link.sent)
            br.handle_command(Msg(uid=uid_p, nick='x', gid='', text='/topic',
                                  mid='topic-0', is_group=False))
            drain()
            got = link.sent[before:]
            check('27b 白名单为空 → /topic 只回提示',
                  len(got) == 1 and '白名单' in (got[0][2] or ''), got)
            # /topic 手动触发（绕开时间判定，但要求开关打开）
            _rc._DATA['private_allow'] = [uid_p]
            args.admin = ['1001', uid_p]
            before = len(link.sent)
            ret = br.handle_command(Msg(uid=uid_p, nick='x', gid='', text='/topic',
                                        mid='topic-1', is_group=False))
            drain()
            got = link.sent[before:]
            check('28 /topic 手动生成一条私聊主动',
                  ret is True and len(got) == 1 and got[0][0] == 'private', got)
            _rc._DATA['private_allow'] = []

        selftest_media.run_tts(br, link, check, Msg, drain)

        # 17 record 消息段结构（NapCat 靠它把 wav 转成 silk 发语音条）
        segs = _segments('', None, os.path.join(root, 'a.wav'))
        check('17 record 消息段结构正确',
              segs == [{'type': 'record',
                        'data': {'file': os.path.join(root, 'a.wav')}}], segs)

        selftest_media.run_text(br, check)

        # 33 `/ <内容>` OOC 旁注：只给作者看，上下文/门控/记忆/回复都听不到
        ooc_gs = br.group_state('123456')
        ooc_turns_before = len(ooc_gs.turns)
        ooc_sent_before = len(link.sent)
        br.on_event(group_ev(1011, '群友C',
                             '/ 后台好感 30、信任 30、熟悉 0，别让她知道',
                             401, at=True))
        drain()
        check('33 / <内容> 群聊：不回、不进上下文',
              len(link.sent) == ooc_sent_before
              and len(ooc_gs.turns) == ooc_turns_before
              and all('后台好感' not in (t.get('text') or '')
                      for t in ooc_gs.turns),
              (link.sent[ooc_sent_before:], list(ooc_gs.turns)[-2:]))
        check('33b OOC 不写记忆',
              not os.path.exists(user_file('1011', 'memory_card.json')))

        ooc_p_before = len(link.sent)
        br.on_event({'post_type': 'message', 'message_type': 'private',
                     'self_id': 999999, 'user_id': '1011', 'message_id': 'ooc-p',
                     'time': time.time(), 'sender': {'nickname': '群友C'},
                     'message': [{'type': 'text',
                                  'data': {'text': '/ 这条也不该回'}}]})
        drain()
        check('33c / <内容> 私聊也不回', len(link.sent) == ooc_p_before,
              link.sent[ooc_p_before:])

        # 13/14 人物卡：只有登记过的 QQ 才注入【用户设定】，别人说话时不能带上
        captured = []

        def _capture(messages, model_key=None, options=None, group=False,
                     should_stop=None):
            captured.append(messages[0]['content'])
            return '（自测回复）'

        core.generate = _capture
        br.on_event(group_ev(10000001, '示例昵称', '在吗', 108, at=True))
        drain()
        br.on_event(group_ev(1002, '群友C', '在吗', 109, at=True))
        drain()
        core.generate = orig_generate
        # 用「对方身份：」判定：角色卡正文里也出现了「【用户设定】」这个词，
        # 拿它当探针会被角色卡自身命中（测试假阳性/假阴性都踩过）
        check('13 登记过的人物卡会注入身份块',
              any('对方身份：' in c and '用户' in c for c in captured), len(captured))
        check('14 未登记的人说话不注入身份块',
              any('对方身份：' not in c for c in captured))

        # 18/19 记忆归属：@ 的 qq 号必须在解析后保留下来（2026-10-28）
        # 以前只把它拼成「@名字」就丢了 uid，于是「@某甲 这人喜欢猫」进记忆后
        # 无法确定"这人"是谁 —— 归属只能靠昵称猜，而群昵称是会改的。
        ev_at = group_ev(10000003, '示例角色', ' 这人喜欢猫', 200)
        ev_at['message'].insert(0, {'type': 'at',
                                    'data': {'qq': '10000002', 'name': '肌霸群友'}})
        m_at = parse_message(ev_at, self_id='999999')
        got = getattr(m_at, 'ats', None) or []
        check('18 @他人保留qq号（归属依据）',
              len(got) == 1 and got[0]['uid'] == '10000002'
              and got[0]['nickname'] == '肌霸群友', got)
        m_self = parse_message(group_ev(10000003, '示例角色', '在吗', 201, at=True),
                               self_id='999999')
        check('19 @她自己不进归属名单',
              m_self.at_self and not (getattr(m_self, 'ats', None) or []),
              getattr(m_self, 'ats', None))

        # 20~22 当前句不能在上下文里出现两遍（2026-10-28 实际事故）
        # handle_group 在 submit **之前**就 add_incoming(msg)，turns 末尾已含当前这句；
        # _run_job 又以 message=msg.text 追加一次 → 模型读到同一个人连着说两遍同一句，
        # 于是每句被 @ 的话都换来一句「你连发两次」，还会被下一轮 turns 继续喂回去。
        seen_msgs = []

        def _capture_msgs(messages, model_key=None, options=None, group=False,
                          should_stop=None):
            seen_msgs.append([dict(m) for m in messages])
            return '（自测回复）'

        core.generate = _capture_msgs
        br.on_event(group_ev(1002, '群友C', '在吗在吗', 201, at=True))
        drain()
        core.generate = orig_generate
        last = seen_msgs[-1] if seen_msgs else []
        body = [m for m in last
                if m.get('role') == 'user' and '在吗在吗' in (m.get('content') or '')]
        check('20 当前句在 messages 里只出现一次', len(body) == 1,
              [m.get('content') for m in last])

        turns_dup = [{'uid': '1', 'mid': 1, 'nickname': 'A', 'text': '一'},
                     {'uid': '2', 'mid': 2, 'nickname': 'B', 'text': '二'},
                     {'uid': '3', 'mid': 3, 'nickname': 'C', 'text': '三'}]

        class _M2(object):
            mid, text, nick = 2, '二', 'B'

        class _M3(object):
            mid, text, nick = None, '三', 'C'

        check('21 按 mid 精确剔除且保留更晚消息',
              [t['text'] for t in _turns_for_reply(turns_dup, _M2())] == ['一', '三'],
              [t['text'] for t in _turns_for_reply(turns_dup, _M2())])
        check('22 mid 缺失时按昵称+文本剔除末尾那条',
              [t['text'] for t in _turns_for_reply(turns_dup, _M3())] == ['一', '二'],
              [t['text'] for t in _turns_for_reply(turns_dup, _M3())])

        # 23/24 复读护栏：她刚说过的模板不能换个尾巴继续说
        self_turns = [
            {'self': True, 'text': '在呢。不过连发两次……是怕我装作没看见吗？', 'ts': 0},
            {'nickname': '群友C', 'text': '在吗', 'ts': 1},
        ]
        check('23 复读被识别（换半句尾巴也算）',
              _repeats_self('哎呀，连发两次……是怕我装作没听见吗？', self_turns))
        check('24 正常回应不误判',
              not _repeats_self('在呢。刚泡了杯红茶，正想歇会儿。', self_turns))
        # 25/26 断言"对方发了几次"属于她无权知道的事实（2026-10-28 编过理由）
        check('25 议论发送次数被识别',
              _claims_resend('哎呀，连发两次……是怕我装作没看见吗？')
              and _claims_resend('刚才那条消息重复发送了两次呀'))
        check('26 正常回应不误触发次数断言',
              not _claims_resend('在呢。刚泡了杯红茶，正想歇会儿。')
              and not _claims_resend('别刷屏了，一条一条说'))
    finally:
        core.generate = orig_generate
        rc.get = orig_rc_get
        persona.PERSONAS_STATE_ROOT = orig_personas_state
        persona.PERSONAS_ROOT = orig_personas_root
        shutil.rmtree(root, ignore_errors=True)

    failed = [n for n, ok in results if not ok]
    print('\n桥接自测：%d/%d 通过' % (len(results) - len(failed), len(results)))
    return 0 if not failed else 1


# ---------- 入口 ----------

def build_args(argv=None):
    ap = argparse.ArgumentParser(description='示例角色 QQ 接入（OneBot v11 反向 WS）')
    ap.add_argument('--host', default='127.0.0.1', help='监听地址（默认仅本机）')
    ap.add_argument('--port', type=int, default=paths.PORT_BRIDGE_WS,
                    help='反向 WS 端口（默认取 NAL_PORT_BRIDGE_WS，与总控台一致）')
    ap.add_argument('--path', default='/onebot/v11/ws', help='反向 WS 路径')
    ap.add_argument('--group', action='append', default=[],
                    help='只处理这些群号（可多次；留空则用 rp_config.QQ_GROUPS，仍为空=所有群）')
    ap.add_argument('--admin', action='append', default=[],
                    help='额外管理员 QQ 号（追加到 rp_config.QQ_ADMIN）')
    ap.add_argument('--self-id', default=cfg.QQ_SELF_ID,
                    help='bot 自己的 QQ（默认取 rp_config.QQ_SELF_ID）')
    ap.add_argument('--model', default=None, help='模型 key（默认取 rp_config 的默认项）')
    ap.add_argument('--judge-model', default=None, dest='judge_model',
                    help='L2 终审与记忆归纳用的模型 key（默认 QQ_JUDGE_MODEL=qwen-max）')
    ap.add_argument('--dry-run', action='store_true', help='只打印不发送（联调用）')
    ap.add_argument('--no-judge', action='store_true',
                    help='关闭 L2 模型终审（只靠 L1 阈值，省一次调用）')
    ap.add_argument('--tick', type=int, default=cfg.BRIDGE_PROACTIVE_TICK,
                    help='主动开话题的检查间隔（秒）')
    ap.add_argument('--log', default='INFO', help='日志级别')
    ap.add_argument('--admin-port', type=int, default=paths.PORT_ADMIN,
                    help='管理面板端口（默认取 NAL_PORT_ADMIN；0 = 关闭面板）')
    ap.add_argument('--no-browser', action='store_true',
                    help='启动后不自动打开管理面板浏览器页面')
    ap.add_argument('--selftest', action='store_true', help='离线自测（不连 QQ）')
    return ap.parse_args(argv)


def _open_admin_window_later(port, delay=0.3, token=''):
    """等管理面板后端就绪后，拉起独立桌面窗口（浏览器 --app 应用窗口）。

    用 subprocess 起 admin_window.py，它用 Chrome（优先）/Edge 的
    --app=URL 开独立窗口后立即返回；窗口关闭也不影响后端/bot。
    admin_window.py 自己会等端口就绪，故这里只需给后端线程一点启动
    时间（0.3s 即可）。
    （pywebview 的 WebView2 在本机报 Error 1411 起不来窗口；Edge --app
    窗口在本机进程起了但不可见，Chrome --app 窗口能正常显示，故优先 Chrome。）
    """
    time.sleep(delay)
    try:
        import subprocess
        py = sys.executable
        script = os.path.join(HERE, 'admin_window.py')
        cmd = [py, script, '--port', str(port)]
        if token:
            cmd += ['--token', token]
        subprocess.Popen(
            cmd,
            cwd=os.path.dirname(HERE),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except Exception:
        LOG.exception('拉起管理面板独立窗口失败（不影响面板本身）')


_SINGLETONS = []   # 命名互斥体句柄：进程活着就不能关，关了锁就失效


def _acquire_single_instance(name='NALQQBridge'):
    """拿进程级单实例锁；已有同名实例在跑则返回 False。

    为什么需要（2026-10-28 实测故障）：两个 qq_bridge 用**完全相同的参数、同一秒**
    启动，同一条群消息被处理两遍、回复两遍，上下文里也读出重音。
    根因是 launcher 的 `port_open(6700)` 探测有竞态——bot 从启动到监听要好几十秒，
    这期间再双击一次 start.bat 就会再起一个实例，而两边都认为"对方还没起来"。

    用命名互斥体（Local\\ 作用域）而不是端口/文件：进程被强杀时内核自动释放，
    不会留下"锁文件残留导致再也起不来"的坑。按 admin-port 区分名字，
    允许不同端口的实例并存（真要多开时必须显式改端口）。
    """
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (wintypes.LPCVOID, wintypes.BOOL,
                                     wintypes.LPCWSTR)
        handle = k32.CreateMutexW(None, False, 'Local\\' + name)
        if not handle:
            return True          # 拿不到句柄时不敢拦，宁可放行
        if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
            return False
        _SINGLETONS.append(handle)   # 故意不释放，随进程退出由内核回收
        return True
    except Exception:
        return True              # 非 Windows / ctypes 异常都不该阻断启动


def _state_lock_name(root=None):
    """同一 state/ 只能有一个 bot 实例——互斥体名由 state 根路径决定。

    光按 admin-port 加锁不够：两个实例换个端口就能同时写同一份记忆/场景，
    症状和"重复回复"不同（数值乱跳、记忆互相覆盖），更难查。
    路径先 normcase 再哈希，Windows 上大小写/斜杠差异视为同一个根。
    """
    root = root or core.STATE_ROOT
    key = os.path.normcase(os.path.abspath(root))
    digest = hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]
    return 'NALQQBridge-state-%s' % digest


def _start_heartbeat(bridge, interval=10.0):
    """后台写 state/bridge_heartbeat.json，供守护进程与面板判断存活。

    独立线程、独立文件：生成/记忆写盘再慢，也不会让心跳变陈旧。
    任何异常都静默吞掉——可观测性不能反过来把 bot 拖垮。
    """
    started = time.time()

    def loop():
        while True:
            try:
                link = getattr(bridge, 'link', None)
                payload = {
                    'ts': time.time(),
                    'pid': os.getpid(),
                    'started_at': started,
                    'uptime_sec': int(time.time() - started),
                    'model': bridge.model,
                    'judge_model': bridge.judge_model,
                    'self_id': bridge.self_id,
                    'napcat_connected': bool(getattr(link, 'connected', False)),
                    'stats': dict(bridge.stats),
                    'groups': {str(gid): {'queue': gs.queue.qsize(),
                                          'dropped': gs.dropped}
                               for gid, gs in (bridge.groups or {}).items()},
                }
                heartbeat.write(heartbeat.BRIDGE_PATH, payload)
            except Exception:
                pass
            time.sleep(interval)

    threading.Thread(target=loop, name='heartbeat', daemon=True).start()


def main(argv=None):
    args = build_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log).upper(), logging.INFO),
                        format='%(asctime)s %(levelname)s %(message)s',
                        datefmt='%H:%M:%S')
    # supervisor（每 10s）和面板状态接口都用裸 TCP 探一次 6700，只为看端口在不在听；
    # 这类连接不握手，websockets 每探一次就记一条 INFO「connection closed」，
    # 把真正有用的日志冲走。NapCat 的上下线由 bridge_transport 自己记
    # （「NapCat 已连接」/「NapCat 连接断开」），这里只关掉库的握手流水账。
    logging.getLogger('websockets.server').setLevel(logging.WARNING)

    # 额外写一份文件日志：bot 跑在独立的 console 窗口里，
    # 窗口被关/被最小化后就看不到输出了，排查「没反应」时是瞎的。
    try:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(log_dir, 'qq_bridge.log'),
                                 maxBytes=10 * 1024 * 1024, backupCount=5,
                                 encoding='utf-8')
        fh.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s %(name)s %(message)s', '%H:%M:%S'))
        logging.getLogger().addHandler(fh)
    except Exception:
        pass   # 写不了日志文件不影响 bot 运行

    if args.selftest:
        return selftest()

    lifecycle.configure('bridge', lifecycle.BRIDGE_STAGES)
    lifecycle.attach_logs()

    # 单实例：重复实例会把同一条群消息处理两遍、回复两遍（2026-10-28 实测）
    # 放在 selftest 之后——连跑自测不该被"已有实例"挡住。
    lifecycle.start('single_instance')
    if not _acquire_single_instance('NALQQBridge-%s' % (args.admin_port or 6780)):
        lifecycle.finish('single_instance', 'fail', '已有实例在运行',
                         '关闭旧实例，或换一个 --admin-port')
        LOG.error('已有一个 bot 实例在运行（admin-port %s），本进程退出；'
                  '确实要多开请换 --admin-port', args.admin_port)
        return 2
    # 端口锁之外再按 state 根加一道：换端口多开也不能共用同一份运行态
    if not _acquire_single_instance(_state_lock_name()):
        lifecycle.finish('single_instance', 'fail', '同一 state 已有实例',
                         '关闭正在使用 %s 的实例' % core.STATE_ROOT)
        LOG.error('已有一个 bot 实例在使用同一状态目录（%s），本进程退出；'
                  '状态目录只能由一个实例写', core.STATE_ROOT)
        return 2
    lifecycle.finish('single_instance', 'ok', '已取得单实例锁')

    # 命令行没给就用配置里的（避免每次双击都要带参数）
    if not args.group:
        args.group = list(getattr(cfg, 'QQ_GROUPS', None) or [])
    # 配置名单是基线，--admin 只做追加：启动脚本始终会带一个号，
    # 若按「命令行覆盖配置」处理，QQ_ADMIN 里新增的人永远不会生效。
    args.admin = list(dict.fromkeys(
        list(getattr(cfg, 'QQ_ADMIN', None) or []) + list(args.admin or [])))

    args.state_root = core.STATE_ROOT
    args.decision_log = os.path.join(core.STATE_ROOT, 'groups', 'decisions.jsonl')
    lifecycle.start('config')
    cfg_problems = []
    # B 类调参（重启生效）：把 runtime_config 里的覆盖值写回 rp_config/memory_card 常量，
    # 使 group_gate/memory_card 里直接读 cfg.GATE_XXX 的代码拿到用户改过的值。
    try:
        applied = rc.apply_overrides()
        if applied:
            LOG.info('已应用 %d 项调参覆盖：%s',
                     len(applied), ', '.join(k for k, _, _, _ in applied[:8]))
    except Exception:
        cfg_problems.append('调参覆盖')
        LOG.exception('调参覆盖应用失败（忽略，沿用默认值）')
    # 注册表自检：cfg 目标缺失/默认值类型漂移时这里先报，避免 get() 静默返回 None。
    problems = rc.validation_problems()
    if problems:
        cfg_problems.append('配置校验')
        LOG.warning('runtime 配置校验：%d 项问题 → %s',
                    len(problems), '；'.join(problems[:5]))
    # API 计费档位：面板里的选择（runtime_config）优先，其次环境变量 QWEN_PROFILE
    try:
        info = cfg.apply_profile(rc.get('api_profile', cfg.QWEN_PROFILE))
        LOG.info('API 档位：%s（%s · key 变量 %s）',
                 info['label'], info['base_url'], info['key_env'])
    except Exception:
        cfg_problems.append('API 档位')
        LOG.exception('API 档位应用失败（沿用 %s）', cfg.QWEN_PROFILE)
    lifecycle.finish('config', 'warn' if cfg_problems else 'ok',
                     ('失败：' + '、'.join(cfg_problems)) if cfg_problems else '配置已应用')
    bridge = Bridge(args)
    link = OneBotLink(args.host, args.port, args.path, bridge)
    bridge.set_link(link)
    _start_heartbeat(bridge)

    LOG.info('bot QQ：%s', args.self_id or '（未设置，等 NapCat 上报）')
    if args.group:
        LOG.info('只处理群：%s', ', '.join(args.group))
    if args.admin:
        LOG.info('管理员：%s', ', '.join(args.admin))
    LOG.info('模型：%s（%s · 并发 %d）', bridge.model, bridge.kind, bridge.conc)
    if bridge.judge_model != bridge.model:
        LOG.info('判官/记忆：%s（%s）', bridge.judge_model,
                 core.resolve_model(bridge.judge_model)[1])
    if bridge.kind == 'api':
        _key = os.environ.get(cfg.QWEN_API_KEY_ENV, '') or read_key_file(cfg.QWEN_API_KEY_FILE)
        if not _key:
            LOG.error('API 模式但没找到 key：把 key 写进 %s 或设置环境变量 %s'
                      '（否则每次生成都会失败，且 L2 终审会一律判沉默）',
                      cfg.QWEN_API_KEY_FILE, cfg.QWEN_API_KEY_ENV)
        else:
            _want = cfg.QWEN_KEY_PREFIX.get(cfg.QWEN_PROFILE)
            if _want and not _key.startswith(_want):
                LOG.error('key 与档位不匹配：%s 档需要 %s 开头的 key，当前是 %s…；'
                          '在管理面板切换「API 计费方式」或改环境变量 %s',
                          cfg.QWEN_PROFILE, _want, _key[:6], cfg.QWEN_API_KEY_ENV)
    elif bridge.kind == 'localapi':
        _key = os.environ.get(cfg.LOCAL_API_KEY_ENV, '') or read_key_file(cfg.LOCAL_API_KEY_FILE)
        if not _key:
            # 本机 Ollama / 内网服务通常免 key，缺 key 不算错误：只在远端地址上提醒。
            _host = (cfg.LOCAL_API_BASE_URL or '').split('//')[-1].split('/')[0]
            _local = _host.startswith(('127.0.0.1', 'localhost', '[::1]', '0.0.0.0'))
            if not _local:
                LOG.warning('端点 %s 没配 key：如果它是需要认证的服务，生成会失败；'
                            '写进 %s 或设置环境变量 %s',
                            cfg.LOCAL_API_BASE_URL, cfg.LOCAL_API_KEY_FILE,
                            cfg.LOCAL_API_KEY_ENV)
    LOG.info('硬件档位：%s', hw.summary())

    def self_check():
        # 自检放后台跑：本地模型首次加载要十几秒，不能卡住 WS 启动
        # （NapCat 一直在重连，卡住的话群消息会积压）。四项：模型可达/可出字、
        # embedding 维度、state 可写、记忆文件可解析。失败只告警，不拒绝启动——
        # 这里没有「不启动更安全」的选项，NapCat 是外部进程，停不起。
        lifecycle.start('selfcheck', '正在检查…')
        try:
            import selfcheck
            results = selfcheck.run_all(bridge.model, state_root=core.STATE_ROOT)
            for level, item, detail, fix in results:
                if level == selfcheck.OK:
                    LOG.info('[自检] %s：%s', item, detail)
                elif level == selfcheck.WARN:
                    LOG.warning('[自检] %s：%s%s', item, detail,
                                ('；修法：' + fix) if fix else '')
                else:
                    LOG.error('[自检] %s：%s%s', item, detail,
                              ('；修法：' + fix) if fix else '')
            level = selfcheck.worst(results)
            if level == selfcheck.FAIL:
                lifecycle.finish('selfcheck', 'fail', '有致命项未通过',
                                 '按自检日志里的修法处理')
                LOG.error('[自检] 有致命项未通过——对话很可能不能正常工作，按上面的修法处理')
            elif level == selfcheck.WARN:
                lifecycle.finish('selfcheck', 'warn', '有告警项，见日志')
                LOG.info('[自检] 完成（%d 项，有告警）', len(results))
            else:
                lifecycle.finish('selfcheck', 'ok', '全部通过（%d 项）' % len(results))
                LOG.info('[自检] 完成（%d 项）', len(results))
        except Exception:
            lifecycle.finish('selfcheck', 'warn', '自检执行失败，不影响对话')
            LOG.exception('[自检] 执行失败（不影响对话）')

    threading.Thread(target=self_check, name='selfcheck', daemon=True).start()

    def warm_daily():
        # 今日日常是「读缓存」不是「现生成」：没人预热她就整天空场景。
        # 放后台线程——首次生成要 5-7 秒，不能卡住 WS 启动。
        lifecycle.start('warmup', '准备今日日常…')
        try:
            txt = daily_scene.ensure_today()
            lifecycle.finish('warmup', 'ok', '今日日常就绪')
            LOG.info('今日日常就绪（%d 字）', len(txt or ''))
        except Exception:
            lifecycle.finish('warmup', 'warn', '今日日常生成失败，不影响对话')
            LOG.exception('今日日常生成失败（不影响对话）')

    threading.Thread(target=warm_daily, name='daily', daemon=True).start()

    # 管理面板：随 bot 生命周期启动，浏览器打开 http://127.0.0.1:6780
    def start_admin():
        try:
            port = getattr(args, 'admin_port', 6780)
            if not port:
                LOG.info('管理面板已关闭（--admin-port 0）')
                return
            import admin_server
            admin_server.set_bridge(bridge)
            token = admin_server.get_token()
            LOG.info('管理面板：http://127.0.0.1:%d/?token=%s', port, token)
            # 自动弹独立桌面窗口（等端口就绪），可用 --no-browser 或 auto_open_admin 开关关闭
            auto_open = rc.get('auto_open_admin', True)
            if auto_open and not getattr(args, 'no_browser', False):
                threading.Thread(
                    target=_open_admin_window_later, args=(port,),
                    kwargs={'token': token}, daemon=True).start()
            admin_server.serve(port)
        except Exception:
            LOG.exception('管理面板启动失败（不影响对话）')

    threading.Thread(target=start_admin, name='admin', daemon=True).start()

    def ticker():
        while True:
            time.sleep(max(5, args.tick))
            try:
                bridge.tick_proactive()
            except Exception:
                LOG.exception('主动开话题检查失败')
            try:
                daily_scene.refresh_async()   # 跨天自动换今日日常，不用重启
            except Exception:
                LOG.exception('今日日常刷新失败')
            try:
                self_state.refresh_async()    # 跨天写日记、补好奇清单，并同步正式日程
            except Exception:
                LOG.exception('自我状态刷新失败')

    threading.Thread(target=ticker, name='proactive', daemon=True).start()
    if args.dry_run:
        LOG.warning('dry-run：只打印，不真的发消息')
    lifecycle.start('napcat_online', '等待 NapCat 连接')
    link.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
