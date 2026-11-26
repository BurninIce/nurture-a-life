# -*- coding: utf-8 -*-
"""构建原作小说检索索引（离线，一次性；不联网）。

    python scripts/novel_index.py --build
    python scripts/novel_index.py --build --only 第01卷 --limit 1   # 试跑
    python scripts/novel_index.py --stats
    python scripts/novel_index.py --query "狂三第一次出现在什么时候"

产物（默认 state/personas/<id>/novel_index/，gitignore）：
    chunks.jsonl / embeddings.npy / meta.json

为什么要分块 + embedding 而不是关键词：
用户问的是「原作里那件事是怎样的」，关键词匹配只能命中人名，命不中情节；
bge-m3 已经在本项目的 RAG/记忆检索里跑着，复用它不增加依赖。
"""
import argparse
import json
import os
import re
import sys
import time
import zipfile
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import persona

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'Raw Materials', 'novels')
_SKIP_TAGS = {'script', 'style', 'rt', 'rp', 'head'}
_BLOCK_TAGS = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr'}
_VOL_RE = re.compile(r'第\s*(\d+)\s*卷')
# 这些页面是 epub 的排版垃圾，不是情节，进索引只会污染检索
_JUNK_TITLES = {'制作信息', '简介', 'CONTENTS', '目录', '版权', '版权页',
                '书名页', '封面', '插图'}


class _TextExtractor(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self.skip:
            self.skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(raw):
    p = _TextExtractor()
    try:
        p.feed(raw)
        p.close()
    except Exception:
        pass
    text = ''.join(p.parts).replace('\xa0', ' ')
    lines = []
    for line in text.splitlines():
        line = re.sub(r'[ \t]+', ' ', line).strip()
        if line:
            lines.append(line)
    return '\n'.join(lines)


def _opf_spine(z, names):
    """按 OPF spine 顺序返回 xhtml 路径；解析失败则按文件名排序兜底。"""
    opfs = [n for n in names if n.lower().endswith('.opf')]
    if not opfs:
        return sorted(n for n in names if n.lower().endswith(('.xhtml', '.html', '.htm')))
    opf = opfs[0]
    base = os.path.dirname(opf)
    try:
        raw = z.read(opf).decode('utf-8', 'ignore')
    except Exception:
        return []
    # 属性顺序不固定（这套书是 href 在 id 前面），逐个 item 抽属性更稳
    items = {}
    for m in re.finditer(r'<item\b([^>]*)>', raw):
        attrs = dict(re.findall(r'([\w:]+)\s*=\s*"([^"]*)"', m.group(1)))
        if attrs.get('id') and attrs.get('href'):
            items[attrs['id']] = attrs['href']
    order = re.findall(r'<itemref\s+[^>]*?idref\s*=\s*"([^"]+)"', raw)
    out = []
    for ref in order:
        href = items.get(ref)
        if not href:
            continue
        out.append(os.path.normpath(os.path.join(base, href)).replace('\\', '/'))
    return out or sorted(n for n in names if n.lower().endswith(('.xhtml', '.html', '.htm')))


def epub_chapters(path):
    """返回 [(chapter, text)]；只取有正文的章节。"""
    chapters = []
    try:
        z = zipfile.ZipFile(path)
    except Exception as e:
        print('  打开失败 %s: %s' % (os.path.basename(path), e))
        return chapters
    with z:
        names = z.namelist()
        for name in _opf_spine(z, names):
            if name not in names:
                continue
            try:
                raw = z.read(name).decode('utf-8', 'ignore')
            except Exception:
                continue
            text = html_to_text(raw)
            if len(text) < 80:
                continue
            title = text.split('\n', 1)[0][:40]
            if title.strip() in _JUNK_TITLES:
                continue
            chapters.append((title, text))
    return chapters


def classify(path):
    p = path.replace('\\', '/')
    if '外传' in p:
        return 'gaiden'
    if '安可' in p:
        return 'anco'
    return 'main'


def volume_of(path):
    m = _VOL_RE.search(os.path.basename(path))
    if m:
        return '%02d' % int(m.group(1))
    return os.path.splitext(os.path.basename(path))[0][:20]


def chunk_text(text, size=400, overlap=80):
    """按段落装箱到 ~size 字，段间留 overlap 字重叠，避免情节被切断。"""
    paras = [p for p in text.split('\n') if p]
    out, buf = [], ''
    for p in paras:
        if buf and len(buf) + len(p) + 1 > size:
            out.append(buf.strip())
            buf = (buf[-overlap:] if overlap else '') + p
        else:
            buf = (buf + '\n' + p) if buf else p
    if buf.strip():
        out.append(buf.strip())
    return [c for c in out if len(c) >= 40]


def find_epubs(root, only=''):
    out = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            if not name.lower().endswith('.epub'):
                continue
            full = os.path.join(dirpath, name)
            if only and only not in full:
                continue
            out.append(full)
    return sorted(out)


def _post_json(url, payload, timeout=180):
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8'))


def embed_texts(texts, model):
    """批量 embedding（Ollama /api/embed）。返回 list[list[float]|None]。"""
    from embedding import OLLAMA_URL
    vecs = []
    batch = 32
    for start in range(0, len(texts), batch):
        part = texts[start:start + batch]
        if start and start % (batch * 10) == 0:
            print('  … %d/%d' % (start, len(texts)))
        try:
            resp = _post_json(OLLAMA_URL, {'model': model, 'input': part})
            got = resp.get('embeddings') or []
            if len(got) != len(part):
                got = [None] * len(part)
        except Exception as e:
            print('  embedding 批次 %d 失败: %s' % (start, e))
            got = [None] * len(part)
        vecs.extend(got)
    return vecs


def build(root=_ROOT, out_dir=None, only='', limit=0, model=None,
          size=400, overlap=80):
    out_dir = out_dir or persona.novel_index_dir()
    model = model or __import__('embedding').EMBED_MODEL
    epubs = find_epubs(root, only)
    if limit:
        epubs = epubs[:limit]
    if not epubs:
        print('没有找到 epub：root=%s only=%r' % (root, only))
        return 1

    rows, texts, sources = [], [], []
    for i, path in enumerate(epubs, 1):
        line = classify(path)
        volume = volume_of(path)
        chapters = epub_chapters(path)
        n = 0
        for chapter, text in chapters:
            for piece in chunk_text(text, size, overlap):
                texts.append(piece)
                rows.append({'i': len(rows), 'line': line, 'volume': volume,
                             'chapter': chapter, 'text': piece})
                n += 1
        sources.append({'path': os.path.relpath(path, root), 'line': line,
                        'volume': volume, 'chunks': n})
        print('[%d/%d] %s → %d 块' % (i, len(epubs), sources[-1]['path'], n))

    print('共 %d 块，开始 embedding（%s）…' % (len(texts), model))
    t0 = time.time()
    vecs = embed_texts(texts, model)
    missing = sum(1 for v in vecs if not v)
    keep = [k for k, v in enumerate(vecs) if v]
    if not keep:
        print('全部 embedding 失败：检查 Ollama 是否在跑（%s）' % model)
        return 1
    if missing:
        print('警告：%d 块没有向量，已跳过' % missing)

    import numpy as np
    mat = np.asarray([vecs[k] for k in keep], dtype='float32')
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'chunks.jsonl'), 'w', encoding='utf-8') as f:
        for k in keep:
            f.write(json.dumps(rows[k], ensure_ascii=False) + '\n')
    np.save(os.path.join(out_dir, 'embeddings.npy'), mat)
    meta = {'built_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'model': model,
            'dim': int(mat.shape[1]), 'count': len(keep), 'chunks': len(rows),
            'missing': missing, 'size': size, 'overlap': overlap,
            'sources': sources}
    with open(os.path.join(out_dir, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print('完成：%d 块 / %d 维 / %.1fs → %s' % (len(keep), mat.shape[1],
                                             time.time() - t0, out_dir))
    return 0


def show_stats(out_dir=None):
    out_dir = out_dir or persona.novel_index_dir()
    meta_path = os.path.join(out_dir, 'meta.json')
    if not os.path.exists(meta_path):
        print('索引不存在：%s' % out_dir)
        return 1
    with open(meta_path, encoding='utf-8') as f:
        meta = json.load(f)
    print('构建时间 %s  模型 %s  %d 维  %d 块（失败 %d）' % (
        meta.get('built_at'), meta.get('model'), meta.get('dim', 0),
        meta.get('count', 0), meta.get('missing', 0)))
    by_line = {}
    for s in meta.get('sources', []):
        key = s.get('line')
        by_line[key] = by_line.get(key, 0) + s.get('chunks', 0)
    for key, n in sorted(by_line.items()):
        print('  %-8s %d 块' % (key, n))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--build', action='store_true')
    ap.add_argument('--stats', action='store_true')
    ap.add_argument('--query', default='')
    ap.add_argument('--root', default=_ROOT)
    ap.add_argument('--out', default='')
    ap.add_argument('--only', default='')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--model', default='')
    ap.add_argument('--size', type=int, default=400)
    ap.add_argument('--overlap', type=int, default=80)
    ap.add_argument('--top', type=int, default=3)
    args = ap.parse_args(argv)

    out_dir = args.out or persona.novel_index_dir()
    if args.build:
        return build(args.root, out_dir, args.only, args.limit, args.model or None,
                     args.size, args.overlap)
    if args.stats:
        return show_stats(out_dir)
    if args.query:
        import novel_rag
        novel_rag.set_dir(out_dir)
        rows = novel_rag.search(args.query, top_k=args.top)
        if not rows:
            print('没有命中（索引不存在或相似度过低）')
            return 1
        for r in rows:
            print('%.3f  [%s %s %s] %s' % (r['score'], r['line'], r['volume'],
                                           r['chapter'], r['text'][:80]))
        return 0
    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
