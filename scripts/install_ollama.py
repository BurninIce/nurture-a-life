# -*- coding: utf-8 -*-
"""把 Ollama 下载并解压到指定目录（默认项目内的 `tools/Ollama`）。

不静默：目录问过才动手，装完把路径写进 `state/setup.json` 的 `ollama_home`，
`scripts/paths.py` 下次启动就会读到它。相对路径按项目根目录展开。

用法：
    python scripts/install_ollama.py                  # 交互：问目录，确认后下载
    python scripts/install_ollama.py --check          # 只检查当前解析到哪、装没装
    python scripts/install_ollama.py --dir D:\\AI\\Ollama
    python scripts/install_ollama.py --url <zip 地址>   # 内网镜像或离线包
    python scripts/install_ollama.py --force          # 目录里已有文件也覆盖解压

安装路径里不要出现 `-`：这个限制来自用户反馈，某些环节会把路径里的连字符
当成参数分隔符，装完起不来。`--dir` 与交互输入都会校验这一条。
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import paths  # noqa: E402

DEFAULT_URL = 'https://ollama.com/download/ollama-windows-amd64.zip'
SETUP_PATH = os.path.join(paths.STATE_DIR, 'setup.json')
_BAD_CHARS = ('-',)


class InstallError(RuntimeError):
    pass


# ---------- 目录 ----------

def default_home():
    """默认装到项目里的 `tools/Ollama`；路径带 - 时退回用户目录下的 Ollama。"""
    inside = os.path.join(paths.ROOT, 'tools', 'Ollama')
    if check_target(inside)[0]:
        return inside
    local = os.environ.get('LOCALAPPDATA') or ''
    if local:
        return os.path.join(local, 'Ollama')
    return r'C:\Ollama'


def _existing_ancestor(path):
    """往上找到第一个真实存在的目录，用来判断能不能写（不创建任何东西）。"""
    while path and not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            return ''
        path = parent
    return path if os.path.isdir(path) else ''


def check_target(target):
    """校验目标目录，返回 (ok, 说明)。只读检查，不建目录。"""
    target = str(target or '').strip()
    if not target:
        return False, '目录不能为空'
    full = paths.abs_path(target)
    for ch in _BAD_CHARS:
        if ch in full:
            return False, '路径里不要有 %s，换一个目录（%s）' % (ch, full)
    if os.path.exists(full) and not os.path.isdir(full):
        return False, '这个路径已经被同名文件占用了（%s）' % full
    anc = _existing_ancestor(full)
    if not anc:
        return False, '上级目录不存在（%s）' % full
    if not os.access(anc, os.W_OK):
        return False, '没有写权限（%s）' % anc
    return True, full


def check(home=None):
    """当前解析到的 Ollama 在哪、装没装。返回 (ok, 路径, 说明)。"""
    target = paths.abs_path(home) if home else paths.OLLAMA_HOME
    exe = os.path.join(target, 'ollama.exe')
    if os.path.isfile(exe):
        size = os.path.getsize(exe)
        return True, exe, '已安装（ollama.exe %.1f MB）' % (size / 1048576.0)
    return False, exe, '没找到 ollama.exe'


# ---------- 下载与解压 ----------

def _progress(got, total):
    if not total:
        return
    pct = got * 100 // total
    if pct % 5 == 0:
        sys.stdout.write('\r  下载中 %3d%%（%.0f / %.0f MB）'
                         % (pct, got / 1048576.0, total / 1048576.0))
        sys.stdout.flush()


def download(url, dest):
    """把 url 下载到 dest，返回 (路径, 字节数)。"""
    req = urllib.request.Request(url, headers={'User-Agent': 'nurture-a-life-installer'})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get('Content-Length') or 0)
            got = 0
            with open(dest, 'wb') as f:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    _progress(got, total)
            sys.stdout.write('\n')
            return dest, got
    except urllib.error.HTTPError as e:
        raise InstallError('下载失败，HTTP %s（%s）' % (e.code, url))
    except urllib.error.URLError as e:
        raise InstallError('下载失败，连不上 %s（%s）' % (url, e.reason))
    except OSError as e:
        raise InstallError('写文件失败（%s）' % e)


def extract(zip_path, target, force=False):
    """解压到 target；压缩包若套了一层同名目录会自动剥掉。返回解出的文件数。"""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise InstallError('不是有效的 zip 包（%s）' % e)
    names = [n for n in zf.namelist() if n and not n.endswith('/')]
    if not names:
        raise InstallError('压缩包里没有文件')
    tops = {n.split('/')[0] for n in names}
    strip = len(tops) == 1 and all('/' in n for n in names)
    os.makedirs(target, exist_ok=True)
    count = 0
    with zf:
        for name in zf.namelist():
            if not name or name.endswith('/'):
                continue
            rel = name.split('/', 1)[1] if strip else name
            if not rel or rel.startswith(('/', '\\')):
                continue
            out = os.path.join(target, rel.replace('/', os.sep))
            if os.path.dirname(out):
                os.makedirs(os.path.dirname(out), exist_ok=True)
            if os.path.exists(out) and not force:
                continue
            with zf.open(name) as src, open(out, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            count += 1
    return count


def _write_home(home):
    """把安装目录写进 state/setup.json（保留其它字段）。"""
    data = {}
    try:
        with open(SETUP_PATH, encoding='utf-8') as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    data['ollama_home'] = home
    os.makedirs(paths.STATE_DIR, exist_ok=True)
    tmp = SETUP_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, SETUP_PATH)


def install(home, url=None, force=False):
    """下载并解压到 home，返回 ollama.exe 的路径。失败抛 InstallError。"""
    ok, full = check_target(home)
    if not ok:
        raise InstallError(full)
    ok, exe, why = check(full)
    if ok and not force:
        return exe
    url = url or DEFAULT_URL
    tmpdir = tempfile.mkdtemp(prefix='ollama_zip_')
    zip_path = os.path.join(tmpdir, 'ollama.zip')
    try:
        print('  来源 %s' % url)
        zip_path, got = download(url, zip_path)
        print('  已下载 %.1f MB，开始解压到 %s' % (got / 1048576.0, full))
        count = extract(zip_path, full, force=force)
        print('  解压了 %d 个文件' % count)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    ok, exe, why = check(full)
    if not ok:
        raise InstallError('解压完了但没找到 ollama.exe（%s）' % full)
    return exe


# ---------- 命令行 ----------

def main(argv=None):
    ap = argparse.ArgumentParser(description='把 Ollama 装到指定目录')
    ap.add_argument('--dir', help='安装目录（默认项目内 tools/Ollama；路径不要带 -）')
    ap.add_argument('--url', help='zip 地址，默认官方 Windows 版')
    ap.add_argument('--check', action='store_true', help='只检查当前状态')
    ap.add_argument('--force', action='store_true', help='已有文件也覆盖解压')
    ap.add_argument('--yes', action='store_true', help='不再确认，直接按 --dir 安装')
    args = ap.parse_args(argv)

    if args.check:
        ok, exe, why = check(args.dir)
        print('%s %s（%s）' % ('[OK]' if ok else '[--]', exe, why))
        print('默认目录 %s' % default_home())
        return 0 if ok else 1

    target = args.dir or default_home()
    ok, reason = check_target(target)
    if not ok:
        print('[FAIL] %s' % reason)
        return 2
    full = reason
    if not args.yes:
        print('要把 Ollama 装到 %s 吗' % full)
        print('（官方 zip，约 1.5 GB；回车确认，输入 n 取消）')
        try:
            ans = input('继续 [Y/n]: ').strip().lower()
        except EOFError:
            ans = 'n'
        if ans in ('n', 'no'):
            print('已取消。')
            return 1
    try:
        exe = install(full, url=args.url, force=args.force)
    except InstallError as e:
        print('[FAIL] %s' % e)
        return 1
    _write_home(full)
    print('[OK] 装好了 %s' % exe)
    print('     路径已写入 %s 的 ollama_home，下次启动直接用。' % SETUP_PATH)
    print('     想换目录就重跑一次，或设环境变量 NAL_OLLAMA_HOME。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
