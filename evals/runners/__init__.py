"""Evaluation runners."""

import sys


def _ensure_utf8_stdio() -> None:
    """把标准输出/错误切到 UTF-8（Windows 管道默认 cp1252，打印中文报告会崩溃）。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        encoding = getattr(stream, "encoding", None)
        if encoding and encoding.lower().replace("-", "") == "utf8":
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


_ensure_utf8_stdio()
