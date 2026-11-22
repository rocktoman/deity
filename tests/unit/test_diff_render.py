"""Codex 移植：unified diff 纯函数渲染（diff_render）的单元测试。"""

from deity.interfaces.cli.diff_render import (
    DARK_DIFF_COLORS,
    count_diff_stats,
    format_diff_lines,
    format_diff_text,
    format_turn_diff,
    format_turn_diff_summary,
    parse_diff_lines,
)

SAMPLE_DIFF = """--- a/src/a.py
+++ b/src/a.py
@@ -1,4 +1,5 @@
 import os
+import sys

 def f():
-    return 1
+    return 2
"""

PAYLOAD = {
    "files": [
        {"path": "src/a.py", "status": "modified", "diff": SAMPLE_DIFF, "truncated": False},
        {
            "path": "src/b.py",
            "status": "created",
            "diff": "@@ -0,0 +1,2 @@\n+new\n+file\n",
            "truncated": False,
        },
    ],
    "files_changed": 2,
}


def test_count_diff_stats_ignores_headers() -> None:
    added, removed = count_diff_stats(SAMPLE_DIFF)
    assert (added, removed) == (2, 1)


def test_parse_diff_lines_tracks_line_numbers() -> None:
    parsed = parse_diff_lines(SAMPLE_DIFF)
    kinds = [line.kind for line in parsed]
    assert kinds == ["meta", "meta", "hunk", "ctx", "add", "ctx", "ctx", "del", "add"]
    # hunk 头 @@ -1,4 +1,5 @@ 起：上下文行 1、新增行 2、空上下文行旧 2/新 3、
    # 上下文 def 行旧 3/新 4、删除行旧 4、新增行新 5。
    numbered = [(line.old_no, line.new_no) for line in parsed[3:]]
    assert numbered == [(1, 1), (None, 2), (2, 3), (3, 4), (4, None), (None, 5)]


def test_format_diff_lines_shows_number_gutter_and_content() -> None:
    text = format_diff_lines(parse_diff_lines(SAMPLE_DIFF), DARK_DIFF_COLORS, max_lines=None)
    plain = text.plain
    assert "1   import os" in plain
    assert "2 + import sys" in plain
    assert "4 -     return 1" in plain
    assert "5 +     return 2" in plain
    assert "@@ -1,4 +1,5 @@" in plain


def test_format_diff_lines_truncates_with_note() -> None:
    text = format_diff_lines(parse_diff_lines(SAMPLE_DIFF), DARK_DIFF_COLORS, max_lines=3)
    assert "… 还有 6 行" in text.plain


def test_format_turn_diff_summary_single_and_multi() -> None:
    single = format_turn_diff_summary({"files": [PAYLOAD["files"][0]]}, DARK_DIFF_COLORS).plain
    assert single.startswith("• 已修改 src/a.py (+2 -1)")
    multi = format_turn_diff_summary(PAYLOAD, DARK_DIFF_COLORS).plain
    assert "• 已修改 2 个文件 (+4 -1)" in multi
    assert "└ src/a.py (+2 -1)" in multi
    assert "└ src/b.py (+2 -0)" in multi


def test_format_turn_diff_empty_payload_is_empty() -> None:
    assert format_turn_diff_summary({}, DARK_DIFF_COLORS).plain == ""
    assert format_turn_diff({}, DARK_DIFF_COLORS).plain == ""


def test_format_turn_diff_renders_hunks() -> None:
    plain = format_turn_diff(PAYLOAD, DARK_DIFF_COLORS).plain
    assert "• 已修改 2 个文件 (+4 -1)" in plain
    assert "└ src/a.py" in plain
    assert "import sys" in plain
    assert "return 1" in plain


def test_format_diff_text_renders_raw_git_diff() -> None:
    plain = format_diff_text(SAMPLE_DIFF, DARK_DIFF_COLORS).plain
    assert "import sys" in plain
    assert "return 1" in plain
    assert "--- a/src/a.py" in plain
