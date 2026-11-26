# -*- coding: utf-8 -*-
"""书库文件解析：EPUB 复用 novel_index，PDF 只取文字层。

扫描版 PDF（整页图片、没有文字层）读不出字，直接标 no_text_layer 跳过，
不引入 OCR，避免 4:00 初始化被拖死。
"""
import os
import posixpath
import re
import zipfile
from urllib.parse import unquote

import novel_index

SUPPORTED = ('.epub', '.pdf')

IMAGE_MIME = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
              '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp'}
MAX_COVER_BYTES = 12 * 1024 * 1024
MIN_CHARS = 500          # 少于这么多字就当没解析出正文
MIN_PAGE_CHARS = 20      # PDF 单页少于这么多字视为插图页
MAX_PAGES = 2000


def _clean(text):
    text = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\u3000', ' ').replace('\xa0', ' ')
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    lines = [line.strip() for line in text.split('\n')]
    return '\n'.join(line for line in lines if line)


def _lang_of(texts):
    """粗判语种：假名 → ja；拉丁字母远多于汉字 → en；否则 zh。"""
    sample = ''.join(texts)[:4000]
    kana = len(re.findall(r'[\u3040-\u30ff]', sample))
    han = len(re.findall(r'[\u4e00-\u9fff]', sample))
    latin = len(re.findall(r'[A-Za-z]', sample))
    if kana and kana * 4 >= han:
        return 'ja'
    if latin > max(han, 1) * 2:
        return 'en'
    return 'zh'


def _epub_meta(path):
    title = os.path.splitext(os.path.basename(path))[0]
    author = ''
    lang = ''
    try:
        with zipfile.ZipFile(path) as z:
            opfs = [n for n in z.namelist() if n.lower().endswith('.opf')]
            if not opfs:
                return title, author, lang
            raw = z.read(opfs[0]).decode('utf-8', 'ignore')
            m = re.search(r'<dc:title[^>]*>(.*?)</dc:title>', raw, re.S | re.I)
            if m:
                title = re.sub(r'<[^>]+>', '', m.group(1)).strip() or title
            m = re.search(r'<dc:creator[^>]*>(.*?)</dc:creator>', raw, re.S | re.I)
            if m:
                author = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            m = re.search(r'<dc:language[^>]*>(.*?)</dc:language>', raw, re.S | re.I)
            if m:
                lang = re.sub(r'<[^>]+>', '', m.group(1)).strip().lower()[:5]
    except Exception:
        pass
    return title, author, lang


def _pdf_meta(doc):
    meta = getattr(doc, 'metadata', None) or {}
    title = str(meta.get('title') or '').strip()
    author = str(meta.get('author') or '').strip()
    return title, author


def extract(path):
    """返回 {ok, kind, title, author, chapters:[(title,text)], chars, reason}。"""
    lower = path.lower()
    if lower.endswith('.epub'):
        return _extract_epub(path)
    if lower.endswith('.pdf'):
        return _extract_pdf(path)
    return {'ok': False, 'kind': '', 'title': os.path.basename(path),
            'author': '', 'chapters': [], 'chars': 0, 'reason': 'unsupported'}


def _extract_epub(path):
    title, author, lang = _epub_meta(path)
    chapters = []
    for chapter, text in novel_index.epub_chapters(path):
        text = _clean(text)
        if len(text) >= 40:
            chapters.append((chapter or title, text))
    chars = sum(len(t) for _, t in chapters)
    lang = lang or _lang_of([t for _, t in chapters])
    if chars < MIN_CHARS:
        return {'ok': False, 'kind': 'epub', 'title': title, 'author': author,
                'lang': lang, 'chapters': [], 'chars': chars,
                'reason': 'empty_text'}
    return {'ok': True, 'kind': 'epub', 'title': title, 'author': author,
            'lang': lang, 'chapters': chapters, 'chars': chars, 'reason': ''}


def _extract_pdf(path):
    try:
        import fitz
    except ImportError:
        return {'ok': False, 'kind': 'pdf',
                'title': os.path.splitext(os.path.basename(path))[0],
                'author': '', 'chapters': [], 'chars': 0,
                'reason': 'pymupdf_missing'}
    fallback = os.path.splitext(os.path.basename(path))[0]
    chapters = []
    skipped = 0
    try:
        doc = fitz.open(path)
    except Exception as e:
        return {'ok': False, 'kind': 'pdf', 'title': fallback, 'author': '',
                'chapters': [], 'chars': 0, 'reason': 'open_failed:%s' % e}
    raw_meta = {}
    try:
        raw_meta = dict(getattr(doc, 'metadata', None) or {})
        title, author = _pdf_meta(doc)
        title = title or fallback
        for i, page in enumerate(doc):
            if i >= MAX_PAGES:
                break
            try:
                text = _clean(page.get_text('text'))
            except Exception:
                text = ''
            if len(text) < MIN_PAGE_CHARS:
                skipped += 1
                continue
            chapters.append(('第%d页' % (i + 1), text))
    finally:
        doc.close()
    chars = sum(len(t) for _, t in chapters)
    lang = str(raw_meta.get('lang') or '').lower()[:5] \
        or _lang_of([t for _, t in chapters])
    if chars < MIN_CHARS:
        return {'ok': False, 'kind': 'pdf', 'title': title, 'author': author,
                'lang': lang, 'chapters': [], 'chars': chars,
                'reason': 'no_text_layer' if skipped else 'empty_text'}
    return {'ok': True, 'kind': 'pdf', 'title': title, 'author': author,
            'lang': lang, 'chapters': chapters, 'chars': chars,
            'reason': 'scanned_pages:%d' % skipped if skipped else ''}


# ---------- 封面 ----------

def _cover_mime(data, name=''):
    """按文件头认图片，认不出再看扩展名；SVG 之类一律不认。"""
    head = (data or b'')[:12]
    if head[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if head[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if head[:2] == b'BM':
        return 'image/bmp'
    return IMAGE_MIME.get(os.path.splitext(str(name or ''))[1].lower(), '')


def _zip_member(z, name):
    name = posixpath.normpath(str(name or '').lstrip('/').split('#')[0])
    if not name or name.startswith('..'):
        return b''

    def read_member(member):
        try:
            if z.getinfo(member).file_size > MAX_COVER_BYTES:
                return b''
            return z.read(member)
        except (KeyError, OSError):
            return b''

    low = name.lower()
    for n in z.namelist():
        if n.lower() == low:
            return read_member(n)
    return b''


def _opf_name(z):
    try:
        raw = z.read('META-INF/container.xml').decode('utf-8', 'ignore')
        m = re.search(r'full-path\s*=\s*"([^"]+)"', raw)
        if m:
            return unquote(m.group(1))
    except KeyError:
        pass
    opfs = [n for n in z.namelist() if n.lower().endswith('.opf')]
    return opfs[0] if opfs else ''


def _epub_cover(path):
    try:
        with zipfile.ZipFile(path) as z:
            opf = _opf_name(z)
            if not opf:
                return b'', ''
            raw = z.read(opf).decode('utf-8', 'ignore')
            opf_dir = posixpath.dirname(opf)
            items = {}
            for m in re.finditer(r'<item\b([^>]*?)/?>', raw, re.S):
                a = dict(re.findall(r'([\w:.\-]+)\s*=\s*"([^"]*)"', m.group(1)))
                if a.get('href'):
                    items[a.get('id') or a['href']] = a
            order = []
            for iid, a in items.items():
                if 'cover-image' in (a.get('properties') or ''):
                    order.append(iid)
            for m in re.finditer(r'<meta\b([^>]*?)/?>', raw, re.S):
                a = dict(re.findall(r'([\w:.\-]+)\s*=\s*"([^"]*)"', m.group(1)))
                if (a.get('name') or '').lower() == 'cover' and a.get('content') in items:
                    order.append(a['content'])
            for iid, a in items.items():
                href = (a.get('href') or '').lower()
                if 'cover' in iid.lower() or 'cover' in href:
                    order.append(iid)
            for iid, a in items.items():
                if (a.get('media-type') or '').startswith('image/'):
                    order.append(iid)
            for iid in dict.fromkeys(order):
                href = (items.get(iid) or {}).get('href') or ''
                if not href:
                    continue
                data = _zip_member(z, posixpath.join(opf_dir, unquote(href)))
                mime = _cover_mime(data, href)
                if data and mime and len(data) <= MAX_COVER_BYTES:
                    return data, mime
    except (OSError, KeyError, zipfile.BadZipFile):
        pass
    return b'', ''


def _pdf_cover(path):
    try:
        import fitz
    except ImportError:
        return b'', ''
    try:
        doc = fitz.open(path)
        try:
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False)
            data = pix.tobytes('png')
        finally:
            doc.close()
        return (data, 'image/png') if data else (b'', '')
    except Exception:
        return b'', ''


def cover(path):
    """取封面图：EPUB 读清单里的封面项，PDF 渲第一页；失败返回 (b'', '')。"""
    lower = str(path or '').lower()
    if lower.endswith('.epub'):
        return _epub_cover(path)
    if lower.endswith('.pdf'):
        return _pdf_cover(path)
    return b'', ''
