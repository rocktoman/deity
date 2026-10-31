"""Bounded, explicit Skill discovery and loading."""

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from deity.core.contracts import AgentRequest, ToolCall, ToolResult

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _read_bounded_file(root: Path, parts: tuple[str, ...]) -> bytes:
    """Read through directory handles without following replaced symlinks."""
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW
        fd = os.open(root, flags | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                child = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            child = os.open(parts[-1], flags, dir_fd=fd)
            try:
                details = os.fstat(child)
                if not stat.S_ISREG(details.st_mode) or details.st_size > 16_000:
                    raise ValueError("Skill file unavailable or too large")
                content = os.read(child, 16_001)
                if len(content) > 16_000:
                    raise ValueError("Skill file too large")
                return content
            finally:
                os.close(child)
        finally:
            os.close(fd)
    except OSError:
        raise ValueError("Skill file unavailable") from None


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

    def __init__(
        self, roots: tuple[tuple[str, Path], ...], *, tolerate_errors: bool = False
    ) -> None:
        self._skills: dict[str, SkillManifest] = {}
        self.errors: list[str] = []
        for source, root in roots:
            if not root.exists():
                continue
            resolved_root = root.resolve(strict=True)
            directories = sorted(root.iterdir())
            if len(directories) > 64:
                raise ValueError("too many Skills")
            for directory in directories:
                try:
                    if not directory.is_dir() or directory.is_symlink():
                        continue
                    path = directory / "SKILL.md"
                    if path.is_symlink() or not path.is_file():
                        continue
                    if not path.resolve(strict=True).is_relative_to(resolved_root):
                        raise ValueError("Skill escapes configured root")
                    raw = _read_bounded_file(resolved_root, (directory.name, "SKILL.md"))
                    header = raw[:2_048].decode("utf-8")
                    name, description = self._metadata(header)
                    if name != directory.name:
                        raise ValueError("Skill name mismatch")
                    if name in self._skills:
                        first = self._skills[name]
                        raise ValueError(f"duplicate Skill ID; first source: {first.source}")
                    digest = hashlib.sha256(raw).hexdigest()
                    self._skills[name] = SkillManifest(
                        name, description, source, path, digest, resolved_root
                    )
                except (ValueError, OSError, UnicodeError) as exc:
                    if not tolerate_errors:
                        raise
                    self.errors.append(f"{source}:{directory.name}: {exc}")

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
        entries = tuple(
            {
                "name": item.name,
                "description": item.description,
                "source": item.source,
                "sha256": item.digest,
            }
            for item in self._skills.values()
        )
        if len(json.dumps(entries, ensure_ascii=False).encode("utf-8")) > 8_192:
            raise ValueError("Skill index exceeds context budget")
        return entries

    def fingerprint(self) -> str:
        """Detect edits to any indexed Skill before a resumed Run."""
        payload = [(item.name, item.source, item.digest) for item in self._skills.values()]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def inspect(self, name: str) -> SkillManifest:
        return self._skills[name]

    def load(self, name: str) -> tuple[str, str]:
        """Load one selected body and return its content hash."""
        manifest = self.inspect(name)
        raw = _read_bounded_file(manifest.root, (name, "SKILL.md"))
        digest = hashlib.sha256(raw).hexdigest()
        if digest != manifest.digest:
            raise ValueError("Skill changed after discovery")
        body = raw.decode("utf-8")
        current_name, _ = self._metadata(body[:2_048])
        if current_name != name:
            raise ValueError("Skill changed")
        return body, digest

    def resource(self, name: str, relative_path: str) -> tuple[str, str]:
        """Read one explicitly requested text resource within the selected Skill."""
        manifest = self.inspect(name)
        if (
            hashlib.sha256(_read_bounded_file(manifest.root, (name, "SKILL.md"))).hexdigest()
            != manifest.digest
        ):
            raise ValueError("Skill changed after discovery")
        relative = Path(relative_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or len(relative.parts) > 5
            or len(relative_path) > 1024
        ):
            raise ValueError("Skill resource boundary")
        content = _read_bounded_file(manifest.root, (name, *relative.parts)).decode("utf-8")
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
        self._active_bodies: dict[tuple[str, str], str] = {}
        self._content_bytes: dict[str, int] = {}

    def _charge(self, run_id: str, content: str) -> bool:
        current = self._content_bytes.get(run_id, 0)
        updated = current + len(content.encode("utf-8"))
        if updated > 65_536:
            return False
        self._content_bytes[run_id] = updated
        return True

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
                if not self._charge(context.run_id, body):
                    return ToolResult(call.call_id, "denied", "skill_context_budget")
                self._active_bodies[(context.run_id, args.name)] = body
                source = self.catalog.inspect(args.name).source
                return ToolResult(
                    call.call_id, "ok", body, evidence_id=f"skill:{args.name}:{digest}:{source}"
                )
            args2 = SkillResourceArgs.model_validate(call.arguments)
            active_body = self._active_bodies.get((context.run_id, args2.name))
            if active_body is None or args2.path not in active_body:
                return ToolResult(call.call_id, "denied", "skill_resource_not_referenced")
            content, digest = self.catalog.resource(args2.name, args2.path)
            if not self._charge(context.run_id, content):
                return ToolResult(call.call_id, "denied", "skill_context_budget")
            source = self.catalog.inspect(args2.name).source
            return ToolResult(
                call.call_id,
                "ok",
                content,
                evidence_id=f"skill_resource:{args2.name}:{digest}:{source}:{args2.path}",
            )
        except (ValueError, KeyError, OSError, UnicodeError):
            return ToolResult(call.call_id, "error", "skill_unavailable")

    async def prepare(self, call: ToolCall, context: AgentRequest) -> ToolResult | None:
        if call.name in {"load_skill", "load_skill_resource"}:
            return None
        prepare = getattr(self.inner, "prepare", None)
        result: ToolResult | None = await prepare(call, context) if callable(prepare) else None
        return result
