from pathlib import Path

import pytest

from deity.adapters.skills import SkillCatalog, SkillDispatcher
from deity.adapters.tools import LocalToolDispatcher
from deity.core.contracts import AgentRequest, ToolCall


def create_skill(root: Path, name: str, body: str = "instructions") -> None:
    directory = root / name
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Use {name} when selected\n---\n{body}"
    )


@pytest.mark.asyncio
async def test_ten_skills_index_only_and_explicit_load(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    for index in range(10):
        create_skill(root, f"skill{index}", f"body {index}")
    catalog = SkillCatalog((("project", root),))
    index = catalog.index()
    assert len(index) == 10
    assert "body 3" not in repr(index)
    dispatcher = SkillDispatcher(LocalToolDispatcher(), catalog)
    request = AgentRequest("s", "t", str(tmp_path), "use skill3", "fake")
    result = await dispatcher.dispatch(ToolCall("c1", "load_skill", {"name": "skill3"}), request)
    assert result.status == "ok" and "body 3" in result.output
    assert result.evidence_id is not None and result.evidence_id.startswith("skill:skill3:")


@pytest.mark.asyncio
async def test_resource_requires_prior_activation_and_body_reference(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    create_skill(root, "safe", "Read resources/allowed.txt")
    resource_dir = root / "safe/resources"
    resource_dir.mkdir()
    (resource_dir / "allowed.txt").write_text("allowed")
    (resource_dir / "unlisted.txt").write_text("unlisted")
    dispatcher = SkillDispatcher(LocalToolDispatcher(), SkillCatalog((("project", root),)))
    request = AgentRequest("s", "t", str(tmp_path), "use safe", "fake")
    early = await dispatcher.dispatch(
        ToolCall("c1", "load_skill_resource", {"name": "safe", "path": "resources/allowed.txt"}),
        request,
    )
    assert early.status == "denied"
    assert (
        await dispatcher.dispatch(ToolCall("c2", "load_skill", {"name": "safe"}), request)
    ).status == "ok"
    unlisted = await dispatcher.dispatch(
        ToolCall("c3", "load_skill_resource", {"name": "safe", "path": "resources/unlisted.txt"}),
        request,
    )
    assert unlisted.status == "denied"
    listed = await dispatcher.dispatch(
        ToolCall("c4", "load_skill_resource", {"name": "safe", "path": "resources/allowed.txt"}),
        request,
    )
    assert listed.status == "ok" and listed.output == "allowed"


@pytest.mark.asyncio
async def test_skill_loaded_content_has_run_budget(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    create_skill(root, "safe", "Read large.txt")
    (root / "safe/large.txt").write_text("x" * 15_000)
    dispatcher = SkillDispatcher(LocalToolDispatcher(), SkillCatalog((("project", root),)))
    request = AgentRequest("s", "t", str(tmp_path), "use safe", "fake", run_id="run")
    assert (
        await dispatcher.dispatch(ToolCall("load", "load_skill", {"name": "safe"}), request)
    ).status == "ok"
    statuses = []
    for index in range(5):
        result = await dispatcher.dispatch(
            ToolCall(f"r{index}", "load_skill_resource", {"name": "safe", "path": "large.txt"}),
            request,
        )
        statuses.append(result.status)
    assert statuses == ["ok", "ok", "ok", "ok", "denied"]


def test_duplicate_and_outside_resource_rejected(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    create_skill(first, "same")
    create_skill(second, "same")
    with pytest.raises(ValueError):
        SkillCatalog((("user", first), ("project", second)))
    catalog = SkillCatalog((("project", first),))
    with pytest.raises(ValueError):
        catalog.resource("same", "../outside.txt")


def test_malformed_frontmatter_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    directory = root / "bad"
    directory.mkdir()
    (directory / "SKILL.md").write_text("not frontmatter")
    with pytest.raises(ValueError):
        SkillCatalog((("project", root),))


def test_resource_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    create_skill(root, "safe")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (root / "safe" / "link.txt").symlink_to(outside)
    catalog = SkillCatalog((("project", root),))
    with pytest.raises(ValueError):
        catalog.resource("safe", "link.txt")


def test_loading_skill_does_not_execute_its_script(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    create_skill(root, "safe", "Run scripts/do_it.py")
    script = root / "safe/scripts/do_it.py"
    script.parent.mkdir()
    script.write_text("from pathlib import Path\nPath('executed').write_text('bad')\n")
    catalog = SkillCatalog((("project", root),))
    body, _digest = catalog.load("safe")
    assert "scripts/do_it.py" in body
    assert not (tmp_path / "executed").exists()


def test_skill_changed_after_discovery_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    create_skill(root, "safe", "original")
    catalog = SkillCatalog((("project", root),))
    (root / "safe/SKILL.md").write_text("---\nname: safe\ndescription: Changed\n---\nnew body")
    with pytest.raises(ValueError, match="changed"):
        catalog.load("safe")


def test_skill_index_has_total_context_limit(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    for index in range(30):
        directory = root / f"skill{index}"
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            f"---\nname: skill{index}\ndescription: {'x' * 400}\n---\nbody"
        )
    catalog = SkillCatalog((("project", root),))
    with pytest.raises(ValueError, match="index"):
        catalog.index()
