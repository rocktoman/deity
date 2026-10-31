"""Bounded, explicit Skill discovery and loading."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from deity.core.contracts import AgentRequest, ToolCall, ToolResult

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


@dataclass(frozen=True)
class SkillManifest:
    name: str
    description: str
    source: str
    path: Path
    digest: str
    root: Path


class SkillCatalog:
    """Read only bounded metadata until a model explicitly selects a Skill."""

    def __init__(self, roots: tuple[tuple[str, Path], ...]) -> None:
        self._skills: dict[str, SkillManifest] = {}
        for source, root in roots:
            if not root.exists():
                continue
            resolved_root = root.resolve(strict=True)
            directories = sorted(root.iterdir())
            if len(directories) > 64:
                raise ValueError("too many Skills")
            for directory in directories:
                if not directory.is_dir() or directory.is_symlink():
                    continue
                path = directory / "SKILL.md"
                if path.is_symlink() or not path.is_file():
                    continue
                if not path.resolve(strict=True).is_relative_to(resolved_root):
                    raise ValueError("Skill escapes configured root")
                if path.stat().st_size > 16_000:
                    raise ValueError("Skill too large")
                with path.open(encoding="utf-8") as stream:
                    header = stream.read(2_048)
                name, description = self._metadata(header)
                if name != directory.name or name in self._skills:
                    raise ValueError("Skill name mismatch or duplicate")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self._skills[name] = SkillManifest(
                    name, description, source, path, digest, resolved_root
                )

    @staticmethod
    def _metadata(header: str) -> tuple[str, str]:
        if not header.startswith("---\n"):
            raise ValueError("Skill frontmatter missing")
        marker = header.find("\n---\n", 4)
        if marker < 0:
            raise ValueError("Skill frontmatter too large or malformed")
        values: dict[str, str] = {}
        for line in header[4:marker].splitlines():
            if ":" not in line:
                raise ValueError("Skill frontmatter malformed")
            key, value = line.split(":", 1)
            if key in values:
                raise ValueError("duplicate Skill metadata")
            values[key] = value.strip().strip("\"'")
        name = values.get("name", "")
        description = values.get("description", "")
        if not _NAME.fullmatch(name) or not description or len(description) > 500:
            raise ValueError("invalid Skill metadata")
        return name, description

    def index(self) -> tuple[dict[str, str], ...]:
        """Expose only metadata to the initial model context."""
        return tuple(
            {"name": item.name, "description": item.description, "source": item.source}
            for item in self._skills.values()
        )

    def fingerprint(self) -> str:
        """Detect edits to any indexed Skill before a resumed Run."""
        payload = [(item.name, item.source, item.digest) for item in self._skills.values()]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def inspect(self, name: str) -> SkillManifest:
        return self._skills[name]

    def load(self, name: str) -> tuple[str, str]:
        """Load one selected body and return its content hash."""
        manifest = self.inspect(name)
        if (
            manifest.path.is_symlink()
            or not manifest.path.resolve(strict=True).is_relative_to(manifest.root)
            or manifest.path.stat().st_size > 16_000
        ):
            raise ValueError("Skill changed or too large")
        body = manifest.path.read_text(encoding="utf-8")
        digest = hashlib.sha256(body.encode()).hexdigest()
        if digest != manifest.digest:
            raise ValueError("Skill changed after discovery")
        current_name, _ = self._metadata(body[:2_048])
        if current_name != name:
            raise ValueError("Skill changed")
        return body, digest

    def resource(self, name: str, relative_path: str) -> tuple[str, str]:
        """Read one explicitly requested text resource within the selected Skill."""
        manifest = self.inspect(name)
        if manifest.path.is_symlink() or not manifest.path.resolve(strict=True).is_relative_to(
            manifest.root
        ):
            raise ValueError("Skill changed after discovery")
        if hashlib.sha256(manifest.path.read_bytes()).hexdigest() != manifest.digest:
            raise ValueError("Skill changed after discovery")
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) > 5:
            raise ValueError("Skill resource boundary")
        root = manifest.path.parent.resolve(strict=True)
        path = root / relative
        candidate = root
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink():
                raise ValueError("Skill resource symlink")
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("Skill resource boundary")
        if not path.is_file() or path.stat().st_size > 16_000:
            raise ValueError("Skill resource unavailable")
        content = path.read_text(encoding="utf-8")
        return content, hashlib.sha256(content.encode()).hexdigest()


class SkillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class SkillResourceArgs(SkillArgs):
    path: str


class SkillDispatcher:
    """Expose explicit loading as read-only tools within the normal Agent Loop."""

    def __init__(self, inner: Any, catalog: SkillCatalog) -> None:
        self.inner = inner
        self.catalog = catalog

    def definitions(self) -> tuple[dict[str, Any], ...]:
        inner_definitions: tuple[dict[str, Any], ...] = self.inner.definitions()
        return inner_definitions + (
            {
                "name": "load_skill",
                "description": "Load one selected Skill body; does not run scripts",
                "parameters": SkillArgs.model_json_schema(),
            },
            {
                "name": "load_skill_resource",
                "description": "Load one text resource from a selected Skill",
                "parameters": SkillResourceArgs.model_json_schema(),
            },
        )

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        if call.name not in {"load_skill", "load_skill_resource"}:
            result: ToolResult = await self.inner.dispatch(call, context)
            return result
        try:
            if call.name == "load_skill":
                args = SkillArgs.model_validate(call.arguments)
                body, digest = self.catalog.load(args.name)
                return ToolResult(
                    call.call_id, "ok", body, evidence_id=f"skill:{args.name}:{digest}"
                )
            args2 = SkillResourceArgs.model_validate(call.arguments)
            content, digest = self.catalog.resource(args2.name, args2.path)
            return ToolResult(
                call.call_id, "ok", content, evidence_id=f"skill_resource:{args2.name}:{digest}"
            )
        except (ValueError, KeyError, OSError, UnicodeError):
            return ToolResult(call.call_id, "error", "skill_unavailable")

    async def prepare(self, call: ToolCall, context: AgentRequest) -> ToolResult | None:
        if call.name in {"load_skill", "load_skill_resource"}:
            return None
        prepare = getattr(self.inner, "prepare", None)
        result: ToolResult | None = await prepare(call, context) if callable(prepare) else None
        return result
