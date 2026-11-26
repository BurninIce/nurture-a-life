# -*- coding: utf-8 -*-
"""书架：本地书库进 未读 / 在读 / 已读，按日程阅读并留下记录。

数据流：
    tools/bookshelf/library/  ← 用户放 epub / pdf
    state/bookshelf/          ← 全文缓存、目录、向量索引（全局，按内容哈希去重）
    state/personas/<id>/bookshelf.json      ← 每个角色自己的未读/在读/已读
    state/personas/<id>/bookshelf_log.jsonl ← 每次阅读的笔记，日记与记忆事件的素材

容量由 rp_config 决定：未读 6、在读 3、未读降到 3 及以下补货；
未读连续 7 次补货没被移入在读就剔除。正文只落本地 state，不进聊天注入。
"""
import hashlib
import json
import logging
import os
import random
import re
import threading
import time

import bookshelf_model
import bookshelf_text
import day_clock
import day_life
import persona
import rp_config as cfg
import self_state

LOG = logging.getLogger('bookshelf')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READING_WORDS = ('读书', '看书', '阅读', '翻书', '读几页', '看几页', '读一会', '看一会')

_LOCK = threading.RLock()
_TASK_LOCK = threading.Lock()
_COVER_LOCK = threading.Lock()
_FAIL_LOCK = threading.Lock()
_FAIL_UNTIL = 0.0
_CACHE = {}
_OVERRIDE = None
_LIB_OVERRIDE = None
_CACHE_OVERRIDE = None
_LOG_OVERRIDE = None


# ---------- 开关 ----------

def enabled():
    """书架总开关：运行配置优先，回退代码默认（默认关）。"""
    try:
        import runtime_config as rc
        return bool(rc.get('bookshelf_enabled',
                           getattr(cfg, 'BOOKSHELF_ENABLED', False)))
    except Exception:
        return bool(getattr(cfg, 'BOOKSHELF_ENABLED', False))


# ---------- 路径 ----------

def set_paths(state=None, library=None, cache=None, log=None):
    """测试用：覆盖状态文件、书库目录、缓存与日志；传 None 恢复默认。"""
    global _OVERRIDE, _LIB_OVERRIDE, _CACHE_OVERRIDE, _LOG_OVERRIDE
    with _LOCK:
        _OVERRIDE = state or None
        _LIB_OVERRIDE = library or None
        _CACHE_OVERRIDE = cache or None
        _LOG_OVERRIDE = log or None
        _CACHE.clear()


def library_dir():
    return (_LIB_OVERRIDE
            or getattr(cfg, 'BOOKSHELF_LIBRARY_DIR', '')
            or os.path.join(ROOT, 'tools', 'bookshelf', 'library'))


def cache_dir():
    return _CACHE_OVERRIDE or os.path.join(ROOT, 'state', 'bookshelf')


def text_dir():
    return os.path.join(cache_dir(), 'text')


def index_dir():
    return os.path.join(cache_dir(), 'index')


def covers_dir():
    return os.path.join(cache_dir(), 'covers')


def state_path(pid=None):
    return _OVERRIDE or os.path.join(persona.state_root(pid), 'bookshelf.json')


def log_path(pid=None):
    return _LOG_OVERRIDE or os.path.join(persona.state_root(pid),
                                         'bookshelf_log.jsonl')


# ---------- 状态 ----------

def _empty():
    return {'version': 1, 'books': {}, 'refill_seq': 0, 'last_init_day': '',
            'init_at': 0.0, 'last_refill_at': 0.0, 'slots': {},
            'finished_day': '', 'finished_count': 0}


def _load(pid=None):
    path = state_path(pid)
    with _LOCK:
        if path in _CACHE:
            return _CACHE[path]
        data = _empty()
        try:
            with open(path, encoding='utf-8') as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for key, default in data.items():
                    value = saved.get(key)
                    if isinstance(value, type(default)):
                        data[key] = value
                if isinstance(saved.get('books'), dict):
                    data['books'] = saved['books']
        except (OSError, ValueError):
            pass
        _CACHE[path] = data
        return data


def _save(data=None, pid=None):
    data = data if data is not None else _load(pid)
    path = state_path(pid)
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        _CACHE[path] = data
        return True
    except OSError:
        LOG.exception('书架状态写盘失败：%s', path)
        return False


# ---------- 目录与全文缓存 ----------

def _catalog_path():
    return os.path.join(cache_dir(), 'catalog.json')


def load_catalog():
    try:
        with open(_catalog_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_catalog(cat):
    os.makedirs(cache_dir(), exist_ok=True)
    tmp = _catalog_path() + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cat, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _catalog_path())


def _sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()[:16]


def _write_text(hid, chapters):
    os.makedirs(text_dir(), exist_ok=True)
    with open(os.path.join(text_dir(), hid + '.jsonl'), 'w',
              encoding='utf-8') as f:
        for title, text in chapters:
            f.write(json.dumps({'title': title, 'text': text},
                               ensure_ascii=False) + '\n')
    with open(os.path.join(text_dir(), hid + '.txt'), 'w',
              encoding='utf-8') as f:
        f.write('\n\n'.join(text for _, text in chapters))


def read_text(hid):
    try:
        with open(os.path.join(text_dir(), hid + '.txt'), encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ''


_HID_RE = re.compile(r'^[0-9a-f]{16}$')
_COVER_EXT = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif',
              'image/webp': '.webp', 'image/bmp': '.bmp'}


def cover_file(hid):
    """已缓存的封面路径；没有返回空串。"""
    hid = str(hid or '')
    if not _HID_RE.match(hid):
        return ''
    for ext in dict.fromkeys(_COVER_EXT.values()):
        path = os.path.join(covers_dir(), hid + ext)
        if os.path.isfile(path):
            return path
    return ''


def ensure_cover(hid, row=None):
    """按需生成封面并缓存；书里确实没有封面时落 .none 标记，避免反复解析。"""
    hid = str(hid or '')
    if not _HID_RE.match(hid):
        return ''
    hit = cover_file(hid)
    if hit:
        return hit
    marker = os.path.join(covers_dir(), hid + '.none')
    if os.path.isfile(marker):
        return ''
    row = row or load_catalog().get(hid) or {}
    rel = str(row.get('path') or '').replace('\\', '/')
    if not rel or not row.get('ok'):
        return ''
    root = os.path.realpath(library_dir())
    src = os.path.realpath(os.path.join(root, *rel.split('/')))
    try:
        if os.path.commonpath([root, src]) != root:
            return ''
    except ValueError:
        return ''
    if not os.path.isfile(src):
        return ''
    with _COVER_LOCK:
        hit = cover_file(hid)
        if hit or os.path.isfile(marker):
            return hit
        data, mime = bookshelf_text.cover(src)
        os.makedirs(covers_dir(), exist_ok=True)
        if not data or not mime:
            try:
                open(marker, 'wb').close()
            except OSError:
                pass
            return ''
        target = os.path.join(covers_dir(), hid + _COVER_EXT.get(mime, '.img'))
        tmp = target + '.tmp'
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, target)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return ''
        return target


# ---------- 系列与阅读顺序 ----------

SERIES_MODES = ('none', 'partial', 'ordered')

_LINE_WORDS = (('外传', '外传'), ('外傳', '外传'), ('插话集', '插话集'),
               ('插話集', '插话集'), ('番外', '番外'), ('安可', '安可'))
_NUM_PATTERNS = (
    re.compile(r'第\s*(\d+)\s*[卷集弹篇部]'),
    re.compile(r'(?:外传|外傳|番外)\s*(\d+)'),
    re.compile(r'卷\s*(\d+)'),
    re.compile(r'vol\.?\s*(\d+)', re.I),
    re.compile(r'^\s*0*(\d{1,3})(?=[\s._、\-（(]|$)'),
    re.compile(r'(\d{1,3})\s*$'),
)


def series_path():
    return os.path.join(cache_dir(), 'series.json')


def _load_series():
    try:
        with open(series_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def series_modes():
    """系列标注：{系列文件夹: none|partial|ordered}，没有标注的按无序。"""
    rows = _load_series().get('series')
    out = {}
    for key, val in (rows or {}).items():
        mode = (val or {}).get('mode') if isinstance(val, dict) else None
        if mode in SERIES_MODES:
            out[str(key)] = mode
    return out


def catalog_series():
    """按书库第一层文件夹分组：{系列: [(hid, 目录行), ...]}。"""
    out = {}
    for hid, row in load_catalog().items():
        key = series_key(row)
        if key:
            out.setdefault(key, []).append((hid, row))
    return out


def series_key(row):
    rel = str((row or {}).get('path') or '').replace('\\', '/')
    return rel.split('/', 1)[0] if '/' in rel else ''


def set_series_mode(key, mode, now=None):
    key = str(key or '').strip()
    mode = str(mode or '')
    if mode not in SERIES_MODES:
        raise ValueError('未知标注')
    if key not in catalog_series():
        raise ValueError('没有这个系列')
    data = _load_series()
    rows = data.get('series')
    if not isinstance(rows, dict):
        rows = data['series'] = {}
    rows[key] = {'mode': mode,
                 'updated_at': time.time() if now is None else float(now)}
    os.makedirs(cache_dir(), exist_ok=True)
    tmp = series_path() + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, series_path())
    return mode


def _first_num(text):
    for pat in _NUM_PATTERNS:
        m = pat.search(str(text or ''))
        if m:
            return int(m.group(1))
    return None


def order_info(row):
    """书的系列内位置：line 为番外分线，line_num 取文件名，seq_num 优先书名序号。"""
    name = os.path.basename(str((row or {}).get('path') or '').replace('\\', '/'))
    stem = os.path.splitext(name)[0]
    title = str((row or {}).get('title') or '')
    line = ''
    for word, key in _LINE_WORDS:
        if word in stem:
            line = key
            break
    line_num = _first_num(stem)
    seq_num = _first_num(title)
    return {'line': line, 'line_num': line_num,
            'seq_num': seq_num if seq_num is not None else line_num}


def order_ids(ids, rows=None):
    """在同一系列的槽位内按卷序重排：有序系列整组，部分有序按分线各排。"""
    ids = list(ids)
    modes = series_modes()
    if not modes or not ids:
        return ids
    rows = rows if rows is not None else load_catalog()
    groups = {}
    for hid in ids:
        row = rows.get(hid) or {}
        key = series_key(row)
        mode = modes.get(key)
        if not key or mode not in ('ordered', 'partial'):
            continue
        info = order_info(row)
        if mode == 'ordered':
            groups.setdefault((key, ''), []).append((hid, info['seq_num']))
        elif info['line_num'] is not None:
            groups.setdefault((key, info['line']),
                              []).append((hid, info['line_num']))
    if not groups:
        return ids
    pos = {hid: i for i, hid in enumerate(ids)}
    out = list(ids)
    for members in groups.values():
        if len(members) < 2:
            continue
        slots = sorted(pos[hid] for hid, _ in members)
        members.sort(key=lambda x: (
            x[1] is None, x[1] if x[1] is not None else 0,
            str((rows.get(x[0]) or {}).get('title') or '')))
        for slot, (hid, _) in zip(slots, members):
            out[slot] = hid
    return out


def _line_key(row, mode):
    info = order_info(row)
    if mode == 'ordered':
        return '', info['seq_num']
    if info['line_num'] is None:
        return None, None
    return info['line'], info['line_num']


def series_next(st, key, line, mode, cat=None, now=None):
    """已读/在读最远处之后紧挨着的一本：{id, on_shelf}，没有返回 None。

    有序系列看整条序列，部分有序只看同一分线；还没有已读/在读时从第一本起。
    已移出且还在冷却的书跳过，顺延到下一本。
    """
    now = time.time() if now is None else float(now)
    cat = cat if cat is not None else load_catalog()
    books = st.get('books') or {}
    rows = {}
    for hid, row in cat.items():
        if series_key(row) != key or not row.get('ok'):
            continue
        line_name, pos = _line_key(row, mode)
        if pos is None or (mode == 'partial' and line_name != line):
            continue
        rows[hid] = pos
    if not rows:
        return None
    anchor = 0
    for hid, pos in rows.items():
        if (books.get(hid) or {}).get('status') in ('reading', 'finished'):
            anchor = max(anchor, pos)
    for pos, hid in sorted((pos, hid) for hid, pos in rows.items() if pos > anchor):
        status = (books.get(hid) or {}).get('status')
        if status in ('unread', 'reading', 'finished', 'abandoned'):
            return {'id': hid, 'on_shelf': True}
        if (status == 'removed'
                and float((books.get(hid) or {}).get('cooldown_until') or 0) > now):
            continue
        return {'id': hid, 'on_shelf': False}
    return None


def _normalize_chosen(st, cat, chosen, cands, now=None, want=None):
    """补货挑到后卷时，换成紧挨着已读/在读位置的下一卷；一条线只留一本。

    want 给定时，空出的位置从 cands 里补到 want 本，模型挑少了也能补足缺口。
    """
    modes = series_modes()
    now = time.time() if now is None else float(now)
    chosen = list(chosen or [])
    target = max(len(chosen), int(want or 0))
    groups = {}
    if modes:
        for hid in chosen:
            row = cat.get(hid) or {}
            key = series_key(row)
            mode = modes.get(key)
            if mode not in ('ordered', 'partial'):
                continue
            line, pos = _line_key(row, mode)
            if pos is None:
                continue
            groups.setdefault((key, line, mode), []).append(hid)
    drop, replace = set(), {}
    for (key, line, mode), members in groups.items():
        nxt = series_next(st, key, line, mode, cat, now)
        if not nxt:
            drop.update(members[1:])
        elif nxt['on_shelf']:
            drop.update(members)
        elif nxt['id'] in members:
            drop.update(hid for hid in members if hid != nxt['id'])
        else:
            replace[members[0]] = nxt['id']
            drop.update(members[1:])
    out = [replace.get(hid, hid) for hid in chosen if hid not in drop]
    for old, new in replace.items():
        LOG.info('系列顺读：《%s》换成《%s》', old, new)
    if len(out) >= target:
        return out
    taken = set(out)
    lines = {(key, line) for (key, line, _mode) in groups}
    for row in cands:
        if len(out) >= target:
            break
        hid = row.get('id')
        if not hid or hid in taken:
            continue
        key = series_key(row)
        mode = modes.get(key)
        add = hid
        if mode in ('ordered', 'partial'):
            line, pos = _line_key(row, mode)
            if pos is None or (key, line) in lines:
                continue
            nxt = series_next(st, key, line, mode, cat, now)
            if not nxt or nxt['on_shelf'] or nxt['id'] in taken:
                continue
            add = nxt['id']
            lines.add((key, line))
        out.append(add)
        taken.add(add)
    return out


def _scan_files():
    root = library_dir()
    os.makedirs(root, exist_ok=True)
    out = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            if name.lower().endswith(bookshelf_text.SUPPORTED):
                out.append(os.path.join(dirpath, name))
    return sorted(out)


RETRY_REASONS = ('pymupdf_missing',)


def _skip_cached(old, size):
    """同一份内容已处理过就不重复解析。

    失败的文件按原因区分：缺依赖、打开失败这类换环境可能变好的允许重试；
    扫描版、格式不支持这类确定性的不再反复消耗。
    """
    if not old or old.get('size') != size:
        return False
    if old.get('ok'):
        return True
    reason = str(old.get('reason') or '')
    return not (reason.startswith('open_failed') or reason in RETRY_REASONS)


def ingest(limit=None, now=None):
    """扫描书库，解析新文件。返回 {added, skipped, failed, total}。"""
    now = time.time() if now is None else float(now)
    root = library_dir()
    files = _scan_files()
    cat = load_catalog()
    added, failed, skipped = [], [], 0
    limit = int(limit or 0)
    for path in files:
        if limit and len(added) >= limit:
            break
        try:
            size = os.path.getsize(path)
            hid = _sha1(path)
            mtime = os.path.getmtime(path)
        except OSError as e:
            failed.append({'path': path, 'reason': str(e)})
            continue
        old = cat.get(hid) or {}
        if _skip_cached(old, size):
            skipped += 1
            continue
        res = bookshelf_text.extract(path)
        row = {'title': res.get('title') or os.path.basename(path),
               'author': res.get('author') or '', 'lang': res.get('lang') or '',
               'kind': res.get('kind') or '', 'chars': int(res.get('chars') or 0),
               'ok': bool(res.get('ok')), 'reason': res.get('reason') or '',
               'path': os.path.relpath(path, root).replace('\\', '/'),
               'size': size, 'mtime': mtime,
               'added_at': now, 'indexed': False}
        if res.get('ok'):
            _write_text(hid, res['chapters'])
            added.append(hid)
            LOG.info('书库入库：%s（%s，%d 字）', row['title'], row['kind'], row['chars'])
        else:
            LOG.info('书库跳过：%s（%s）', row['title'], row['reason'])
        cat[hid] = row
    _save_catalog(cat)
    if added:
        try:
            build_index(added, now=now)
        except Exception:
            LOG.exception('书架索引构建失败（不影响入库）')
    bad = [k for k, v in cat.items() if not v.get('ok')]
    return {'added': added, 'skipped': skipped, 'failed': failed,
            'bad': bad, 'total': len(cat)}


# ---------- 向量索引 ----------

def _index_paths():
    return (os.path.join(index_dir(), 'ids.json'),
            os.path.join(index_dir(), 'vectors.npy'))


def build_index(ids=None, now=None):
    """给新书建一条向量（标题+作者+开头），用于按口味检索候选。"""
    import numpy as np
    from embedding import embed
    cat = load_catalog()
    ids_path, vec_path = _index_paths()
    old_ids, old_mat = [], None
    try:
        with open(ids_path, encoding='utf-8') as f:
            old_ids = json.load(f)
        old_mat = np.load(vec_path)
        if len(old_ids) != len(old_mat):
            old_ids, old_mat = [], None
    except (OSError, ValueError, ImportError):
        old_ids, old_mat = [], None
    want = [k for k in (ids or cat.keys())
            if (cat.get(k) or {}).get('ok') and k not in old_ids]
    fresh_ids, fresh_vecs = [], []
    for hid in want:
        row = cat[hid]
        text = read_text(hid)[:1200]
        query = '%s %s\n%s' % (row.get('title') or '', row.get('author') or '', text)
        vec = embed(query)
        if vec:
            fresh_ids.append(hid)
            fresh_vecs.append(vec)
            row['indexed'] = True
    if not fresh_ids:
        if old_ids:
            _save_catalog(cat)
        return 0
    mat = np.asarray(fresh_vecs, dtype='float32')
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    if old_mat is not None and len(old_ids):
        mat = np.vstack([old_mat, mat])
        old_ids = list(old_ids) + fresh_ids
    else:
        old_ids = fresh_ids
    os.makedirs(index_dir(), exist_ok=True)
    np.save(vec_path, mat)
    with open(ids_path, 'w', encoding='utf-8') as f:
        json.dump(old_ids, f)
    _save_catalog(cat)
    return len(fresh_ids)


def _search_catalog(query, k=16, exclude=None):
    import numpy as np
    cat = load_catalog()
    ids_path, vec_path = _index_paths()
    try:
        with open(ids_path, encoding='utf-8') as f:
            ids = json.load(f)
        mat = np.load(vec_path)
    except (OSError, ValueError, ImportError):
        return []
    if not ids or len(ids) != len(mat):
        return []
    from embedding import embed
    q = embed(query)
    if not q:
        return []
    qv = np.asarray(q, dtype='float32')
    norm = float(np.linalg.norm(qv))
    if not norm or qv.shape[0] != mat.shape[1]:
        return []
    scores = mat.dot(qv / norm)
    order = np.argsort(-scores)
    exclude = set(exclude or ())
    out = []
    for pos in order:
        hid = ids[int(pos)]
        if hid in exclude:
            continue
        row = cat.get(hid)
        if not row or not row.get('ok'):
            continue
        item = dict(row)
        item['id'] = hid
        item['score'] = round(float(scores[int(pos)]), 3)
        out.append(item)
        if len(out) >= k:
            break
    return out


# ---------- 书架状态 ----------

def _unread(st):
    return [k for k, v in st['books'].items() if v.get('status') == 'unread']


def _reading(st):
    return [k for k, v in st['books'].items() if v.get('status') == 'reading']


def _title_key(title):
    return re.sub(r'[^\w\u4e00-\u9fff]', '', str(title or '')).lower()


def _eligible(st, cat, now, exclude=None):
    exclude = set(exclude or ())
    taken = set()
    for v in st['books'].values():
        if v.get('status') in ('unread', 'reading', 'finished', 'abandoned'):
            taken.add(_title_key(v.get('title')))
    out = []
    for hid, row in cat.items():
        if not row.get('ok') or hid in exclude:
            continue
        if _title_key(row.get('title')) in taken:
            continue
        book = st['books'].get(hid) or {}
        if book.get('status') == 'removed':
            if float(book.get('cooldown_until') or 0) > now:
                continue
        item = dict(row)
        item['id'] = hid
        out.append(item)
    return out


def _evict(st, now):
    """连续 N 次补货没被移入在读的未读，移出书架并冷却。"""
    seq = int(st.get('refill_seq') or 0)
    removed = []
    for hid in _unread(st):
        book = st['books'][hid]
        ref = max(int(book.get('added_epoch') or 0),
                  int(book.get('promoted_epoch') or 0))
        if seq - ref >= int(cfg.BOOKSHELF_EVICT_REFILLS):
            book['status'] = 'removed'
            book['removed_at'] = now
            book['cooldown_until'] = now + cfg.BOOKSHELF_REMOVED_COOLDOWN_DAYS * 86400
            removed.append(hid)
    return removed


def refill(now=None):
    """未读降到阈值时补货。模型提名一半、随机一半，再由模型按缺口挑选。"""
    now = time.time() if now is None else float(now)
    st = _load()
    cat = load_catalog()
    unread = _unread(st)
    if len(unread) > int(cfg.BOOKSHELF_UNREAD_LOW):
        return {'skipped': 'enough', 'unread': len(unread)}
    evicted = _evict(st, now)
    need = int(cfg.BOOKSHELF_UNREAD_MAX) - len(_unread(st))
    if need <= 0:
        _save(st)
        return {'skipped': 'full', 'evicted': evicted}
    taste = bookshelf_model.taste(now)
    pool = _search_catalog(taste, k=16)
    if not pool:
        pool = _eligible(st, cat, now)
    pool = _eligible(st, {r['id']: r for r in pool}, now) if pool else []
    if not pool:
        _save(st)
        return {'skipped': 'no_books', 'evicted': evicted}
    by_id = {r['id']: r for r in pool}
    pool = [by_id[hid] for hid in order_ids(list(by_id), by_id)]
    pool = pool[:16]
    model_ids = bookshelf_model.pick_candidates(
        taste, pool, int(cfg.BOOKSHELF_MODEL_PICK))
    if not model_ids:
        model_ids = [r['id'] for r in pool[:int(cfg.BOOKSHELF_MODEL_PICK)]]
    rest = [r for r in pool if r['id'] not in model_ids]
    random.shuffle(rest)
    picks = [r for r in pool if r['id'] in model_ids] + rest
    by_id = {r['id']: r for r in picks}
    picks = [by_id[hid] for hid in order_ids(list(by_id), by_id)]
    cands = picks[:int(cfg.BOOKSHELF_CANDIDATES)]
    chosen = bookshelf_model.choose(taste, cands, need)
    if not chosen:
        chosen = [r['id'] for r in cands[:need]]
    chosen = _normalize_chosen(st, cat, chosen, picks, now, want=need)
    st['refill_seq'] = int(st.get('refill_seq') or 0) + 1
    seq = st['refill_seq']
    for hid in chosen[:need]:
        row = cat.get(hid) or {}
        st['books'][hid] = {
            'status': 'unread', 'title': row.get('title') or hid,
            'author': row.get('author') or '', 'lang': row.get('lang') or '',
            'added_at': now, 'added_epoch': seq, 'promoted_epoch': 0,
            'progress': 0, 'last_note': '', 'last_read_at': 0, 'sessions': 0,
        }
    st['last_refill_at'] = now
    _save(st)
    LOG.info('书架补货：%d 本（候选 %d，模型提名 %d）',
             len(chosen), len(cands), len(model_ids))
    return {'added': chosen[:need], 'evicted': evicted,
            'candidates': [r.get('id') for r in cands], 'taste': taste}


def _fill_reading(st, now):
    unread = sorted(_unread(st),
                    key=lambda k: (st['books'][k].get('added_at') or 0, k))
    unread = order_ids(unread)
    while len(_reading(st)) < int(cfg.BOOKSHELF_READING_MAX) and unread:
        hid = unread.pop(0)
        book = st['books'][hid]
        book['status'] = 'reading'
        book['promoted_epoch'] = int(st.get('refill_seq') or 0)
        book['last_read_at'] = 0
        book['progress'] = int(book.get('progress') or 0)
    return _reading(st)


def _choose_book(st):
    rows = []
    cat = load_catalog()
    for hid in _reading(st):
        book = st['books'][hid]
        total = int((cat.get(hid) or {}).get('chars') or 0) or 1
        ratio = float(book.get('progress') or 0) / total
        last = float(book.get('last_read_at') or 0)
        rows.append((-ratio, 0 if not last else last, hid))
    if not rows:
        return None
    rows.sort()
    ids = order_ids([hid for _, _, hid in rows], cat)
    return ids[0] if ids else None


def _sample(text, budget):
    text = text or ''
    if len(text) <= budget:
        return text
    window = max(200, budget // 5)
    n = 5
    starts = [int(i * (len(text) - window) / (n - 1)) for i in range(n)]
    return '\n…\n'.join(text[s:s + window] for s in starts)


def _append_log(pid, row):
    path = log_path(pid)
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError:
        LOG.exception('读书日志写盘失败：%s', path)


def _add_memory(summary):
    uid = self_state.recent_private_uid()
    if not uid:
        return False
    try:
        import memory_card as mem
        import rp_core as core
        with mem.state_scope(core.user_dir(uid)):
            data = mem.load()
            mem.add_episode(data, {'summary': summary[:120], 'importance': 2,
                                   'channel': 'private', 'speaker': '你'})
            mem.save(data)
        return True
    except Exception:
        LOG.exception('读书总结写记忆事件失败（不影响阅读）')
        return False


def run_session(now=None, item=None, pid=None):
    """读一次：推进进度、写笔记、更新好奇点，读完或弃读后进已读区。"""
    now = time.time() if now is None else float(now)
    day = day_clock.logical_today(now)
    with _LOCK:
        st = _load(pid)
        if st.get('finished_day') != day:
            st['finished_day'] = day
            st['finished_count'] = 0
        if int(st.get('finished_count') or 0) >= int(cfg.BOOKSHELF_DAILY_FINISH_MAX):
            _save(st, pid)
            return {'skipped': 'daily_done'}
        _fill_reading(st, now)
        hid = _choose_book(st)
        if not hid:
            _save(st, pid)
            return {'skipped': 'no_reading'}
        book = st['books'][hid]
        cat = load_catalog()
        row = cat.get(hid) or {}
        total = int(row.get('chars') or 0)
        if total <= 0:
            book['status'] = 'abandoned'
            book['abandoned'] = True
            book['finished_at'] = now
            st['finished_count'] = int(st.get('finished_count') or 0) + 1
            _save(st, pid)
            return {'skipped': 'empty_book'}
        minutes = int((item or {}).get('duration_min')
                      or cfg.BOOKSHELF_SLOT_MINUTES)
        pace = bookshelf_model.pace_chars(row.get('lang') or 'zh')
        step = max(200, pace * minutes)
        start = int(book.get('progress') or 0)
        end = min(total, start + step)
        progress = float(end) / total
        excerpt = _sample(read_text(hid)[start:end], cfg.BOOKSHELF_SAMPLE_CHARS)
        res = bookshelf_model.read_note(
            row, book.get('last_note'), excerpt, progress, end - start, minutes)
        book['progress'] = end
        book['last_note'] = res.get('note') or ''
        book['last_read_at'] = now
        book['sessions'] = int(book.get('sessions') or 0) + 1
        status = 'reading'
        if end >= total:
            status = 'finished'
        elif res.get('abandon'):
            status = 'abandoned'
        if status != 'reading':
            book['status'] = status
            book['finished_at'] = now
            book['abandoned'] = status == 'abandoned'
            st['finished_count'] = int(st.get('finished_count') or 0) + 1
        _save(st, pid)
    title = row.get('title') or hid
    _append_log(pid, {'ts': now, 'day': day,
                      'time': day_life.clock(now), 'book': hid, 'title': title,
                      'summary': res.get('note') or '',
                      'progress': round(progress, 4), 'chars': end,
                      'status': status})
    if res.get('curiosity'):
        if not self_state.has_reading_curiosity(hid):
            self_state.add_curiosity(res['curiosity'], kind='reading',
                                     book_id=hid, title=title, now=now)
    if status != 'reading':
        verb = '读完' if status == 'finished' else '读不下去，放下了'
        _add_memory('她%s《%s》。%s' % (verb, title, res.get('note') or ''))
    LOG.info('阅读《%s》到 %d/%d（%d%%），状态 %s',
             title, end, total, int(progress * 100), status)
    return {'book': hid, 'title': title, 'status': status,
            'progress': progress, 'note': res.get('note'),
            'curiosity': res.get('curiosity')}


# ---------- 日程联动 ----------

def reading_items(now=None):
    rows = []
    try:
        rows = self_state.items(now)
    except Exception:
        rows = day_life.routine_items()
    out = []
    for r in rows:
        if not any(w in str(r.get('text') or '') for w in READING_WORDS):
            continue
        item = dict(r)
        item['id'] = str(r.get('id') or 'routine-%s' % (r.get('time') or ''))
        out.append(item)
    return out


def due_slot(now=None):
    now = time.time() if now is None else float(now)
    day = day_clock.logical_today(now)
    st = _load()
    done = set((st.get('slots') or {}).get(day) or [])
    cur = day_life.minutes(day_life.clock(now)) or 0
    window = int(cfg.BOOKSHELF_SLOT_WINDOW_MIN)
    for item in reading_items(now):
        if str(item.get('id')) in done:
            continue
        start = day_life.minutes(item.get('time'))
        if start is None:
            continue
        if start <= cur <= start + window:
            return item
    return None


def mark_slot_done(item, now=None, pid=None):
    now = time.time() if now is None else float(now)
    day = day_clock.logical_today(now)
    with _LOCK:
        st = _load(pid)
        slots = st.setdefault('slots', {})
        rows = slots.get(day) or []
        if str(item.get('id')) not in rows:
            rows.append(str(item.get('id')))
        slots[day] = rows[-12:]
        _save(st, pid)


def daily_init(now=None, pid=None):
    """4:00 初始化：扫书库，日程有读书条目就补货，并记下本次初始化时间。"""
    now = time.time() if now is None else float(now)
    out = {}
    try:
        out['ingest'] = ingest(limit=cfg.BOOKSHELF_MAX_INGEST_PER_RUN, now=now)
    except Exception:
        LOG.exception('书库扫描失败')
        out['ingest'] = {'error': True}
    if reading_items(now):
        try:
            out['refill'] = refill(now)
        except Exception:
            LOG.exception('书架补货失败')
            out['refill'] = {'error': True}
    with _LOCK:
        st = _load(pid)
        st['last_init_day'] = day_clock.logical_today(now)
        st['init_at'] = now
        _save(st, pid)
    return out


def _run_async(name, fn, pid):
    if not _TASK_LOCK.acquire(False):
        return False

    def run():
        global _FAIL_UNTIL
        try:
            if pid:
                with persona.context(pid):
                    fn()
            else:
                fn()
        except Exception:
            LOG.exception('书架任务失败：%s', name)
            with _FAIL_LOCK:
                _FAIL_UNTIL = time.time() + cfg.SELF_RETRY_SEC
        finally:
            _TASK_LOCK.release()

    threading.Thread(target=run, name='bookshelf-%s' % name, daemon=True).start()
    return True


def tick(now=None):
    """ticker 每轮调用：到读书时段就开始阅读。4:00 的初始化走 day_init 流水线。"""
    if not enabled():
        return False
    now = time.time() if now is None else float(now)
    pid = persona.current()
    with _FAIL_LOCK:
        if now < _FAIL_UNTIL:
            return False
    item = due_slot(now)
    if item:
        def session():
            res = run_session(now, item, pid)
            if res.get('skipped') not in ('no_reading',):
                mark_slot_done(item, now, pid)
        return _run_async('read', session, pid)
    return False


# ---------- 只读视图 ----------

SHELF_ACTIONS = ('start', 'pause', 'abandon', 'restore', 'remove')


def shelf_action(pid, hid, action, now=None):
    """面板手动调整书架：开始读 / 暂停 / 搁置 / 放回未读 / 移出。"""
    now = time.time() if now is None else float(now)
    action = str(action or '')
    if action not in SHELF_ACTIONS:
        raise ValueError('未知操作')
    with _LOCK:
        st = _load(pid)
        book = (st.get('books') or {}).get(str(hid or ''))
        if not book:
            raise ValueError('这本书不在书架上')
        cur = book.get('status')
        if action == 'start':
            if cur == 'removed':
                raise ValueError('这本书已移出书架')
            if cur != 'reading':
                if len(_reading(st)) >= int(cfg.BOOKSHELF_READING_MAX):
                    raise ValueError('在读最多 %d 本，先暂停或搁置一本'
                                     % int(cfg.BOOKSHELF_READING_MAX))
                if cur == 'finished':
                    book['progress'] = 0
                    book['last_note'] = ''
                book['status'] = 'reading'
                book['abandoned'] = False
                book['promoted_epoch'] = int(st.get('refill_seq') or 0)
        elif action == 'pause':
            if cur != 'reading':
                raise ValueError('这本书不在读')
            book['status'] = 'unread'
        elif action == 'abandon':
            if cur not in ('unread', 'reading'):
                raise ValueError('只有未读或在读的书可以搁置')
            book['status'] = 'abandoned'
            book['abandoned'] = True
            book['finished_at'] = now
        elif action == 'restore':
            if cur not in ('finished', 'abandoned'):
                raise ValueError('只有已读或搁置的书可以放回未读')
            book['status'] = 'unread'
            book['abandoned'] = False
            book['progress'] = 0
            book['last_note'] = ''
            book['last_read_at'] = 0
            book['promoted_epoch'] = 0
        elif cur == 'removed':
            raise ValueError('这本书已移出书架')
        else:
            book['status'] = 'removed'
            book['removed_at'] = now
            book['cooldown_until'] = (now + int(cfg.BOOKSHELF_REMOVED_COOLDOWN_DAYS)
                                      * 86400)
        _save(st, pid)
    LOG.info('书架手动操作：%s %s（%s）', pid, hid, action)
    return status(pid)


def notes(pid, hid, limit=60):
    """某本书的阅读日志，新的在前。正文不入日志，只有每次留下的笔记。"""
    hid = str(hid or '')
    try:
        limit = max(1, min(int(limit or 60), 200))
    except (TypeError, ValueError):
        limit = 60
    rows = []
    try:
        with open(log_path(pid), encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if str((row or {}).get('book') or '') == hid:
                    rows.append(row)
    except OSError:
        return []
    return list(reversed(rows))[:limit]


def status(pid=None):
    with _LOCK:
        st = _load(pid)
    cat = load_catalog()
    groups = {'unread': [], 'reading': [], 'finished': [], 'abandoned': [],
              'removed': []}
    for hid, book in st['books'].items():
        group = groups.get(book.get('status'))
        if group is None:
            continue
        row = cat.get(hid) or {}
        total = int(row.get('chars') or 0) or 1
        group.append({
            'id': hid,
            'title': book.get('title') or row.get('title') or hid,
            'author': book.get('author') or row.get('author') or '',
            'lang': book.get('lang') or row.get('lang') or '',
            'chars': int(row.get('chars') or 0),
            'progress': round(float(book.get('progress') or 0) / total, 4),
            'last_note': book.get('last_note') or '',
            'last_read_at': book.get('last_read_at') or 0,
            'sessions': int(book.get('sessions') or 0),
            'added_at': book.get('added_at') or 0,
            'finished_at': book.get('finished_at') or 0,
        })
    groups['unread'].sort(key=lambda r: r['title'])
    groups['reading'].sort(key=lambda r: (-r['progress'], r['last_read_at']))
    for key in ('finished', 'abandoned'):
        groups[key].sort(key=lambda r: (-r['finished_at'], r['title']))
    return {'library': library_dir(), 'refill_seq': st.get('refill_seq'),
            'last_init_day': st.get('last_init_day'), 'groups': groups}
