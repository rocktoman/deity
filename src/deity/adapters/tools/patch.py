"""多文件 patch 协议：解析、四级模糊定位、先全量校验后落盘（ADR-018）。

格式参照 openai/codex 的 apply-patch 文法（Apache-2.0，
`codex-rs/apply-patch/src/parser.rs`），定位算法参照其
`seek_sequence.rs` 的四级严格度回退（精确 → 忽略行尾空白 → 忽略
首尾空白 → Unicode 标点归一），均用 Python 重新实现；来源声明见
`docs/06-legal/third-party-notices.md`。与 Codex 的一处刻意差异：
old_lines 为空的 chunk（纯插入）在锚点位置插入，而不是追加到
文件末尾。

本模块不做文件 IO：读取经回调注入，校验（plan_patch）与落盘
（调用方按 PlannedChange 写）分离，保证任一 hunk 校验失败时
整批不写。
"""

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

BEGIN_MARKER = "*** Begin Patch"
END_MARKER = "*** End Patch"
ADD_MARKER = "*** Add File: "
DELETE_MARKER = "*** Delete File: "
UPDATE_MARKER = "*** Update File: "
MOVE_MARKER = "*** Move to: "
EOF_MARKER = "*** End of File"


class PatchError(ValueError):
    """Patch 文本不合法或定位/校验失败。"""


@dataclass
class Chunk:
    context: str | None = None
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    is_eof: bool = False


@dataclass
class AddFile:
    path: str
    lines: list[str] = field(default_factory=list)


@dataclass
class DeleteFile:
    path: str


@dataclass
class UpdateFile:
    path: str
    move_to: str | None = None
    chunks: list[Chunk] = field(default_factory=list)


Hunk = AddFile | DeleteFile | UpdateFile


@dataclass(frozen=True)
class PlannedChange:
    """一个已通过全量校验、等待落盘的文件变更。"""

    path: str
    kind: str  # "add" | "delete" | "update"
    new_content: str | None
    move_to: str | None = None


def _normalise(text: str) -> str:
    """Unicode 标点归一（seek 第四级）：弯引号/破折号/异形空格 → ASCII。"""
    out: list[str] = []
    for char in text.strip():
        if unicodedata.category(char) == "Pd" or char == "−":
            out.append("-")
        elif char in "‘’‚‛":
            out.append("'")
        elif char in "“”„‟":
            out.append('"')
        elif char.isspace():
            out.append(" ")
        else:
            out.append(char)
    return "".join(out)


def seek_sequence(lines: list[str], pattern: list[str], start: int, eof: bool) -> int | None:
    """在 lines[start:] 中按四级严格度定位 pattern，返回起始下标。"""
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None
    search_start = start
    if eof:
        search_start = max(start, len(lines) - len(pattern))
    last = len(lines) - len(pattern)

    def scan(key: Callable[[str], str]) -> int | None:
        wanted = [key(item) for item in pattern]
        for index in range(search_start, last + 1):
            if [key(item) for item in lines[index : index + len(pattern)]] == wanted:
                return index
        return None

    for key in (
        lambda item: item,
        lambda item: item.rstrip(),
        lambda item: item.strip(),
        _normalise,
    ):
        found = scan(key)
        if found is not None:
            return found
    return None


def _split_content(content: str) -> tuple[list[str], bool]:
    if content == "":
        return [], False
    lines = content.split("\n")
    if content.endswith("\n"):
        return lines[:-1], True
    return lines, False


def _join_content(lines: list[str], ends_with_newline: bool) -> str:
    text = "\n".join(lines)
    return text + "\n" if ends_with_newline and lines else text


def derive_new_content(original: str, chunks: list[Chunk], path: str) -> str:
    """把一个 UpdateFile 的全部 chunk 应用到原文，返回新内容（纯函数）。"""
    lines, ends_with_newline = _split_content(original)
    line_index = 0
    for chunk in chunks:
        if chunk.context is not None:
            found = seek_sequence(lines, [chunk.context], line_index, False)
            if found is None:
                raise PatchError(f"{path}: 找不到上下文锚: {chunk.context!r}")
            line_index = found + 1
        if not chunk.old_lines:
            lines[line_index:line_index] = chunk.new_lines
            line_index += len(chunk.new_lines)
            continue
        pattern = chunk.old_lines
        new_lines = chunk.new_lines
        found = seek_sequence(lines, pattern, line_index, chunk.is_eof)
        if found is None and pattern and pattern[-1] == "":
            # 末尾空行是文件终止符的哨兵，原文行表里没有这一行，去掉后重试。
            pattern = pattern[:-1]
            trimmed = new_lines[:-1] if new_lines and new_lines[-1] == "" else new_lines
            found = seek_sequence(lines, pattern, line_index, chunk.is_eof)
            if found is not None:
                new_lines = trimmed
                ends_with_newline = True
        if found is None:
            raise PatchError(f"{path}: 定位失败，目标片段不存在或已漂移")
        lines[found : found + len(pattern)] = new_lines
        line_index = found + len(new_lines)
    return _join_content(lines, ends_with_newline)


def parse_patch(text: str) -> list[Hunk]:
    """解析 patch 文本为 hunk 列表；不合法抛 PatchError（带行号）。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or lines[index].strip() != BEGIN_MARKER:
        raise PatchError("patch 必须以 *** Begin Patch 开始")
    index += 1
    hunks: list[Hunk] = []
    while True:
        if index >= len(lines):
            raise PatchError("patch 缺少 *** End Patch")
        marker = lines[index].strip()
        if marker == END_MARKER:
            index += 1
            break
        if marker.startswith("*** Environment ID: "):
            index += 1
            continue
        if marker.startswith(ADD_MARKER):
            path = marker[len(ADD_MARKER) :].strip()
            if not path:
                raise PatchError(f"第 {index + 1} 行: Add File 缺少路径")
            index += 1
            body: list[str] = []
            while index < len(lines) and lines[index].startswith("+"):
                body.append(lines[index][1:])
                index += 1
            hunks.append(AddFile(path, body))
            continue
        if marker.startswith(DELETE_MARKER):
            path = marker[len(DELETE_MARKER) :].strip()
            if not path:
                raise PatchError(f"第 {index + 1} 行: Delete File 缺少路径")
            hunks.append(DeleteFile(path))
            index += 1
            continue
        if marker.startswith(UPDATE_MARKER):
            path = marker[len(UPDATE_MARKER) :].strip()
            if not path:
                raise PatchError(f"第 {index + 1} 行: Update File 缺少路径")
            index += 1
            move_to: str | None = None
            if index < len(lines) and lines[index].strip().startswith(MOVE_MARKER):
                move_to = lines[index].strip()[len(MOVE_MARKER) :].strip()
                if not move_to:
                    raise PatchError(f"第 {index + 1} 行: Move to 缺少路径")
                index += 1
            chunks: list[Chunk] = []
            current: Chunk | None = None
            while index < len(lines):
                raw = lines[index]
                stripped = raw.strip()
                if stripped == EOF_MARKER:
                    if current is None:
                        current = Chunk()
                        chunks.append(current)
                    current.is_eof = True
                    index += 1
                    continue
                if stripped.startswith("*** ") or stripped == END_MARKER:
                    break
                if stripped == "@@" or stripped.startswith("@@ "):
                    current = Chunk(context=stripped[2:].strip() or None)
                    chunks.append(current)
                    index += 1
                    continue
                if current is None:
                    current = Chunk()
                    chunks.append(current)
                if raw.startswith("+"):
                    current.new_lines.append(raw[1:])
                elif raw.startswith("-"):
                    current.old_lines.append(raw[1:])
                elif raw.startswith(" "):
                    current.old_lines.append(raw[1:])
                    current.new_lines.append(raw[1:])
                elif raw == "":
                    current.old_lines.append("")
                    current.new_lines.append("")
                else:
                    raise PatchError(f"第 {index + 1} 行: 无法识别的 patch 行: {raw!r}")
                index += 1
            if not chunks:
                raise PatchError(f"{path}: Update File 没有任何 chunk")
            hunks.append(UpdateFile(path, move_to, chunks))
            continue
        raise PatchError(f"第 {index + 1} 行: 无法识别的标记: {marker!r}")
    while index < len(lines):
        if lines[index].strip():
            raise PatchError("*** End Patch 之后还有内容")
        index += 1
    if not hunks:
        raise PatchError("patch 没有任何 hunk")
    return hunks


def patch_target_paths(text: str) -> list[str]:
    """尽力解析出 patch 涉及的全部路径（含 Move 目标）；失败返回空列表。"""
    try:
        hunks = parse_patch(text)
    except PatchError:
        return []
    paths: list[str] = []
    for hunk in hunks:
        paths.append(hunk.path)
        if isinstance(hunk, UpdateFile) and hunk.move_to is not None:
            paths.append(hunk.move_to)
    return paths


def plan_patch(text: str, read: Callable[[str], str | None]) -> list[PlannedChange]:
    """全量校验：读齐所有目标、算齐所有新内容，任一失败整批抛错、不落盘。

    read(path) 返回文件当前文本，不存在返回 None；read 内部的边界
    拒绝应以 PatchError 抛出（由调用方转换）。
    """
    hunks = parse_patch(text)
    seen: set[str] = set()
    for hunk in hunks:
        targets = [hunk.path]
        if isinstance(hunk, UpdateFile) and hunk.move_to is not None:
            targets.append(hunk.move_to)
        for target in targets:
            if target in seen:
                raise PatchError(f"同一 patch 内路径重复: {target}")
            seen.add(target)
    changes: list[PlannedChange] = []
    for hunk in hunks:
        if isinstance(hunk, AddFile):
            if read(hunk.path) is not None:
                raise PatchError(f"{hunk.path}: Add File 目标已存在")
            content = "".join(line + "\n" for line in hunk.lines)
            changes.append(PlannedChange(hunk.path, "add", content))
        elif isinstance(hunk, DeleteFile):
            if read(hunk.path) is None:
                raise PatchError(f"{hunk.path}: Delete File 目标不存在")
            changes.append(PlannedChange(hunk.path, "delete", None))
        else:
            original = read(hunk.path)
            if original is None:
                raise PatchError(f"{hunk.path}: Update File 目标不存在")
            if hunk.move_to is not None and read(hunk.move_to) is not None:
                raise PatchError(f"{hunk.move_to}: Move to 目标已存在")
            new_content = derive_new_content(original, hunk.chunks, hunk.path)
            changes.append(PlannedChange(hunk.path, "update", new_content, hunk.move_to))
    return changes


__all__ = [
    "AddFile",
    "Chunk",
    "DeleteFile",
    "Hunk",
    "PatchError",
    "PlannedChange",
    "UpdateFile",
    "derive_new_content",
    "parse_patch",
    "patch_target_paths",
    "plan_patch",
    "seek_sequence",
]
