"""Deity TUI 自定义主题：深浅两套 + 四级文字色。

深色主题为黑色基底（2026-11-22 用户定，取代 Klee 深绿）：终端底色
纯黑、面板/底色深灰、强调色白/浅灰中性色，界面里不再出现绿色——
深色变体的绿色 token（accent、success、user-bg、code-bg 的绿调）
已一并换成中性灰。浅色主题暂不动（仍用 Klee 深绿作强调色），与黑
色方案不冲突。颜色走语义层级：正文 / 次级 / 三级 / 弱提示四级文
字色，以及 success/warning/error 语义色。边框色（primary）为中性灰。

主题只提供呈现 token：工作台（interactive.py）与监视台（app.py）
共用，保证两个界面观感一致。Rich 文本（如工具行、diff 行）无法引
用 CSS 变量，调色板函数 palette() 给出与主题变量同源的十六进制色值。
"""

import os
from collections.abc import Mapping
from typing import Any

from textual.theme import Theme

ACCENT_LIGHT = "#1E4C35"
ACCENT_DARK = "#E8EAE8"

# 四级文字色与辅助色块的十六进制值（与 Theme.variables 同源）。
_DARK_LEVELS = {
    "text-secondary": "#BDBDBD",
    "text-tertiary": "#8A8A8A",
    "text-quaternary": "#5E5E5E",
    "user-bg": "#1F1F1F",
    "hover-bg": "#2C2C2C",
    "code-bg": "#1F1F1F",
}
_LIGHT_LEVELS = {
    "text-secondary": "#454A45",
    "text-tertiary": "#6D726D",
    "text-quaternary": "#9BA09B",
    "user-bg": "#E6EFE9",
    "hover-bg": "#E3E7E0",
    "code-bg": "#EEF1EA",
}

DEITY_DARK = Theme(
    name="deity-dark",
    dark=True,
    primary="#6E6E6E",
    secondary="#8A8A8A",
    accent=ACCENT_DARK,
    foreground="#E8EAE8",
    background="#000000",
    surface="#1A1A1A",
    panel="#242424",
    success="#E8EAE8",
    warning="#D9A441",
    error="#E5534B",
    variables=dict(_DARK_LEVELS),
)

DEITY_LIGHT = Theme(
    name="deity-light",
    dark=False,
    primary="#98A29A",
    secondary="#6D7A6E",
    accent=ACCENT_LIGHT,
    foreground="#1D201D",
    background="#F7F8F5",
    surface="#FFFFFF",
    panel="#ECEEE8",
    success="#1E7D46",
    warning="#B97F16",
    error="#C62F3B",
    variables=dict(_LIGHT_LEVELS),
)

DEITY_THEMES = (DEITY_DARK, DEITY_LIGHT)


def default_theme_name(environ: Mapping[str, str] | None = None) -> str:
    """默认主题名：按 COLORFGBG（fg;bg 约定）粗判终端明暗，缺失时深色。

    终端没有统一的外观查询协议；COLORFGBG 是 xterm/rxvt 系的既有
    约定，背景色号为 7/15（白/亮白）时判浅色，其余一律深色——终端
    用户以深色居多，且深色主题在浅底终端上只是偏沉、不会不可读。
    """
    env = os.environ if environ is None else environ
    colorfgbg = env.get("COLORFGBG", "")
    parts = colorfgbg.split(";")
    if parts and parts[-1].strip() in {"7", "15"}:
        return DEITY_LIGHT.name
    return DEITY_DARK.name


def apply_deity_theme(app: Any) -> str:
    """给 Textual App 注册两套 deity 主题并选中默认一套，返回主题名。"""
    for theme in DEITY_THEMES:
        app.register_theme(theme)
    name = default_theme_name()
    app.theme = name
    return name


def palette(dark: bool) -> dict[str, str]:
    """Rich 渲染用的十六进制调色板（与主题变量同源，按明暗二选一）。

    深色为黑色方案：success 不再是绿色，改用中性浅灰（与 accent 同值）；
    diff 行底色：增行深灰、删行暗红底（无绿色）。
    """
    levels = _DARK_LEVELS if dark else _LIGHT_LEVELS
    return {
        "accent": ACCENT_DARK if dark else ACCENT_LIGHT,
        "secondary": levels["text-secondary"],
        "tertiary": levels["text-tertiary"],
        "quaternary": levels["text-quaternary"],
        "success": "#E8EAE8" if dark else "#1E7D46",
        "warning": "#D9A441" if dark else "#B97F16",
        "error": "#E5534B" if dark else "#C62F3B",
        "diff_add_bg": "#262626" if dark else "#EDEDED",
        "diff_del_bg": "#3A1F1C" if dark else "#F6DEDC",
    }


__all__ = [
    "ACCENT_DARK",
    "ACCENT_LIGHT",
    "DEITY_DARK",
    "DEITY_LIGHT",
    "DEITY_THEMES",
    "apply_deity_theme",
    "default_theme_name",
    "palette",
]
