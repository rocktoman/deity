"""Deity TUI 自定义主题：黑色主题（深底中性色）、四级文字色、明暗默认。"""

from deity.interfaces.tui.theme import (
    ACCENT_DARK,
    ACCENT_LIGHT,
    DEITY_DARK,
    DEITY_LIGHT,
    default_theme_name,
    palette,
)


def _luminance(hex_color: str) -> float:
    raw = hex_color.lstrip("#")
    r, g, b = (int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_accent_is_neutral_black_theme() -> None:
    # 黑色主题（2026-11-22）：深底强调色改为中性白/浅灰，界面无绿色；
    # 浅底主题暂不动，仍用 Klee 深绿原色。
    assert ACCENT_DARK == "#E8EAE8"
    assert DEITY_DARK.accent == ACCENT_DARK
    assert ACCENT_LIGHT == "#1E4C35"
    assert DEITY_LIGHT.accent == ACCENT_LIGHT
    assert _luminance(ACCENT_DARK) > _luminance(ACCENT_LIGHT)


def test_both_themes_define_four_text_levels_in_order() -> None:
    def lums(theme: object) -> list[float]:
        variables = theme.variables  # type: ignore[attr-defined]
        return [
            _luminance(variables[key])
            for key in ("text-secondary", "text-tertiary", "text-quaternary")
        ]

    # 深底：次级最亮、弱提示最暗；浅底反之。层级顺序不得乱。
    dark_lums = lums(DEITY_DARK)
    assert dark_lums[0] > dark_lums[1] > dark_lums[2]
    light_lums = lums(DEITY_LIGHT)
    assert light_lums[0] < light_lums[1] < light_lums[2]


def test_default_theme_name_follows_colorfgbg() -> None:
    assert default_theme_name({}) == "deity-dark"
    assert default_theme_name({"COLORFGBG": "15;0"}) == "deity-dark"
    assert default_theme_name({"COLORFGBG": "0;15"}) == "deity-light"
    assert default_theme_name({"COLORFGBG": "0;7"}) == "deity-light"


def test_palette_matches_theme_accents() -> None:
    assert palette(dark=True)["accent"] == DEITY_DARK.accent
    assert palette(dark=False)["accent"] == DEITY_LIGHT.accent
    assert palette(dark=True)["secondary"] == DEITY_DARK.variables["text-secondary"]
