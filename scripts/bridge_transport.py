# -*- coding: utf-8 -*-
"""QQ 桥接的传输层：OneBot v11 消息段拼装、反向 WS 连接、发送。

从 qq_bridge.py 拆出，只为让「协议/传输」与「发言决策/队列」分开维护。
Bridge 通过 TransportMixin 继承发送相关方法；qq_bridge 继续再导出这里的
公开名字（OneBotLink / FakeLink / strip_at_tags 等），旧调用点不用改。
"""
import json
import logging
import queue
import re
import sys
import threading

import lifecycle
import group_roster
import rp_config as cfg
import runtime_config as rc

LOG = logging.getLogger('qq_bridge')

# 她回复里的 @ 标记：<@10000002> 或 <@群友>，发送时转成真正的 OneBot at 段。
# 只认本群名册里的 QQ 号/名字；不在名册里的一律丢弃（防止被诱导乱 @ 人）。
_AT_TAG = re.compile(r'[<＜]\s*[＠@]\s*([^>＞\n]{1,32}?)\s*[>＞]')
# 裸写法（模型常常直接写 @群友）：只在名字/QQ号精确命中本群名册时才转。
# 负向后顾排除 <@…>，避免把规范标记再包一层。
_BARE_AT = re.compile(r'(?<![<＜])[＠@]([^\s@＠<>＜＞]+)')


def _num(v):
    """OneBot 里 id 用 int 更保险（部分实现不认字符串）。"""
    s = str(v)
    return int(s) if s.isdigit() else v


def _resolve_at(key, lookup=None):
    """@ 标记里的内容 → uid。数字直接用；名字走本群名册映射。"""
    key = (key or '').strip()
    if key.isdigit():
        return key
    return str((lookup or {}).get(key) or '')


def strip_at_tags(text, uid_name=None):
    """去掉回复里的 <@…> 标记：记忆/上下文只留正文。

    uid_name: uid → 展示名；给了就把标记换成「@名字」，否则整段删掉。
    """
    def _repl(m):
        uid = _resolve_at(m.group(1))
        name = uid_name(uid) if (uid and uid_name) else ''
        return ('@%s' % name) if name else ''

    return _AT_TAG.sub(_repl, text or '').strip()


def _normalize_bare_ats(text, at_lookup=None, allowed_ats=None):
    """把裸 @名字 / @QQ号 规整成 <@uid>；只认名册里精确命中的前缀。

    例：「@群友是谁」→「<@10000002>是谁」。命中不了就原样保留，
    不动普通文本（「你@我了吗」里没有本群成员名字，不会被改写）。
    """
    if not text or not at_lookup:
        return text
    names = sorted((n for n in at_lookup if n), key=len, reverse=True)

    def _repl(m):
        rest = m.group(1)
        for name in names:
            if not rest.startswith(name):
                continue
            uid = str(at_lookup.get(name) or '')
            if uid and (allowed_ats is None or uid in allowed_ats):
                return '<@%s>%s' % (uid, rest[len(name):])
            # 命中的名字不可 @（不在本群/是 bot 自己）：原样保留，
            # 不能退到更短的名字上——那会把 A 的称呼安给 B。
            return m.group(0)
        mm = re.match(r'(\d{5,})', rest)
        if mm:
            uid = mm.group(1)
            if allowed_ats is not None and uid not in allowed_ats:
                return m.group(0)
            return '<@%s>%s' % (uid, rest[len(uid):])
        return m.group(0)

    return _BARE_AT.sub(_repl, text)


def _text_segments(text, at_lookup=None, allowed_ats=None):
    """正文 → 消息段；<@…> 展开成 at 段，其余原样保留。"""
    text = text or ''
    if not text:
        return []
    text = _normalize_bare_ats(text, at_lookup, allowed_ats)
    if not _AT_TAG.search(text):
        return [{'type': 'text', 'data': {'text': text}}]

    def _keep(m):
        uid = _resolve_at(m.group(1), at_lookup)
        if uid and (allowed_ats is None or str(uid) in allowed_ats):
            return m.group(0)
        return ''

    text = _AT_TAG.sub(_keep, text)
    segs, pos = [], 0
    for m in _AT_TAG.finditer(text):
        uid = _resolve_at(m.group(1), at_lookup)
        if m.start() > pos:
            chunk = text[pos:m.start()]
            if chunk:
                segs.append({'type': 'text', 'data': {'text': chunk}})
        segs.append({'type': 'at', 'data': {'qq': _num(uid)}})
        pos = m.end()
    tail = text[pos:]
    if tail:
        segs.append({'type': 'text', 'data': {'text': tail}})
    return segs or [{'type': 'text', 'data': {'text': ''}}]


def _file_uri(path):
    """本地图片 → file:// URI（NapCat 能直接读；Windows 盘符需要三段斜杠）。"""
    import pathlib
    return pathlib.Path(str(path)).resolve().as_uri()


def _segments(text, reply_to=None, record=None, image=None,
              at_lookup=None, allowed_ats=None):
    """把文字 / 回复引用 / 语音 / 图片拼成 OneBot v11 的消息段数组。

    record 是**本地音频绝对路径**——NapCat 会自己读文件并转成 24k 单声道 silk
    （它检测到不是 #!SILK 头就调 ffmpeg 转 ntsilk_s16le），所以我们不用自己转码。
    image 是**本地图片绝对路径**，转成 file:// URI 后交给 NapCat。
    """
    segs = []
    if reply_to:
        segs.append({'type': 'reply', 'data': {'id': str(reply_to)}})
    if record:
        segs.append({'type': 'record', 'data': {'file': str(record)}})
    if image:
        segs.append({'type': 'image', 'data': {'file': _file_uri(image)}})
    segs.extend(_text_segments(text, at_lookup, allowed_ats))
    if not segs:
        segs.append({'type': 'text', 'data': {'text': ''}})
    return segs


class TransportMixin(object):
    """Bridge 的发送侧方法：@ 白名单、展示文本、文字与图片发送。"""

    def _mentions_for(self, msg, gs):
        """本条消息提到的群友：最近发言者 + 本群名册（含重启前说过话的人）。

        名册命中单独返回，因为它的名字形式可能来自成员卡（她叫「群友」，
        群名片却是「肌霸群友（…）」）——不能再拿 nickname 做一次子串校验。
        """
        import rp_core as core
        try:
            hits = group_roster.match_text(msg.gid, msg.text) if msg.is_group else []
        except Exception:
            hits = []
        base = core.mentions_from_text(msg.text, gs.speaker_list(limit=8))
        merged = core.merge_speakers(hits, base, limit=8)
        sid = str(self.self_id or '')
        return [m for m in merged if str(m.get('uid') or '') != sid]

    def _display_text(self, msg, text):
        """回复的展示版：@ 标记换成「@名字」，供记忆/上下文使用。"""
        fn = (lambda uid: group_roster.display_name(msg.gid, uid)) if msg.is_group else None
        return strip_at_tags(text, uid_name=fn) or text

    def _allowed_at_uids(self, msg):
        """可被真正 @ 的 uid：本群名册 + 最近发言者 + 本条消息 @ 到的人。

        只允许命中这些集合，避免模型编造 QQ 号、或被用户诱导把 @ 打到群外。
        """
        allowed = set()
        try:
            allowed |= group_roster.uids(msg.gid)
        except Exception:
            pass
        with self.groups_lock:
            gs = self.groups.get(msg.gid)
        if gs is not None:
            allowed |= {str(uid) for uid, _nick in list(gs.speakers)}
        allowed |= {str(a.get('uid')) for a in (msg.ats or []) if a.get('uid')}
        allowed.discard(str(self.self_id or ''))   # 不 @ 自己
        return allowed

    def _send(self, msg, text, reply_to=None, image=None, force_ats=None):
        if not text and not image:
            return False
        if self.args.dry_run:
            LOG.info('[dry-run] → %s：%s%s', msg.gid or msg.uid,
                     text.replace('\n', ' '), ' [图片]' if image else '')
            return True
        if not self.link:
            LOG.warning('未连接，消息被丢弃：%s', (text or '[图片]')[:30])
            return False
        try:
            if msg.is_group:
                allowed = self._allowed_at_uids(msg)
                if force_ats:
                    allowed |= {str(a) for a in force_ats}
                kwargs = {'reply_to': reply_to,
                          'at_lookup': group_roster.name_map(msg.gid),
                          'allowed_ats': allowed}
                if image:
                    kwargs['image'] = image
                self.link.send_group_msg(msg.gid, text, **kwargs)
            else:
                if image:
                    self.link.send_private_msg(msg.uid, text, image=image)
                else:
                    self.link.send_private_msg(msg.uid, text)
            return True
        except Exception:
            self.stats['errors'] += 1
            LOG.exception('发送失败')
            return False

    def _send_record(self, msg, path):
        """只发语音（不带文字）。"""
        if self.args.dry_run:
            LOG.info('[dry-run] → 语音：%s', path)
            return
        if not self.link:
            LOG.warning('未连接，语音被丢弃：%s', path)
            return
        try:
            if msg.is_group:
                self.link.send_group_msg(msg.gid, '', record=path)
            else:
                self.link.send_private_msg(msg.uid, '', record=path)
        except Exception:
            self.stats['errors'] += 1
            LOG.exception('语音发送失败')


class OneBotLink(object):
    """OneBot v11 反向 WS 服务端：收事件、发 action。"""

    def __init__(self, host, port, path, bridge):
        self.host = host
        self.port = port
        self.path = path
        self.bridge = bridge
        self._ws = None
        self._lock = threading.Lock()
        self._seq = 0
        # echo → 等待响应的队列。用来做「同步问 NapCat 一句」的探测
        # （如 get_status / get_group_list），排查 NapCat 是否还活着。
        self._waiters = {}

    @property
    def connected(self):
        """NapCat 的反向 WS 是否还连着（心跳与 /diag 共用）。"""
        return self._ws is not None

    def call_api_raw(self, action, params=None, timeout=8):
        """发一个 action 并**等响应**。返回完整响应（含 retcode），失败返回 None。

        NapCat 的反向 WS 模式会回带 echo 的响应；这里用队列配对。
        `set_qq_avatar` 这类接口成功时 data 为 null，只看 data 会把成功当失败，
        需要判断 retcode 的调用方用这个方法。
        """
        with self._lock:
            if self._ws is None:
                return None
            self._seq += 1
            echo = '%s-%d' % (action, self._seq)
            q = queue.Queue(maxsize=1)
            self._waiters[echo] = q
            payload = {'action': action, 'params': params or {}, 'echo': echo}
            try:
                self._ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception:
                self._waiters.pop(echo, None)
                LOG.exception('发送 action 失败')
                return None
        try:
            obj = q.get(timeout=timeout)
        except queue.Empty:
            self._waiters.pop(echo, None)
            LOG.warning('action %s 超时（%.0fs 无响应）', action, timeout)
            return None
        return obj if isinstance(obj, dict) else None

    def call_api(self, action, params=None, timeout=8):
        """发一个 action 并**等响应**。返回响应的 data，失败返回 None。"""
        obj = self.call_api_raw(action, params, timeout)
        return (obj.get('data') if isinstance(obj, dict) else None)

    def send_action(self, action, params):
        """发一个 action（不等响应；响应在读取循环里只用于记录 message_id）。"""
        with self._lock:
            if self._ws is None:
                return False
            self._seq += 1
            payload = {'action': action, 'params': params,
                       'echo': '%s-%d' % (action, self._seq)}
            try:
                self._ws.send(json.dumps(payload, ensure_ascii=False))
                return True
            except Exception:
                LOG.exception('发送 action 失败')
                return False

    def send_group_msg(self, gid, text, reply_to=None, record=None, image=None,
                       at_lookup=None, allowed_ats=None):
        """发群消息。record 传本地音频路径则发一条语音（OneBot record 段）；
        image 传本地图片路径则发一张图（OneBot image 段）。

        text 里的 <@QQ号>/<@名字> 会转成真实的 at 段；allowed_ats 为空集时
        一个都不转（只发正文），防止模型或用户诱导 @ 到群外的人。
        """
        params = {'group_id': _num(gid),
                  'message': _segments(text, reply_to, record, image,
                                       at_lookup=at_lookup,
                                       allowed_ats=allowed_ats)}
        return self.send_action('send_group_msg', params)

    def send_private_msg(self, uid, text, record=None, image=None):
        return self.send_action('send_private_msg',
                                {'user_id': _num(uid),
                                 'message': _segments(text, None, record, image)})

    def _api_response(self, obj):
        """处理 API 响应：记下自己发出的 message_id，并唤醒等待 echo 的调用方。

        data 不一定是 dict —— get_group_member_list 返回的是成员列表。
        以前这里无条件 data.get('message_id')，一个列表响应就把整个读循环打崩
        （2026-11-02 线上：连接反复断开、名册超时）。
        """
        data = obj.get('data')
        mid = data.get('message_id') if isinstance(data, dict) else None
        if mid:
            self.bridge.our_mids.append(str(mid))
        echo = obj.get('echo')
        if echo:
            w = self._waiters.pop(echo, None)
            if w is not None:
                try:
                    w.put_nowait(obj)
                except queue.Full:
                    pass

    def _handle(self, ws):
        self._ws = ws
        lifecycle.mark('napcat_online', 'ok', '已连接')
        LOG.info('NapCat 已连接：%s:%s%s', self.host, self.port, self.path)
        # 预热群名册：必须另起线程——call_api 等的是本函数的读循环，自己等自己会死锁。
        try:
            threading.Thread(target=self.bridge.warm_rosters,
                             name='roster-warm', daemon=True).start()
        except Exception:
            LOG.debug('群名册预热线程启动失败')
        # 每次进程启动只同步一次当前角色的 QQ 昵称/头像；之后切换角色再由
        # /character 或面板触发。未启用 qq_profile 的角色会直接返回。
        try:
            if not getattr(self.bridge, '_profile_sync_started', False):
                self.bridge._profile_sync_started = True
                threading.Thread(target=self.bridge.sync_qq_profile,
                                 name='qq-profile-startup', daemon=True).start()
        except Exception:
            LOG.debug('启动同步 QQ 档案失败')
        # NapCat 重启 / 网络抖动是常态：连接被对端切断时 websockets 抛
        # ConnectionClosedError，不接住就会打出一整段 traceback（
        # 「connection handler failed」），看着像故障，其实只是等重连。
        # **只吞这一个异常**，其它异常照旧上抛，真 bug 不会被藏起来。
        from websockets.exceptions import ConnectionClosed as _Closed
        try:
            for raw in ws:
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if 'retcode' in obj:      # API 响应
                    self._api_response(obj)
                    continue
                # 事件到达的原始记录（DEBUG 级）：排查「群里发了但 bot 没反应」时，
                # 要先确认事件到底有没有到 bot —— 是 NapCat 没推，还是 bot 过滤掉了。
                LOG.debug('收到事件：%s %s', obj.get('post_type'),
                          str(obj.get('message') or '')[:80])
                self.bridge.on_event(obj)
        except _Closed as e:
            LOG.info('NapCat 连接关闭：%s', e or type(e).__name__)
        finally:
            self._ws = None
            lifecycle.mark('napcat_online', 'warn', '连接断开，等待重连')
            LOG.warning('NapCat 连接断开，等待重连…')

    def serve_forever(self):
        try:
            from websockets.sync.server import serve
        except ImportError:
            lifecycle.finish('ws_listen', 'fail', '缺少 websockets 依赖',
                             'pip install -r requirements.txt')
            sys.exit('缺少依赖：pip install websockets（只需连 QQ 时才需要）')
        lifecycle.start('ws_listen', '正在绑定端口…')
        path = self.path

        # websockets ≥14 的 sync serve() 没有 path 参数了，握手地址只能在
        # process_request 里自己校验；返回 None 表示放行。
        def _check_path(connection, request):
            if request.path != path:
                return connection.respond(404, 'not found\n')
            return None

        try:
            with serve(self._handle, self.host, self.port,
                       process_request=_check_path) as server:
                lifecycle.finish('ws_listen', 'ok',
                                 'ws://%s:%d%s' % (self.host, self.port, self.path))
                lifecycle.finish('ready', 'ok', '服务已就绪')
                LOG.info('反向 WS 已启动：ws://%s:%d%s ← 在 NapCat 里填这个地址',
                         self.host, self.port, self.path)
                server.serve_forever()
        except OSError as e:
            lifecycle.finish('ws_listen', 'fail', str(e), '检查端口是否被占用')
            raise


class FakeLink(object):
    """selftest 用的替身：记录发出的消息，不连 QQ。"""

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
