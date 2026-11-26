# -*- coding: utf-8 -*-
"""运行态快照：给 git 管不到的记忆/场景数据留可回滚的副本。

state/ 里的东西不是代码：
  - state/personas/<id>/users/*/memory_card.json（用户记忆，按角色分区）
  - state/groups/decisions.jsonl（群决策记录）
  - state/runtime_config.json（面板设置）
这些数据每天都在变，进 git 只会产生噪音；但误删/误改时需要能回滚。

做法：
  - `create` 把 state/ 打成 zip，包内附 `_snapshot.json`（逐文件 sha256 + 代码版本）；
  - 默认跳过 `state/backups/` 和可重建的 `corpus_embeddings.json`；
  - `prune --keep N` 只保留最近 N 份，避免无限增长。

用法：
    python scripts/state_snapshot.py create
    python scripts/state_snapshot.py list
    python scripts/state_snapshot.py prune --keep 30
"""
import argparse
import hashlib
import json
import os
import sys
import zipfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import paths  # noqa: E402

ROOT = paths.ROOT
STATE_DIR = paths.STATE_DIR
BACKUP_DIR = os.path.join(STATE_DIR, 'backups')
PREFIX = 'state-'
MAX_FILE = 100 * 1024 * 1024       # 单文件超过 100MB 跳过，避免把内存打满

# 浏览器 profile 是窗口缓存，不是记忆数据；embeddings 可由语料重建
EXCLUDE_DIRS = ('backups',)
EXCLUDE_DIR_SUFFIXES = ('_profile',)
EXCLUDE_FILES = ('corpus_embeddings.json',)
EXCLUDE_FILE_SUFFIXES = ('.dim-mismatch.bak',)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _git_version():
    import subprocess

    def run(args):
        try:
            return subprocess.check_output(
                ['git', '-C', ROOT] + args,
                stderr=subprocess.DEVNULL, text=True).strip() or None
        except Exception:
            return None
    return {'describe': run(['describe', '--tags', '--always', '--dirty']),
            'commit': run(['rev-parse', '--short', 'HEAD'])}


def _iter_files(root, include_embeddings=False):
    for base, dirs, files in os.walk(root):
        rel_base = os.path.relpath(base, root)
        if rel_base == '.':
            dirs[:] = [d for d in dirs
                       if d not in EXCLUDE_DIRS
                       and not d.endswith(EXCLUDE_DIR_SUFFIXES)]
        for name in files:
            if name.endswith('.tmp') or name.endswith(EXCLUDE_FILE_SUFFIXES):
                continue
            if not include_embeddings and name in EXCLUDE_FILES:
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root).replace('\\', '/')
            yield path, rel


def create(out_dir=BACKUP_DIR, include_embeddings=False):
    if not os.path.isdir(STATE_DIR):
        raise SystemExit('state/ 不存在：%s' % STATE_DIR)
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    target = os.path.join(out_dir, '%s%s.zip' % (PREFIX, stamp))
    tmp = target + '.tmp'

    entries = []
    skipped = []
    with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        for path, rel in _iter_files(STATE_DIR, include_embeddings):
            try:
                size = os.path.getsize(path)
                if size > MAX_FILE:
                    skipped.append({'path': rel, 'reason': 'too_large', 'size': size})
                    continue
                with open(path, 'rb') as f:
                    data = f.read()
                zf.writestr(rel, data)
                entries.append({
                    'path': rel,
                    'size': len(data),
                    'sha256': _sha256(data),
                    'mtime': datetime.fromtimestamp(
                        os.path.getmtime(path)).astimezone().isoformat(
                            timespec='seconds'),
                })
            except OSError as e:
                skipped.append({'path': rel, 'reason': str(e)[:120]})
        zf.writestr('_snapshot.json', json.dumps({
            'created_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'code': _git_version(),
            'files': entries,
            'skipped': skipped,
            'note': 'state/ 运行态快照；不含 state/backups 与可重建的 embeddings',
        }, ensure_ascii=False, indent=2))
    os.replace(tmp, target)
    return target, entries, skipped


def list_snapshots(out_dir=BACKUP_DIR):
    if not os.path.isdir(out_dir):
        return []
    rows = []
    for name in os.listdir(out_dir):
        if not (name.startswith(PREFIX) and name.endswith('.zip')):
            continue
        path = os.path.join(out_dir, name)
        st = os.stat(path)
        rows.append({'name': name, 'path': path, 'size': st.st_size,
                     'mtime': datetime.fromtimestamp(st.st_mtime)})
    return sorted(rows, key=lambda r: r['mtime'], reverse=True)


def prune(keep=30, out_dir=BACKUP_DIR, dry_run=False):
    rows = list_snapshots(out_dir)
    victims = rows[int(keep):]
    if not dry_run:
        for row in victims:
            try:
                os.remove(row['path'])
            except OSError:
                pass
    return victims


def main(argv=None):
    ap = argparse.ArgumentParser(description='运行态快照：把 state/ 打成可回滚的 zip')
    sub = ap.add_subparsers(dest='cmd')

    c = sub.add_parser('create', help='打一份 state/ 快照')
    c.add_argument('--out-dir', default=BACKUP_DIR)
    c.add_argument('--include-embeddings', action='store_true',
                   help='连可重建的 corpus_embeddings.json 一起打包')

    l = sub.add_parser('list', help='列出已有快照')
    l.add_argument('--out-dir', default=BACKUP_DIR)

    p = sub.add_parser('prune', help='只保留最近 N 份')
    p.add_argument('--keep', type=int, default=30)
    p.add_argument('--out-dir', default=BACKUP_DIR)
    p.add_argument('--dry-run', action='store_true')

    args = ap.parse_args(argv)
    if args.cmd == 'create':
        target, entries, skipped = create(args.out_dir, args.include_embeddings)
        print('已创建：%s' % target)
        print('文件 %d 个，%d 字节；跳过 %d 个' % (
            len(entries), os.path.getsize(target), len(skipped)))
        for item in skipped[:5]:
            print('  [跳过] %s（%s）' % (item['path'], item['reason']))
        return 0
    if args.cmd == 'list':
        rows = list_snapshots(args.out_dir)
        if not rows:
            print('还没有快照：%s' % args.out_dir)
            return 0
        for row in rows:
            print('%s  %8.1f MB  %s' % (
                row['name'], row['size'] / 1048576.0,
                row['mtime'].strftime('%Y-%m-%d %H:%M:%S')))
        return 0
    if args.cmd == 'prune':
        victims = prune(args.keep, args.out_dir, args.dry_run)
        verb = '将删除' if args.dry_run else '已删除'
        print('%s %d 份（保留最近 %d 份）' % (verb, len(victims), args.keep))
        for row in victims:
            print('  %s' % row['name'])
        return 0
    ap.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
