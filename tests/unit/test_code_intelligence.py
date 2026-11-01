from pathlib import Path

import pytest

from deity.adapters.code_intelligence.lexical import LexicalCodeSearch
from deity.core.contracts.code_intelligence import ReferenceQuery


@pytest.mark.asyncio
async def test_three_language_definitions_and_textual_references(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        "class Shared:\n    pass\n\ndef helper():\n    return Shared()\n", encoding="utf-8"
    )
    (tmp_path / "Sample.java").write_text(
        "interface Shared {}\nclass Demo {\n  void helper() {}\n}\n", encoding="utf-8"
    )
    (tmp_path / "sample.ts").write_text(
        "type Shared = string;\nexport function helper(): Shared { return 'x'; }\n",
        encoding="utf-8",
    )
    search = LexicalCodeSearch(tmp_path, cache_path=tmp_path / "cache.json")

    shared = await search.find_symbol(ReferenceQuery("Shared"))
    assert [(item.path, item.line, item.language) for item in shared.locations] == [
        ("Sample.java", 1, "java"),
        ("sample.py", 1, "python"),
        ("sample.ts", 1, "typescript"),
    ]
    helper = await search.find_symbol(ReferenceQuery("helper"))
    assert {item.language for item in helper.locations} == {"python", "java", "typescript"}
    references = await search.find_references(ReferenceQuery("Shared"))
    assert references.locations
    assert all(item.confidence == "textual_reference" for item in references.locations)


@pytest.mark.asyncio
async def test_cache_invalidates_on_content_change_and_deleted_file(tmp_path: Path) -> None:
    source = tmp_path / "feature.py"
    source.write_text("def old_name():\n    pass\n", encoding="utf-8")
    cache = tmp_path / ".cache/index.json"
    search = LexicalCodeSearch(tmp_path, cache_path=cache)
    assert (await search.find_symbol(ReferenceQuery("old_name"))).locations
    assert cache.exists()

    source.write_text("def new_name():\n    pass\n", encoding="utf-8")
    assert not (await search.find_symbol(ReferenceQuery("old_name"))).locations
    assert (await search.find_symbol(ReferenceQuery("new_name"))).locations
    source.unlink()
    assert not (await search.find_symbol(ReferenceQuery("new_name"))).locations


@pytest.mark.asyncio
async def test_binary_oversized_and_result_limit_are_explicit(tmp_path: Path) -> None:
    (tmp_path / "binary.py").write_bytes(b"\x00def hidden():\n")
    (tmp_path / "large.py").write_text("def large():\n    pass\n" + "x" * 100, encoding="utf-8")
    for index in range(3):
        (tmp_path / f"item{index}.py").write_text("def duplicate():\n    pass\n", encoding="utf-8")
    search = LexicalCodeSearch(
        tmp_path,
        cache_path=tmp_path / "cache.json",
        max_file_bytes=40,
        result_limit=2,
    )
    result = await search.find_symbol(ReferenceQuery("duplicate"))
    assert len(result.locations) == 2 and result.partial
    hidden = await search.find_symbol(ReferenceQuery("hidden"))
    assert not hidden.locations and any("binary" in item for item in hidden.diagnostics)
    large = await search.find_symbol(ReferenceQuery("large"))
    assert not large.locations and any("large.py" in item for item in large.diagnostics)
