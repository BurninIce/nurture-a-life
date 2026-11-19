# -*- coding: utf-8 -*-
"""管理面板的独立桌面窗口：用系统默认浏览器打开（最稳定）。

历史踩坑记录（都在这台机上验证过会失败，不要再走这些路）：
  - pywebview 的 WebView2：报 Error 1411 起不来窗口。
  - Edge --app + 独立 user-data-dir：进程起了但窗口不可见。
  - Chrome --app / --new-window + 独立 user-data-dir：窗口"显示一会又没了"
    （约 20-60 秒后被系统回收，疑似独立 profile 的单实例/会话恢复机制所致）。
  - 各种 subprocess.Popen 拉起 Chrome：从 WorkBuddy Bash 沙箱拉起时，
    进程树会被沙箱回收，窗口跟着消失。

最终方案：
  用系统默认关联打开 URL（win_open.open_url）：优先 os.startfile，
  失败才回退 explorer.exe。不要直接用 explorer.exe——资源管理器未运行时
  它会先显示「文档」文件夹，再打开浏览器。

作为独立进程运行，由 qq_bridge 的 start_admin 用 subprocess 拉起；
窗口关闭不影响后端/bot（仅关掉 UI）。

用法：
    python scripts/admin_window.py [--port 6780] [--token TOKEN]
"""
import argparse
import socket
import sys
import time

from win_open import open_url


def _wait_port(port, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description='NAL 管理面板独立窗口')
    ap.add_argument('--port', type=int, default=6780)
    ap.add_argument('--token', default='', help='管理面板 token（由 bridge 传入）')
    ap.add_argument('--title', default='苏禾 · 管理面板')
    args = ap.parse_args(argv)

    if not _wait_port(args.port):
        print('[admin_window] 后端未在 15s 内就绪，仍尝试打开', file=sys.stderr)

    url = 'http://127.0.0.1:%d' % args.port
    if args.token:
        from urllib.parse import quote
        url += '/?token=' + quote(args.token, safe='')

    if open_url(url):
        print('[admin_window] 已打开管理面板：%s' % url, flush=True)
    else:
        print('[admin_window] 打开窗口失败：没有可用的默认浏览器', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
