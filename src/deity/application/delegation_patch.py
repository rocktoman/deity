"""ADR-021 B6：把可写子 Run 在暂存副本里的改动回收为 Deity patch 文本。

输入是宿主侧哈希对比得到的 ArtifactManifest（不信任子进程自报清单）。
每个文件的 patch 片段生成后都用 plan_patch 对基线内容自校验，校验不
过先回退整文件替换 chunk、再不过则跳过该文件并记录原因；二进制/不可
解码/无尾换行的新增文件同样跳过。整体超限时不返回半截 patch。
"""

import difflib
from dataclasses import dataclass
from pathlib import Path

from deity.adapters.tools.patch import PatchError, plan_patch
from deity.core.contracts.execution import ArtifactEntry


@dataclass(frozen=True)
class HarvestedPatch:
    """一次回收的结果：patch 文本（或 None）、纳入文件与跳过文件。"""

    patch: str | None
    files: tuple[str, ...]
    skipped: tuple[str, ...]
    reason: str | None = None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _update_section(path: str, before: str, after: str) -> str | None:
    """生成 Update File 片段并自校验；失败回退整文件替换，再失败返回 None。"""
    before_lines = before.split("\n")
    after_lines = after.split("\n")
    if before.endswith("\n"):
        before_lines = before_lines[:-1]
    if after.endswith("\n"):
        after_lines = after_lines[:-1]

    def verify(section: str) -> bool:
        text = f"*** Begin Patch\n{section}*** End Patch"
        try:
            changes = plan_patch(text, lambda p: before if p == path else None)
        except PatchError:
            return False
        return len(changes) == 1 and changes[0].new_content == after

    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    grouped: list[str] = [f"*** Update File: {path}"]
    for group in matcher.get_grouped_opcodes(3):
        grouped.append("@@")
        for tag, i1, i2, j1, j2 in group:
            if tag in ("replace", "delete"):
                grouped.extend(f"-{line}" for line in before_lines[i1:i2])
            if tag in ("replace", "insert"):
                grouped.extend(f"+{line}" for line in after_lines[j1:j2])
            if tag == "equal":
                grouped.extend(f" {line}" for line in before_lines[i1:i2])
    section = "\n".join(grouped) + "\n"
    if verify(section):
        return section
    fallback = [f"*** Update File: {path}", "@@"]
    fallback.extend(f"-{line}" for line in before_lines)
    fallback.extend(f"+{line}" for line in after_lines)
    section = "\n".join(fallback) + "\n"
    return section if verify(section) else None


def build_patch(
    workspace: Path,
    stage: Path,
    entries: tuple[ArtifactEntry, ...],
    *,
    max_chars: int,
) -> HarvestedPatch:
    """按 manifest 逐文件生成 patch；任一文件跳过都会如实记录。"""
    if not entries:
        return HarvestedPatch(None, (), (), None)
    sections: list[str] = []
    files: list[str] = []
    skipped: list[str] = []
    for entry in entries:
        relative = entry.path
        if entry.operation == "create":
            content = _read_text(stage / relative)
            if content is None or (content and not content.endswith("\n")):
                skipped.append(relative)
                continue
            lines = content.split("\n")[:-1] if content else []
            body = "".join(f"+{line}\n" for line in lines)
            sections.append(f"*** Add File: {relative}\n{body}")
            files.append(relative)
        elif entry.operation == "delete":
            sections.append(f"*** Delete File: {relative}\n")
            files.append(relative)
        else:
            before = _read_text(workspace / relative)
            after = _read_text(stage / relative)
            if before is None or after is None:
                skipped.append(relative)
                continue
            section = _update_section(relative, before, after)
            if section is None:
                skipped.append(relative)
                continue
            sections.append(section)
            files.append(relative)
    if not files:
        return HarvestedPatch(None, (), tuple(skipped), "no_applicable_changes")
    patch = "*** Begin Patch\n" + "".join(sections) + "*** End Patch"
    if len(patch) > max_chars:
        return HarvestedPatch(None, (), tuple(files + skipped), "patch_exceeds_max_chars")
    return HarvestedPatch(patch, tuple(files), tuple(skipped), None)
