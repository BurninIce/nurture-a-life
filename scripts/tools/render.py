# -*- coding: utf-8 -*-
"""ToolResult → system 文本块。

模型看到的不是原始搜索结果，而是「事实摘要 + 转述要求」。
这一步是防止人格被检索文本污染的关键：结果越像百科，越要把口吻拉回来。
"""
import rp_config as cfg


def render(result):
    """成功给事实 + 转述约束；失败给「不许编」的约束，而不是空块。

    技术错误（超时/后端失败）只进日志，不进 system——模型只会把它当怪话。
    """
    if result is not None and result.ok and result.text.strip():
        # 原作片段和联网结果不是一回事：前者是「你记得的事」，要按回忆讲，不能朗读
        if result.source == 'novel':
            header, note = cfg.TOOL_NOVEL_HEADER, cfg.TOOL_NOVEL_NOTE
        else:
            header, note = cfg.TOOL_RESULT_HEADER, cfg.TOOL_OK_NOTE
        return '%s\n%s\n%s' % (header, result.text.strip(), note)
    return '%s\n%s' % (cfg.TOOL_RESULT_HEADER, cfg.TOOL_FAIL_NOTE)
