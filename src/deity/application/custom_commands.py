"""自定义项目/用户斜杠命令加载器（ADR-025：.deity/commands/*.md）。"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
_MAX_COMMAND_BYTES = 65_536


@dataclass(frozen=True)
class CustomCommand:
    """一条从 Markdown 加载的自定义斜杠命令。"""

    name: str
    description: str
    template: str
    source: Literal["user", "project"]
    path: Path

    @property
    def slash_name(self) -> str:
        return f"/{self.name}"

    def render(self, args: str = "") -> str:
        """按 {{args}} / $ARGUMENTS 渲染任务模板；无占位符且有参数时追加在末尾。"""
        cleaned = args.strip()
        text = self.template
        has_placeholder = "{{args}}" in text or "$ARGUMENTS" in text
        if has_placeholder:
            text = text.replace("{{args}}", cleaned).replace("$ARGUMENTS", cleaned)
            return text.strip()
        if cleaned:
            return f"{text.strip()}\n\n附加参数：{cleaned}"
        return text.strip()


def _parse_markdown(content: str, fallback_name: str) -> tuple[str, str]:
    """解析可选 frontmatter 或首行标题作为说明，正文作为提示词模板。"""
    body = content.strip()
    description = ""
    if body.startswith("---"):
        lines = body.splitlines()
        end_idx = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end_idx is not None:
            for meta in lines[1:end_idx]:
                if ":" in meta:
                    key, val = meta.split(":", 1)
                    if key.strip().lower() == "description":
                        description = val.strip().strip("\"'")
            body = "\n".join(lines[end_idx + 1 :]).strip()
    if not description:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped:
                description = stripped.lstrip("#").strip()[:60]
                break
    return description or f"自定义命令 {fallback_name}", body


class CustomCommandCatalog:
    """发现并加载用户级与项目级自定义斜杠命令（项目级覆盖同名用户级）。"""

    def __init__(self, workspace: Path, *, user_dir: Path | None = None) -> None:
        self._workspace = workspace.resolve(strict=False)
        self._user_dir = (
            user_dir if user_dir is not None else Path.home() / ".config/deity/commands"
        )
        self._project_dir = self._workspace / ".deity" / "commands"

    def list_commands(self) -> tuple[CustomCommand, ...]:
        """按名称排序返回所有有效自定义命令；不可读目录/超大文件安全跳过。"""
        found: dict[str, CustomCommand] = {}
        for source, directory in (("user", self._user_dir), ("project", self._project_dir)):
            try:
                if not directory.is_dir():
                    continue
                entries = sorted(directory.glob("*.md"))
            except OSError:
                continue
            for path in entries:
                stem = path.stem
                if _NAME_RE.fullmatch(stem) is None:
                    continue
                try:
                    if not path.is_file() or path.stat().st_size > _MAX_COMMAND_BYTES:
                        continue
                    raw = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                description, template = _parse_markdown(raw, stem)
                if not template:
                    continue
                found[stem.lower()] = CustomCommand(
                    name=stem,
                    description=description,
                    template=template,
                    source=source,  # type: ignore[arg-type]
                    path=path,
                )
        return tuple(sorted(found.values(), key=lambda item: item.name.lower()))

    def get(self, command_name: str) -> CustomCommand | None:
        """按命令名（可带或不带前导 /）查找自定义命令。"""
        needle = command_name.removeprefix("/").strip().lower()
        if not needle:
            return None
        for item in self.list_commands():
            if item.name.lower() == needle:
                return item
        return None


__all__ = ["CustomCommand", "CustomCommandCatalog"]
