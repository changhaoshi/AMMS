# -*- coding: utf-8 -*-
"""
utf8_stdout.py —— 让 Python 在 Windows 控制台也能安全输出中文

背景：
    Windows 的控制台默认编码不是 UTF-8（简体中文机器上是 cp936，
    英文/西文机器上是 cp1252）。此时 print("中文") 会抛
        UnicodeEncodeError: 'charmap' codec can't encode characters ...
    打包后的 exe 用 console=False 运行时 sys.stdout 为 None，
    此时 print() 会静默返回（CPython 行为），不会崩，但也看不到任何输出。

本模块做两件事（幂等，可重复调用）：
    1. stdout/stderr 不是 None 时，强制切到 UTF-8 + errors="replace"
       —— 即使编码切换失败，也不会因为一个字符让整个程序崩掉。
    2. stdout/stderr 是 None 时（GUI 打包模式），补一个丢弃输出的空流，
       保证 print() 依然安全。

用法：
    import utf8_stdout
    utf8_stdout.enable()
"""
import sys


class _NullWriter:
    """GUI 模式下的替身：接收所有输出并丢弃。

    之所以要补这个而不是依赖「stdout 为 None 时 print 静默返回」，
    是因为那属于 CPython 的实现细节；显式提供替身更可控，
    也避免 print(..., file=sys.stdout) 之类写法直接报 AttributeError。
    """

    encoding = "utf-8"

    def write(self, s):
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("NullWriter has no file descriptor")

    @property
    def buffer(self):
        return self


def enable() -> None:
    """幂等地把 stdout/stderr 调整为 UTF-8 安全模式。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            # GUI 打包模式：sys.stdout is None
            setattr(sys, name, _NullWriter())
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # 老版本 Python / 某些被重定向的流可能不支持 reconfigure，
            # 此时保持原状即可 —— 至少不会让 import 失败。
            pass


enable()
