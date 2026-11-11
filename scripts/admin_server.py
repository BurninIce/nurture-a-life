# -*- coding: utf-8 -*-
"""管理面板后端（stdlib http.server，无第三方依赖）。

提供：
  GET  /api/config   当前运行时配置（热生效开关）
  PUT  /api/config   更新运行时配置（body: JSON，部分字段），立即生效 + 落盘
  GET  /api/status   运行状态（NapCat/Ollama 在线、显存档位、运行统计、当前模型）
  GET  /api/members  群成员卡列表（昵称/称呼/身份/晋升/消息数/好感）
  GET  /api/group    群氛围 + 最近事件 + 成员风格画像
  GET  /api/models   模型清单（供前端下拉框，后端权威）
  GET  /api/model-catalog  某后端的模型目录（?provider=&profile=&limit=&current=）
  GET  /api/persona  角色卡编辑数据（?pid=，缺省当前角色）
  PUT  /api/persona  保存角色卡清单/文本/发言决策（body: {id, manifest?, card?, gate?}）
  POST /api/persona/import  导入角色卡包（body: 扁平 card.json 或 {persona, card, stats}）
  GET  /api/mem      某用户记忆明细（?uid=）
  POST /api/mem/rollback  回滚某天新增的记忆（body: {uid, day?, dry_run?}）
  POST /api/mem/favorite  收藏/取消收藏事件（body: {uid, id, favorite}）
  POST /api/mem/delete    归档删除事件（body: {uid, id, reason?, dry_run?}）
  POST /api/mem/restore   从删除归档恢复事件（body: {uid, id}）
  GET  /             index.html（admin_ui）

启动（独立进程/线程，默认 127.0.0.1:6780）：
    python scripts/admin_server.py --port 6780

安全：只绑 127.0.0.1，并校验管理 token（环境变量 NAL_ADMIN_TOKEN，
或首次启动生成后落盘 state/admin_token）。本机其他进程/浏览器页面拿不到 token
就无法读写配置与记忆；/healthz 与静态资源（无数据）不校验，方便探活。
"""
import argparse
import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UI_DIR = os.path.join(ROOT, 'admin_ui')
sys_path = HERE
if sys_path not in __import__('sys').path:
    __import__('sys').path.insert(0, sys_path)

import admin_payloads
import group_settings
import lifecycle
import persona
import persona_import
import runtime_config as rc
from admin_payloads import (
    _first_identity_uid, _group_payload, _cg_dir, _cg_images,
    _cg_name_ok, _cg_prompt, _cg_status, _members_payload,
    _groups_payload, _group_settings_payload,
    _mem_detail, _mem_summary, _models_payload, _persona_detail,
    _personas_payload, _schema_payload, _supervisor_payload, _tts_health,
    _tts_url, _user_state_dir, model_catalog, status_payload,
)

# 由 qq_bridge 启动时注入的 Bridge 实例（用于读取实时运行统计）。
# 独立进程运行（python admin_server.py）时为 None，统计部分返回空。
BRIDGE = None


def set_bridge(bridge):
    """qq_bridge 启动面板前注入，供 status 读取实时 stats。"""
    global BRIDGE
    BRIDGE = bridge
    admin_payloads.set_bridge(bridge)


_TOKEN = None
_TOKEN_LOCK = threading.Lock()


def token_path():
    return os.path.join(ROOT, 'state', 'admin_token')


def get_token():
    """管理 token：优先环境变量；否则读 state/admin_token，缺失时生成并落盘。"""
    global _TOKEN
    with _TOKEN_LOCK:
        if _TOKEN:
            return _TOKEN
        env = (os.environ.get('NAL_ADMIN_TOKEN') or '').strip()
        if env:
            _TOKEN = env
            return _TOKEN
        path = token_path()
        tok = ''
        try:
            with open(path, encoding='utf-8') as f:
                tok = f.read().strip()
        except OSError:
            pass
        if not tok:
            tok = secrets.token_urlsafe(32)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(tok + '\n')
            except OSError:
                pass
        _TOKEN = tok
        return _TOKEN


def _probe_api_models():
    """切档前探测：生成/判官正在用的 API 模型在新端点是否可用。

    全本地模型时用 qwen3.8-flash 探路（两个端点都有）。任一失败即抛错。
    """
    import rp_core as core
    from rp_common import chat_qwen
    ids = []
    for key in (rc.get('gen_model'), rc.get('judge_model')):
        try:
            kind, model_id, _ = core.resolve_model(key)
        except Exception:
            continue
        if kind == 'api' and model_id not in ids:
            ids.append(model_id)
    if not ids:
        ids = ['qwen3.8-flash']
    for model_id in ids:
        out = chat_qwen([{'role': 'user', 'content': 'ping'}], model_id,
                        max_tokens=1, timeout=15)
        if out is None:
            raise RuntimeError('%s 通了但没返回内容（可能额度用尽）' % model_id)
    return ids


def _tts_text(payload):
    """校验试听文本：非空、长度上限（试听不是批量接口）。"""
    text = (payload.get('text') or '').strip()
    if not text:
        raise ValueError('请先输入要合成的话')
    if len(text) > 500:
        raise ValueError('试听文本最长 500 字')
    return text


def switch_profile(target):
    """切换 API 计费档位：先应用、再探测，失败回滚到原档位。

    返回新档位名；失败抛异常（此时进程内档位已恢复原值，且不落盘）。
    """
    import rp_config as cfg
    target = str(target or '').strip().lower()
    if target not in cfg.QWEN_PROFILES:
        raise ValueError('未知档位：%s' % target)
    old = cfg.QWEN_PROFILE
    try:
        cfg.apply_profile(target)
        _probe_api_models()
    except Exception:
        cfg.apply_profile(old)
        raise
    return target


# ---------- CG（本机 ComfyUI） ----------

def _cg_params(payload):
    """解析并校验生成参数；非法时抛 ValueError。"""
    uid = str(payload.get('uid') or '').strip()
    if not uid.isdigit():
        raise ValueError('uid 必须是数字')
    stage = payload.get('stage') or None
    prompt = (payload.get('prompt') or '').strip()
    if len(prompt) > 4000:
        raise ValueError('提示词最长 4000 字')

    def num(key, default, cast, label):
        value = payload.get(key)
        if value in (None, ''):
            return default
        try:
            return cast(value)
        except (TypeError, ValueError):
            raise ValueError('%s 必须是数字' % label)

    width = num('width', 768, int, '宽度')
    height = num('height', 512, int, '高度')
    steps = num('steps', 20, int, '步数')
    cfg = num('cfg', 7.0, float, 'cfg')
    for label, value in (('宽度', width), ('高度', height)):
        if not 256 <= value <= 2048 or value % 8:
            raise ValueError('%s要在 256-2048 之间且为 8 的倍数' % label)
    if not 1 <= steps <= 100:
        raise ValueError('步数要在 1-100 之间')
    if not 0 <= cfg <= 30:
        raise ValueError('cfg 要在 0-30 之间')
    seed = payload.get('seed')
    if seed in (None, ''):
        seed = None
    else:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            raise ValueError('seed 必须是整数')
        if not 0 <= seed <= 2147483647:
            raise ValueError('seed 要在 0-2147483647 之间')
    return {'uid': uid, 'stage': stage, 'prompt': prompt,
            'width': width, 'height': height, 'steps': steps, 'cfg': cfg,
            'seed': seed}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get('Content-Length', 0) or 0)
        return self.rfile.read(n) if n else b''

    def _token_ok(self):
        given = (self.headers.get('X-Admin-Token') or '').strip()
        if not given:
            from urllib.parse import urlparse, parse_qs
            given = (parse_qs(urlparse(self.path).query).get('token') or [''])[0].strip()
        return bool(given) and hmac.compare_digest(given, get_token())

    def _same_origin(self):
        """浏览器发起的写请求必须同源；无 Origin（curl/测试）放行，token 仍兜底。"""
        origin = (self.headers.get('Origin') or '').strip()
        if not origin:
            return True
        from urllib.parse import urlparse
        u = urlparse(origin)
        port = self.server.server_address[1]
        return (u.scheme == 'http' and u.hostname in ('127.0.0.1', 'localhost')
                and u.port == port)

    def _write_ok(self):
        if not self._token_ok() or not self._same_origin():
            return False
        ctype = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        return ctype == 'application/json'

    def do_GET(self):
        path = self.path.split('?')[0]
        if (path not in ('/', '/index.html') and path != '/healthz'
                and not self._token_ok()):
            self._send(403, {'error': 'forbidden'})
            return
        if path == '/api/config':
            self._send(200, rc.all())
        elif path == '/api/schema':
            self._send(200, _schema_payload())
        elif path == '/api/personas':
            self._send(200, _personas_payload())
        elif path == '/api/persona':
            try:
                self._send(200, _persona_detail(self._str_arg('pid')))
            except ValueError as e:
                self._send(404, {'error': str(e)})
        elif path == '/api/status':
            self._send(200, status_payload())
        elif path == '/api/supervisor':
            self._send(200, _supervisor_payload())
        elif path == '/api/members':
            self._send(200, _members_payload())
        elif path == '/api/group':
            self._send(200, _group_payload(self._str_arg('gid')))
        elif path == '/api/groups':
            self._send(200, _groups_payload())
        elif path == '/api/group-settings':
            try:
                self._send(200, _group_settings_payload(self._str_arg('gid')))
            except ValueError as e:
                self._send(400, {'error': str(e)})
        elif path == '/api/models':
            self._send(200, _models_payload())
        elif path == '/api/model-catalog':
            try:
                self._send(200, model_catalog(
                    self._str_arg('provider', 'local'),
                    self._str_arg('profile'),
                    self._int_arg('limit', None),
                    self._str_arg('current')))
            except ValueError as e:
                self._send(400, {'error': str(e)})
        elif path == '/api/tts/health':
            self._send(200, _tts_health())
        elif path == '/api/cg/status':
            self._send(200, _cg_status())
        elif path == '/api/cg/prompt':
            uid = self._query_uid()
            if not uid or not uid.isdigit():
                self._send(400, {'error': 'uid 必须是数字'})
                return
            self._send(200, _cg_prompt(uid, self._str_arg('stage')))
        elif path == '/api/cg/images':
            uid = self._query_uid()
            if not uid or not uid.isdigit():
                self._send(400, {'error': 'uid 必须是数字'})
                return
            self._send(200, _cg_images(uid))
        elif path == '/api/cg/image':
            self._cg_image()
        elif path == '/healthz':
            snap = lifecycle.snapshot(include_launcher=True)
            code = 503 if snap.get('worst') == 'fail' else 200
            self._send(code, {'ok': snap.get('worst') != 'fail',
                              'worst': snap.get('worst'), 'ts': snap.get('ts')})
        elif path == '/api/lifecycle':
            self._send(200, lifecycle.snapshot(include_launcher=True))
        elif path == '/api/events':
            self._send(200, lifecycle.events_since(
                self._int_arg('since', 0), self._float_arg('timeout', 0.0)))
        elif path == '/api/logs':
            self._send(200, lifecycle.logs_since(
                self._int_arg('since', 0), level=self._str_arg('level'),
                limit=self._int_arg('limit', 200)))
        elif path.startswith('/api/mem'):
            self._send(200, _mem_detail(self._query_uid()))
        elif path in ('/', '/index.html'):
            self._serve_index()
        else:
            self._send(404, {'error': 'not found'})

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    def _str_arg(self, name, default=None):
        return (self._query().get(name) or [default])[0]

    def _int_arg(self, name, default=0):
        try:
            return int(self._str_arg(name, default) or default)
        except (TypeError, ValueError):
            return default

    def _float_arg(self, name, default=0.0):
        try:
            return float(self._str_arg(name, default) or default)
        except (TypeError, ValueError):
            return default

    def _query_uid(self):
        """从 query string 取 uid，缺省取主用户。"""
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        uid = (q.get('uid') or [None])[0]
        return uid or _first_identity_uid()

    def do_PUT(self):
        path = self.path.split('?')[0]
        if path == '/api/persona':
            if not self._write_ok():
                self._send(403, {'error': 'forbidden'})
                return
            self._persona_save()
            return
        if path == '/api/group-settings':
            if not self._write_ok():
                self._send(403, {'error': 'forbidden'})
                return
            self._group_settings_save()
            return
        if path != '/api/config':
            self._send(404, {'error': 'not found'})
            return
        if not self._write_ok():
            self._send(403, {'error': 'forbidden'})
            return
        try:
            kv = json.loads(self._read_body().decode('utf-8'))
            if not isinstance(kv, dict):
                raise ValueError('body must be object')
            if 'api_profile' in kv:
                # 先切档、再探测；探测失败回滚，不落盘
                import rp_config as cfg
                old = cfg.QWEN_PROFILE
                try:
                    switch_profile(kv.get('api_profile'))
                except ValueError as e:
                    self._send(400, {'error': str(e)})
                    return
                except Exception as e:
                    self._send(400, {
                        'error': '切换失败，已保持「%s」：%s'
                                 % (cfg.QWEN_PROFILES.get(old, {}).get('label', old),
                                    str(e)[:200]),
                    })
                    return
            if 'character' in kv:
                ok, reason = persona.validate(str(kv.get('character') or ''),
                                              strict=True)
                if not ok:
                    self._send(400, {'error': '角色不可用：%s' % reason})
                    return
            updated = rc.set_many(kv)
            if 'character' in kv:
                self._after_switch()
            self._send(200, updated)
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {'error': str(e)})

    def _group_settings_save(self):
        """保存某个群的参数覆盖；返回最新参数页供前端整体重绘。"""
        try:
            payload = json.loads(self._read_body().decode('utf-8') or '{}')
            if not isinstance(payload, dict):
                raise ValueError('body must be object')
            gid = str(payload.get('gid') or '').strip()
            if not gid.isdigit():
                raise ValueError('群号必须是数字')
            if 'copy_from' in payload:
                group_settings.copy(str(payload.get('copy_from') or ''), gid)
            elif 'unset' in payload:
                group_settings.unset(gid, payload.get('unset'))
            elif isinstance(payload.get('set'), dict):
                group_settings.set_many(gid, payload['set'])
            else:
                raise ValueError('需要 set、unset 或 copy_from')
            self._send(200, _group_settings_payload(gid))
        except ValueError as e:
            self._send(400, {'error': str(e)})
        except Exception as e:  # noqa: BLE001 - 面板要看到原因而不是连接断开
            self._send(500, {'error': str(e)[:300]})

    def _persona_save(self):
        """保存角色卡编辑内容；返回最新编辑数据供前端整体重绘。"""
        try:
            payload = json.loads(self._read_body().decode('utf-8'))
            if not isinstance(payload, dict):
                raise ValueError('body must be object')
            out = persona_import.update_bundle(payload)
            out['detail'] = _persona_detail(out['id'])
            self._send(200, out)
        except persona_import.PersonaImportError as e:
            self._send(400, {'error': str(e)})
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {'error': str(e)})
        except Exception as e:  # noqa: BLE001 - 面板要看到原因而不是连接断开
            self._send(500, {'error': str(e)[:300]})

    def _after_switch(self):
        """切换角色后让桥接层清上下文、同步 QQ 档案（独立运行时无事可做）。"""
        if BRIDGE is None:
            return
        try:
            BRIDGE._check_persona_switch()
            spec, _why = persona.qq_profile()
            if spec:
                threading.Thread(target=BRIDGE.sync_qq_profile,
                                 name='qq-profile-sync', daemon=True).start()
        except Exception:
            import logging
            logging.getLogger('admin').exception('切换角色后清理上下文失败')

    def do_POST(self):
        path = self.path.split('?')[0]
        if not self._write_ok():
            self._send(403, {'error': 'forbidden'})
            return
        if path == '/api/persona/import':
            self._persona_import()
        elif path == '/api/config/reset':
            out = rc.reset()
            import rp_config as cfg
            target = out.get('api_profile')
            if target and target != cfg.QWEN_PROFILE:
                try:
                    switch_profile(target)
                except Exception:
                    # 切不回默认档就保持原档，并把配置改回当前值，避免面板与实况不一致
                    out = rc.set_value('api_profile', cfg.QWEN_PROFILE)
            self._send(200, out)
        elif path == '/api/tts/synth':
            self._tts_synth()
        elif path == '/api/cg/generate':
            self._cg_generate()
        elif path == '/api/mem/rollback':
            self._mem_rollback()
        elif path == '/api/mem/favorite':
            self._mem_favorite()
        elif path == '/api/mem/delete':
            self._mem_delete()
        elif path == '/api/mem/restore':
            self._mem_restore()
        else:
            self._send(404, {'error': 'not found'})

    def _persona_import(self):
        """导入角色卡（扁平 card.json 或 {persona, card, stats} 包）。"""
        try:
            n = int(self.headers.get('Content-Length', 0) or 0)
        except (TypeError, ValueError):
            n = 0
        if n > persona_import.MAX_BUNDLE_BYTES:
            self._send(413, {'error': '导入包过大（上限 2 MB）'})
            return
        if n <= 0:
            self._send(400, {'error': '请求体为空'})
            return
        try:
            payload = json.loads(self._read_body().decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._send(400, {'error': 'JSON 解析失败：%s' % str(e)[:200]})
            return
        try:
            out = persona_import.import_bundle(payload)
        except persona_import.PersonaImportConflict as e:
            self._send(409, {'error': str(e)})
            return
        except persona_import.PersonaImportError as e:
            self._send(400, {'error': str(e)})
            return
        except Exception as e:
            self._send(500, {'error': '导入失败：%s' % str(e)[:200]})
            return
        if out.get('activated'):
            self._after_switch()
        self._send(200, dict(out, personas=_personas_payload()))

    def _mem_payload(self):
        """解析记忆维护请求的 uid + 事件 id；非法时抛 ValueError。"""
        payload = json.loads(self._read_body().decode('utf-8') or '{}')
        if not isinstance(payload, dict):
            raise ValueError('body must be object')
        uid = str(payload.get('uid') or '').strip()
        if not uid.isdigit():
            raise ValueError('uid 必须是数字')
        try:
            eid = int(payload.get('id'))
        except (TypeError, ValueError):
            raise ValueError('id 必须是事件编号')
        return payload, uid, eid

    def _mem_favorite(self):
        """收藏/取消收藏一条事件（带锁切目录，避免与对话写入交叉）。"""
        try:
            payload, uid, eid = self._mem_payload()
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {'error': str(e)})
            return
        try:
            import memory_card as mem
            with mem.state_scope(_user_state_dir(uid)):
                out = mem.set_episode_favorite(eid, bool(payload.get('favorite', True)))
            self._send(200, {'uid': uid, 'result': out, 'summary': _mem_summary(uid)})
        except ValueError as e:
            self._send(400, {'error': str(e)})
        except Exception as e:
            self._send(500, {'error': '收藏失败：%s' % str(e)[:200]})

    def _mem_delete(self):
        """归档删除一条事件；dry_run 只返回受影响的卡片条目（二次确认用）。"""
        try:
            payload, uid, eid = self._mem_payload()
            dry = bool(payload.get('dry_run'))
            reason = str(payload.get('reason') or '').strip()
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {'error': str(e)})
            return
        try:
            import memory_card as mem
            with mem.state_scope(_user_state_dir(uid)):
                report = mem.delete_episode(eid, reason=reason, dry_run=dry)
            self._send(200, {'uid': uid, 'report': report,
                             'summary': _mem_summary(uid)})
        except ValueError as e:
            self._send(400, {'error': str(e)})
        except Exception as e:
            self._send(500, {'error': '删除失败：%s' % str(e)[:200]})

    def _mem_restore(self):
        """把删除归档里的事件放回事件库。"""
        try:
            _, uid, eid = self._mem_payload()
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {'error': str(e)})
            return
        try:
            import memory_card as mem
            with mem.state_scope(_user_state_dir(uid)):
                out = mem.restore_episode(eid)
            self._send(200, {'uid': uid, 'result': out, 'summary': _mem_summary(uid)})
        except ValueError as e:
            self._send(400, {'error': str(e)})
        except Exception as e:
            self._send(500, {'error': '恢复失败：%s' % str(e)[:200]})

    def _mem_rollback(self):
        """回滚某天新增的记忆（默认今天）；dry_run 只预览不落盘。"""
        try:
            payload = json.loads(self._read_body().decode('utf-8') or '{}')
            if not isinstance(payload, dict):
                raise ValueError('body must be object')
            uid = str(payload.get('uid') or '').strip()
            if not uid.isdigit():
                raise ValueError('uid 必须是数字')
            day = str(payload.get('day') or '').strip()
            if day:
                time.strptime(day, '%Y-%m-%d')
            dry = bool(payload.get('dry_run'))
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {'error': str(e)})
            return
        try:
            import memory_card as mem
            # state_scope 持锁切换目录：回滚与正在进行的对话轮次不会互相覆盖
            with mem.state_scope(_user_state_dir(uid)):
                report = mem.rollback_day(day or None, dry_run=dry)
            self._send(200, {'uid': uid, 'report': report,
                             'summary': _mem_summary(uid)})
        except ValueError as e:
            self._send(400, {'error': str(e)})
        except Exception as e:
            self._send(500, {'error': '回滚失败：%s' % str(e)[:200]})

    def _tts_synth(self):
        """面板试听：调 TTS 服务合成，直接把 wav 流回浏览器。"""
        try:
            payload = json.loads(self._read_body().decode('utf-8'))
            if not isinstance(payload, dict):
                raise ValueError('body must be object')
            text = _tts_text(payload)
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {'error': str(e)})
            return
        style = payload.get('style')
        if style not in ('daily', 'battle'):
            style = 'daily'
        import tts_client
        url = _tts_url()
        res = tts_client.synth(text, style=style, url=url, timeout=90)
        if not res:
            hint = (('TTS 服务未启动（%s）' % url)
                    if not _tts_health().get('online')
                    else '合成失败（查看 TTS 窗口日志）')
            self._send(502, {'error': hint})
            return
        path, dur = res
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError as e:
            self._send(500, {'error': '音频读取失败：%s' % e})
            return
        self.send_response(200)
        self.send_header('Content-Type', 'audio/wav')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-TTS-Duration', '%.2f' % float(dur or 0))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _cg_image(self):
        uid = (self._str_arg('uid') or '').strip()
        name = (self._str_arg('name') or '').strip()
        if not uid.isdigit() or not _cg_name_ok(name):
            self._send(400, {'error': 'bad uid or name'})
            return
        folder = os.path.realpath(_cg_dir(uid))
        path = os.path.realpath(os.path.join(folder, name))
        if os.path.dirname(path) != folder or not os.path.isfile(path):
            self._send(404, {'error': 'image not found'})
            return
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError as e:
            self._send(500, {'error': '读取失败：%s' % e})
            return
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(body)

    def _cg_generate(self):
        try:
            payload = json.loads(self._read_body().decode('utf-8') or '{}')
            if not isinstance(payload, dict):
                raise ValueError('body must be object')
            params = _cg_params(payload)
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {'error': str(e)})
            return
        import cg_gen
        pid = persona.active_id()
        res = cg_gen.generate(
            params['uid'], stage=params['stage'],
            prompt=params['prompt'] or None, width=params['width'],
            height=params['height'], steps=params['steps'], cfg=params['cfg'],
            seed=params['seed'], card=persona.card_json_path(pid), pid=pid)
        if not res.get('ok'):
            self._send(502, {'error': res.get('error') or '生成失败'})
            return
        res['url'] = '/api/cg/image?uid=%s&name=%s' % (params['uid'], res['name'])
        res.pop('meta', None)
        self._send(200, res)

    def _serve_index(self):
        path = os.path.join(UI_DIR, 'index.html')
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError:
            self._send(404, {'error': 'index.html missing'})
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # 静默，不刷控制台


def serve(port=6780):
    lifecycle.configure('bridge', lifecycle.BRIDGE_STAGES)
    lifecycle.attach_logs()
    rc.load()
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    srv.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(description='NAL 管理面板后端')
    ap.add_argument('--port', type=int, default=6780)
    args = ap.parse_args(argv)
    print('管理面板：http://127.0.0.1:%d/?token=%s' % (args.port, get_token()))
    serve(args.port)


if __name__ == '__main__':
    main()
