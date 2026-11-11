# -*- coding: utf-8 -*-
"""离线套件启动器：装 state_guard 钩子后按 `python script.py args...` 的方式执行。

pytest 与各 `--selftest` 套件都经由这里拉起子进程，保证套件误写线上 state/
时立即报错，而不是静默污染。用法：
    python scripts/run_offline.py scripts/test_xxx.py [套件自己的参数...]
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import state_guard  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    state_guard.install()
    script = sys.argv[1]
    # 让套件看到的 argv 与直接 `python script.py args...` 完全一致
    sys.argv = sys.argv[1:]
    runpy.run_path(script, run_name='__main__')
    return 0


if __name__ == '__main__':
    sys.exit(main())