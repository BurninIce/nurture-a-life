# -*- coding: utf-8 -*-
"""TTS 客户端（bot 侧，只用标准库）。

对端是**单独的** GPT-SoVITS 服务（官方 api_v2.py，MIT 许可），
本仓库不含它的代码与权重，只通过公开 HTTP 接口调用：

    POST {base}/tts  JSON 请求 → audio/wav 字节

配置全部走环境变量（见 .env.example）：
    NAL_TTS_URL            服务地址，默认 http://127.0.0.1:9880
    NAL_TTS_REF_AUDIO      参考音频路径（必填，服务端可读）
    NAL_TTS_PROMPT_TEXT    参考音频对应的文本（建议填写）
    NAL_TTS_REF_DAILY      可选：日常语气专用参考音频
    NAL_TTS_REF_BATTLE     可选：战斗语气专用参考音频

任何失败都不能影响文字回复：所有函数返回 None 而不是抛异常。
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URL = os.environ.get('NAL_TTS_URL') or 'http://127.0.0.1:9880'
OUT_DIR = os.environ.get('NAL_TTS_OUT') or os.path.join(
    ROOT, 'state', 'tts_cache')

_REF_ENV = {'daily': 'NAL_TTS_REF_DAILY',
            'battle': 'NAL_TTS_REF_BATTLE'}
_PROMPT_ENV = {'daily': 'NAL_TTS_PROMPT_DAILY',
               'battle': 'NAL_TTS_PROMPT_BATTLE'}

_warned = set()


def _warn_once(key, message):
    if key in _warned:
        return
    _warned.add(key)
    print('[tts] ' + message)


def _ref_audio(style):
    return (os.environ.get(_REF_ENV.get(style, '')) or
            os.environ.get('NAL_TTS_REF_AUDIO') or '').strip()


def _prompt_text(style):
    return (os.environ.get(_PROMPT_ENV.get(style, '')) or
            os.environ.get('NAL_TTS_PROMPT_TEXT') or '').strip()


def _duration(path):
    try:
        with wave.open(path, 'rb') as f:
            return f.getnframes() / float(f.getframerate() or 1)
    except Exception:
        return 0.0


def _clean_old(days=1):
    cutoff = time.time() - days * 86400
    try:
        names = os.listdir(OUT_DIR)
    except OSError:
        return
    for name in names:
        path = os.path.join(OUT_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def _post(url, payload, timeout):
    req = urllib.request.Request(
        url.rstrip('/') + '/tts',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def synth(text, style='daily', url=DEFAULT_URL, timeout=75):
    """合成语音。成功返回 (wav 路径, 时长秒)，失败返回 None。

    GPT-SoVITS 的语气由参考音频决定，这里把 style 映射到不同的 ref。
    超时给 75s：冷启动/首次推理可能较慢，但别让客户端先于服务端放弃。
    """
    text = (text or '').strip()
    if not text:
        return None
    ref = _ref_audio(style)
    if not ref:
        _warn_once('ref', '未配置参考音频：设置 NAL_TTS_REF_AUDIO '
                          '（或按语气的 NAL_TTS_REF_DAILY/BATTLE）')
        return None
    payload = {
        'text': text,
        'text_lang': os.environ.get('NAL_TTS_TEXT_LANG') or 'zh',
        'ref_audio_path': ref,
        'prompt_text': _prompt_text(style),
        'prompt_lang': os.environ.get('NAL_TTS_PROMPT_LANG') or 'zh',
        'top_k': 5,
        'top_p': 1.0,
        'temperature': 1.0,
        'text_split_method': 'cut5',
        'batch_size': 1,
        'speed_factor': float(os.environ.get('NAL_TTS_SPEED') or 1.0),
        'streaming_mode': False,
        'media_type': 'wav',
    }
    try:
        body = _post(url, payload, timeout)
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'ignore')[:200]
        except Exception:
            pass
        print('[tts] 服务返回 %s %s' % (e.code, detail))
        return None
    except Exception as e:
        print('[tts] 服务不可用：%s' % e)
        return None
    if not body.startswith(b'RIFF') or b'WAVE' not in body[:16]:
        print('[tts] 返回内容不是 WAV：%r' % body[:80])
        return None
    os.makedirs(OUT_DIR, exist_ok=True)
    _clean_old()
    name = '%s_%s_%s.wav' % (time.strftime('%Y%m%d_%H%M%S'),
                             style or 'daily',
                             hashlib.sha1(text.encode('utf-8')).hexdigest()[:8])
    path = os.path.join(OUT_DIR, name)
    tmp = path + '.part'
    try:
        with open(tmp, 'wb') as f:
            f.write(body)
        os.replace(tmp, path)
    except OSError as e:
        print('[tts] 音频落盘失败：%s' % e)
        return None
    return path, _duration(path)


def health(url=DEFAULT_URL, timeout=5):
    """服务在不在。官方 api 没有 /health，用 /docs 探测（404 也算在线）。"""
    url = (url or DEFAULT_URL).rstrip('/')
    try:
        with urllib.request.urlopen(url + '/docs', timeout=timeout):
            pass
    except urllib.error.HTTPError:
        pass  # 服务有响应，只是没有 /docs
    except Exception:
        return None
    ref = _ref_audio('daily')
    return {'online': True, 'url': url,
            'ref_audio': os.path.basename(ref) if ref else ''}
