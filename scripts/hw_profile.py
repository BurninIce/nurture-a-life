# -*- coding: utf-8 -*-
"""按本机显存自动选「上下文长度 / 并发 / embedding 模型」。

为什么需要这个模块：用户有两台工作站——移动工作站 12GB、台式 8GB。
任何一套参数写死都会在另一台上出事：12GB 上白扔一半显存，8GB 上直接 OOM 或
把 9B 挤到内存里（速度从 35 tok/s 掉到 3 tok/s）。

检测优先级：
    1. 环境变量 `NAL_VRAM_GB`（手动指定，最可靠；多卡机器也用它）
    2. `nvidia-smi --query-gpu=memory.total`（自动，约 100ms）
    3. 兜底 `DEFAULT_VRAM_GB = 12`（宁可保守，不假设大显存）

用法：
    py -3.13 scripts/hw_profile.py     # 打印本机检测结果与全部档位
    from hw_profile import NUM_CTX, CONCURRENCY, EMBED_MODEL
"""
import os
import subprocess
import sys

DEFAULT_VRAM_GB = 12.0     # 检测失败时的兜底（别假设大卡）
DESKTOP_RESERVE_GB = 1.5   # Windows 桌面/浏览器常占，算预算时要扣掉

# 各档位：显存下限 → (num_ctx, 并发, embedding 模型)
# 长角色卡需要给历史与记忆留出余量；8GB 扩到 8192 时仍保持单并发和小 embedding。
TIERS = (
    (24.0, 8192, 3, 'bge-m3'),
    (16.0, 8192, 2, 'bge-m3'),
    (11.0, 8192, 2, 'bge-m3'),          # 12GB 移动工作站落这一档
    (7.5, 8192, 1, 'bge-small-zh-v1.5'),  # 8GB 台式落这一档
    (0.0, 3072, 1, 'bge-small-zh-v1.5'),
)


def detect_vram_gb():
    """返回 (显存GB, 来源)。来源：env / nvidia-smi / default。"""
    env = os.environ.get('NAL_VRAM_GB')
    if env:
        try:
            return float(env), 'env'
        except ValueError:
            pass
    if os.environ.get('NAL_HW', '').lower() == 'off':
        return DEFAULT_VRAM_GB, 'default(off)'
    try:
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW，别闪黑框
        out = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=5, **kwargs)
        vals = []
        for line in (out.stdout or '').splitlines():
            line = line.strip()
            if line.isdigit():
                vals.append(int(line) / 1024.0)
        if vals:
            return round(max(vals), 1), 'nvidia-smi'
    except (OSError, subprocess.SubprocessError):
        pass
    return DEFAULT_VRAM_GB, 'default'


def profile_for(vram_gb):
    """纯函数：显存 → (num_ctx, 并发, embedding)。便于测试与手动推算。"""
    v = DEFAULT_VRAM_GB if vram_gb is None else float(vram_gb)
    for floor, num_ctx, conc, embed in TIERS:
        if v >= floor:
            return num_ctx, conc, embed
    return TIERS[-1][1], TIERS[-1][2], TIERS[-1][3]


VRAM_GB, SOURCE = detect_vram_gb()
NUM_CTX, CONCURRENCY, EMBED_MODEL = profile_for(VRAM_GB)
# embedding 可单独覆盖（拉不到推荐模型时退回 bge-m3）
EMBED_MODEL = os.environ.get('NAL_EMBED') or EMBED_MODEL


def summary():
    """一行人类可读摘要（启动日志/状态指令用）。"""
    return ('显存 %.1fG（%s）→ num_ctx %d · 并发 %d · embedding %s'
            % (VRAM_GB, SOURCE, NUM_CTX, CONCURRENCY, EMBED_MODEL))


def _main():
    print(summary())
    print('\n档位表（显存下限 → num_ctx / 并发 / embedding）：')
    for floor, num_ctx, conc, embed in TIERS:
        mark = ' ← 本机' if (floor <= VRAM_GB and
                            profile_for(VRAM_GB)[0] == num_ctx) else ''
        print('  ≥%3.0fG  ctx %4d  并发 %d  %-18s%s'
              % (floor, num_ctx, conc, embed, mark))
    print('\n覆盖方式：NAL_VRAM_GB=12（手动指定）、NAL_EMBED=bge-m3（换 embedding）、'
          'NAL_HW=off（关掉检测）')
    return 0


if __name__ == '__main__':
    sys.exit(_main())
