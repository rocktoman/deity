"""@文件/@symbol 引用解析：工作区边界校验与大小上限。

@ 后跟相对路径时解析为文件引用（读内容拼进任务）；
@ 后跟符号名时复用 LexicalCodeSearch.find_symbol() 定位定义。
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from deity.core.contracts.code_intelligence import ReferenceQuery

_MENTION_RE = re.compile(r"(?<!\S)@([^\s@，。；：！？、,;:!?)\]}]+)")
_TRAILING_PUNCT = ".,;:!?)]}，。；：！？、"


@dataclass(frozen=True)
class Mention:
    """一条已解析的引用：文件取全文，符号取定义位置。"""

    kind: Literal["file", "symbol"]
    name: str
    path: str | None
    excerpt: str


def find_mentions(text: str) -> tuple[str, ...]:
    """找出文本里的 @引用 token；邮箱里的 @ 不算（前面不是空白/行首）。"""
    found: list[str] = []
    for match in _MENTION_RE.finditer(text):
        token = match.group(1).rstrip(_TRAILING_PUNCT)
        if token:
            found.append(token)
    return tuple(found)


async def resolve_mentions(
    text: str,
    workspace: Path,
    *,
    max_bytes: int = 2_000_000,
    symbol_search: Any | None = None,
) -> tuple[Mention, ...]:
    """解析全部 @引用：越界、超大、不存在都报 ValueError，不发送任务。"""
    root = workspace.resolve()
    mentions: list[Mention] = []
    for token in find_mentions(text):
        resolved = (root / token).resolve()
        if resolved.is_relative_to(root) and resolved.is_file():
            size = resolved.stat().st_size
            if size > max_bytes:
                raise ValueError(f"引用文件过大：{token}（{size} 字节，上限 {max_bytes} 字节）")
            content = resolved.read_bytes().decode("utf-8", errors="replace")
            mentions.append(Mention(kind="file", name=token, path=token, excerpt=content))
            continue
        if not resolved.is_relative_to(root):
            raise ValueError(f"引用越界：{token} 不在工作区内")
        if symbol_search is None:
            if "/" in token or "." in token:
                raise ValueError(f"引用不存在：{token}")
            raise ValueError(f"符号 {token} 无法解析：代码智能未启用或不可用")
        result = await symbol_search.find_symbol(ReferenceQuery(token))
        locations = getattr(result, "locations", ())
        if not locations:
            raise ValueError(f"未找到符号：{token}")
        hit = locations[0]
        excerpt = f"{hit.path}:{hit.line} {hit.kind} {hit.symbol}"
        mentions.append(Mention(kind="symbol", name=token, path=str(hit.path), excerpt=excerpt))
    return tuple(mentions)


def expand_task(task: str, mentions: tuple[Mention, ...]) -> str:
    """把引用以结构化形式拼进任务；无引用时原样返回。"""
    if not mentions:
        return task
    blocks = ["\n\n引用内容："]
    for mention in mentions:
        if mention.kind == "file":
            blocks.append(f"文件 {mention.path}：\n```\n{mention.excerpt}\n```")
        else:
            blocks.append(f"符号 {mention.name}（{mention.path}）：\n{mention.excerpt}")
    return task + "\n".join(blocks)


def complete_paths(prefix: str, workspace: Path, *, limit: int = 200) -> tuple[str, ...]:
    """补全 @路径：工作区内以 prefix 开头的相对路径（跳过隐藏文件与目录）。"""
    root = workspace.resolve()
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        text = relative.as_posix()
        if text.startswith(prefix):
            found.append(text)
            if len(found) >= limit:
                break
    return tuple(found)


__all__ = [
    "Mention",
    "complete_paths",
    "expand_task",
    "find_mentions",
    "resolve_mentions",
]
