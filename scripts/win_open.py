# -*- coding: utf-8 -*-
"""用系统默认程序打开 URL（Windows）。

优先 `os.startfile`：走 shell 关联，直接打开默认浏览器，不会额外弹资源管理器。
旧实现用 `explorer.exe <url>`；当资源管理器尚未运行时，Windows 会先启动
Shell 并显示「文档」文件夹，然后再打开浏览器——一键启动时表现为莫名多出
一个「文档 - 文件资源管理器」窗口。因此 explorer 只作为 fallback 保留：
它能让浏览器脱离宿主 Job Object，在沙箱/自动化环境里更稳。

用法：
    from win_open import open_url
    open_url('http://127.0.0.1:6780')
"""
import os
import subprocess

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def open_url(url):
    """打开 URL。成功返回 True；两种方式都失败返回 False。"""
    if hasattr(os, 'startfile'):
        try:
            os.startfile(url)
            return True
        except OSError:
            pass
    try:
        subprocess.Popen(
            ['explorer.exe', url],
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False
