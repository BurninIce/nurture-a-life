# -*- coding: utf-8 -*-
"""图片识别 + 链接抓取：让模型「看懂」群友发的图片和链接。

设计：
- 图片：调视觉模型识别内容（默认走本地 API，也可切千问云端），
  输入是 QQ 图片的临时 url（CQ 码里的 url 字段），直接传 URL，无需下载。
- 链接：抓取网页 <title> 与正文摘要，喂给模型。轻量、带超时与缓存，
  抓取失败（被墙/超时/非网页）静默降级，绝不阻塞主流程。

原则：
- 全部函数「失败静默」：识别不出、抓不到、超时，都返回空/原文，不抛异常，
  保证 bot 不会因为一张图/一个链接就崩。
- 缓存：同一 url 短期内只识别/抓取一次（群聊常转发同一张图/同一个链接）。
- 只在群聊里有人发图/发链接、且相关开关打开时才调用（调用方控制）。
"""
import base64
import html
import re
import threading
import time
import urllib.parse
import urllib.request

from rp_common import chat_localapi, chat_qwen

try:
    from rp_config import (IMAGE_VISION_PROVIDER as VISION_PROVIDER,
                           IMAGE_VISION_MODEL as VISION_MODEL,
                           IMAGE_VISION_CLOUD_MODEL as VISION_CLOUD_MODEL)
except Exception:
    VISION_PROVIDER = 'local-vision'
    VISION_MODEL = 'qwen-vl'
    VISION_CLOUD_MODEL = 'qwen3.8-flash'

# 缓存上限（条），防内存膨胀
_CACHE_MAX = 256
_cache = {}
_cache_lock = threading.Lock()


def _cached(key, value):
    """把结果塞进 LRU 缓存，返回 value。"""
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            # 简单淘汰：删最旧一半
            for k in list(_cache.keys())[:_CACHE_MAX // 2]:
                _cache.pop(k, None)
        _cache[key] = value
    return value


def _get_cache(key):
    with _cache_lock:
        return _cache.get(key)


# ---------------------------------------------------------------------------
# 图片识别
# ---------------------------------------------------------------------------

IMAGE_DESCRIBE_PROMPT = (
    '用一句话（不超过 40 字）客观描述这张图片的内容，'
    '重点说清画面里有什么、发生了什么。不要评价、不要脑补、不要问问题。'
    '如果是表情包/梗图，描述画面元素即可；如果是文字截图，复述关键文字。'
)

# 本地服务推荐 image_url 用 data URI：QQ 图片的临时 URL 可能带鉴权/防盗链，
# 由服务端去拉容易失败。这里先自己取回并转 base64；失败才把原 URL 交给服务端。
_IMG_MAX_BYTES = 4 * 1024 * 1024
_IMG_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')


def _as_data_uri(url, timeout=10):
    """图片 URL → data URI；已经是 data: 或取不到图片时原样返回。"""
    if url.startswith('data:'):
        return url
    try:
        req = urllib.request.Request(url, headers={'User-Agent': _IMG_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            data = resp.read(_IMG_MAX_BYTES + 1)
        if not ctype.startswith('image/') or len(data) > _IMG_MAX_BYTES:
            return url
        return 'data:%s;base64,%s' % (ctype, base64.b64encode(data).decode('ascii'))
    except Exception:
        return url


def describe_image(url, timeout=None):
    """识别一张图片的内容，返回一句话描述；失败返回 ''。

    url: 图片地址（QQ 图片的临时 url 或 data:image/...base64）。
    timeout: 覆盖默认超时（秒）。
    """
    url = (url or '').strip()
    if not url:
        return ''
    cached = _get_cache('img:' + url)
    if cached is not None:
        return cached
    try:
        image_url = _as_data_uri(url, timeout=timeout or 10)
        messages = [{
            'role': 'user',
            'content': [
                {'type': 'image_url', 'image_url': {'url': image_url}},
                {'type': 'text', 'text': IMAGE_DESCRIBE_PROMPT},
            ],
        }]
        if VISION_PROVIDER == 'qwen':
            out = chat_qwen(messages, VISION_CLOUD_MODEL,
                            temperature=0, max_tokens=80)
        else:
            out = chat_localapi(messages, VISION_MODEL, temperature=0, max_tokens=80,
                                timeout=timeout)
        out = (out or '').strip()
        if out:
            return _cached('img:' + url, out)
    except Exception:
        pass
    return ''


# ---------------------------------------------------------------------------
# 链接抓取
# ---------------------------------------------------------------------------

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')

# 匹配正文/标题里可能含 URL 的正则：http(s):// 开头，到空白/中文标点/引号截止
_URL_RE = re.compile(r'https?://[^\s\u4e00-\u9fff，。！？、；：""''<>（）()【】]+')

_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.S | re.I)
_SCRIPT_RE = re.compile(r'<script[^>]*>.*?</script>', re.S | re.I)
_STYLE_RE = re.compile(r'<style[^>]*>.*?</style>', re.S | re.I)
_TAG_RE = re.compile(r'<[^>]+>')


def extract_urls(text):
    """从一句话里提取所有 http(s) 链接（去重、保序）。"""
    return list(dict.fromkeys(m.group(0) for m in _URL_RE.finditer(text or '')))


def fetch_link(url, timeout=8):
    """抓取网页标题 + 正文摘要，返回 '标题｜摘要'；失败返回 ''。

    只抓文本类网页，图片/视频/大文件直接放弃。带超时与大小上限。
    """
    url = (url or '').strip()
    if not url:
        return ''
    if timeout is None:
        try:
            from rp_config import LINK_FETCH_TIMEOUT
            timeout = int(LINK_FETCH_TIMEOUT)
        except Exception:
            timeout = 8
    cached = _get_cache('link:' + url)
    if cached is not None:
        return cached
    try:
        req = urllib.request.Request(url, headers={'User-Agent': _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # 只看文本类；其它 content-type 直接放弃
            ctype = (resp.headers.get('Content-Type') or '').lower()
            if 'text/html' not in ctype and 'text/plain' not in ctype:
                return ''
            # 上限 512KB，防止拉回巨物
            data = resp.read(512 * 1024)
        charset = _guess_charset(data, resp.headers.get('Content-Type') or '')
        text = data.decode(charset, errors='replace')
    except Exception:
        return ''

    title = ''
    m = _TITLE_RE.search(text)
    if m:
        title = html.unescape(m.group(1)).strip()

    # 去脚本/样式/标签 → 纯文本。
    # 先 unescape 再清理标签：部分站点把 <style>/<script> 用 HTML 实体
    # （&lt;style&gt;）编码，不先还原就清不掉，样式会漏进正文（百度首页实测踩到）。
    body = html.unescape(text)
    body = _SCRIPT_RE.sub(' ', body)
    body = _STYLE_RE.sub(' ', body)
    body = _TAG_RE.sub(' ', body)
    body = html.unescape(body)
    body = re.sub(r'\s+', ' ', body).strip()

    # 摘要：取正文前 180 字（去掉标题本身在开头重复）
    summary = body[:220].strip()
    if title and summary.startswith(title):
        summary = summary[len(title):].strip(' ：:|-')
    if not title and not summary:
        return ''
    out = '、'.join(x for x in (title, summary[:180]) if x).strip()
    return _cached('link:' + url, out) if out else ''


def _guess_charset(data, ctype):
    """从 content-type 或 <meta> 里猜编码，默认 utf-8。"""
    m = re.search(r'charset=([\w\-]+)', ctype, re.I)
    if m:
        return m.group(1)
    head = data[:2048].decode('ascii', errors='ignore')
    m = re.search(r'charset=["\']?([\w\-]+)', head, re.I)
    if m:
        return m.group(1)
    return 'utf-8'
