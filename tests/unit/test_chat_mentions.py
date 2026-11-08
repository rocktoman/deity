"""Phase 2：@文件/@symbol 引用解析（工作区边界、大小上限、符号复用）。"""

from pathlib import Path

import pytest

from deity.interfaces.cli.chat_mentions import (
    Mention,
    complete_paths,
    expand_task,
    find_mentions,
    resolve_mentions,
)


def test_find_mentions_ignores_email_addresses() -> None:
    assert find_mentions("联系 user@example.com 讨论") == ()
    assert find_mentions("看下 @src/main.py 和 @helper") == ("src/main.py", "helper")


def test_find_mentions_strips_trailing_punctuation() -> None:
    assert find_mentions("读 @a.txt，再看 @b.py。") == ("a.txt", "b.py")


@pytest.mark.asyncio
async def test_resolve_file_mention_reads_content(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    mentions = await resolve_mentions("看 @a.txt", tmp_path)
    assert len(mentions) == 1
    assert mentions[0].kind == "file"
    assert mentions[0].path == "a.txt"
    assert mentions[0].excerpt == "hello"
    assert "hello" in expand_task("看 @a.txt", mentions)


@pytest.mark.asyncio
async def test_resolve_mention_rejects_path_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError, match="越界"):
        await resolve_mentions("看 @../secret.txt", tmp_path)


@pytest.mark.asyncio
async def test_resolve_mention_rejects_oversized_file(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x" * 100, encoding="utf-8")
    with pytest.raises(ValueError, match="过大"):
        await resolve_mentions("看 @big.txt", tmp_path, max_bytes=10)


@pytest.mark.asyncio
async def test_resolve_mention_missing_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不存在"):
        await resolve_mentions("看 @nope.txt", tmp_path)


@pytest.mark.asyncio
async def test_resolve_symbol_mention_uses_search(tmp_path: Path) -> None:
    from deity.core.contracts.code_intelligence import CodeSearchResult, SymbolLocation

    class FakeSearch:
        async def find_symbol(self, query):  # type: ignore[no-untyped-def]
            assert query.symbol == "helper"
            return CodeSearchResult(
                locations=(
                    SymbolLocation(
                        path="src/util.py",
                        line=12,
                        column=1,
                        symbol="helper",
                        language="python",
                        kind="function",
                        source="lexical",
                        confidence="exact_definition",
                    ),
                )
            )

    mentions = await resolve_mentions("看 @helper", tmp_path, symbol_search=FakeSearch())
    assert len(mentions) == 1
    assert mentions[0].kind == "symbol"
    assert mentions[0].path == "src/util.py"
    assert "helper" in mentions[0].excerpt


@pytest.mark.asyncio
async def test_symbol_without_search_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="代码智能"):
        await resolve_mentions("看 @helper", tmp_path)


def test_expand_task_without_mentions_is_identity() -> None:
    assert expand_task("普通任务", ()) == "普通任务"


def test_complete_paths_lists_workspace_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "util.py").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    assert complete_paths("src/", tmp_path) == ("src/main.py", "src/util.py")
    assert complete_paths("RE", tmp_path) == ("README.md",)
    assert complete_paths("zz", tmp_path) == ()


def test_mention_is_frozen_dataclass() -> None:
    mention = Mention(kind="file", name="a.txt", path="a.txt", excerpt="hi")
    assert mention.name == "a.txt"
    with pytest.raises(AttributeError):
        mention.excerpt = "changed"  # type: ignore[misc]
