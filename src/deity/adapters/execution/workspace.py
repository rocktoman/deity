"""Copy a bounded workspace without following links or copying common secrets."""

import fnmatch
import hashlib
import os
import shutil
from pathlib import Path
from typing import Literal

from deity.core.contracts.execution import ArtifactEntry, ArtifactManifest

EXCLUDED = {".git", ".deity", ".env", ".ssh"}


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.digest()


class WorkspaceStager:
    """Stage regular files outside the user workspace for container execution."""

    def __init__(
        self,
        workspace: Path,
        destination: Path,
        *,
        max_files: int = 50_000,
        max_bytes: int = 1_073_741_824,
        excluded_paths: tuple[Path, ...] = (),
        excluded_patterns: tuple[str, ...] = (),
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.destination = destination
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.excluded_paths = {path.resolve(strict=False) for path in excluded_paths}
        self.excluded_patterns = excluded_patterns

    def _excluded(self, path: Path) -> bool:
        relative = path.relative_to(self.workspace).as_posix()
        return any(
            fnmatch.fnmatchcase(relative, pattern) or fnmatch.fnmatchcase(path.name, pattern)
            for pattern in self.excluded_patterns
        )

    def copy(self) -> Path:
        if self.destination.resolve().is_relative_to(self.workspace):
            raise ValueError("staging inside workspace")
        if self.destination.exists():
            raise ValueError("staging already exists")
        files = 0
        total = 0
        copied: list[tuple[Path, Path]] = []
        self.destination.mkdir(mode=0o700, parents=True)
        try:
            for base, directories, names in os.walk(self.workspace, followlinks=False):
                root = Path(base)
                directories[:] = sorted(
                    name
                    for name in directories
                    if name not in EXCLUDED
                    and not name.startswith(".env")
                    and not self._excluded(root / name)
                )
                for name in directories:
                    if (root / name).is_symlink():
                        raise ValueError("symlink in workspace")
                for name in sorted(names):
                    source = root / name
                    if name in EXCLUDED or name.startswith(".env") or self._excluded(source):
                        continue
                    if source.resolve(strict=False) in self.excluded_paths:
                        continue
                    if source.is_symlink():
                        raise ValueError("symlink in workspace")
                    if not source.is_file() or source.stat().st_nlink != 1:
                        raise ValueError("non-regular file in workspace")
                    size = source.stat().st_size
                    files += 1
                    total += size
                    if files > self.max_files or total > self.max_bytes:
                        raise ValueError("workspace staging limit exceeded")
                    target = self.destination / source.relative_to(self.workspace)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target, follow_symlinks=False)
                    target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
                    copied.append((source, target))
            for source, target in copied:
                if (
                    source.is_symlink()
                    or not source.is_file()
                    or source.stat().st_nlink != 1
                    or source.stat().st_size != target.stat().st_size
                    or _file_digest(source) != _file_digest(target)
                ):
                    raise ValueError("workspace_changed_during_stage")
            return self.destination
        except BaseException:
            shutil.rmtree(self.destination)
            raise


def file_hashes(
    root: Path, *, max_files: int = 50_000, max_bytes: int = 1_073_741_824
) -> dict[str, tuple[str, int]]:
    """Hash regular staged files for host-side artifact comparison."""
    found: dict[str, tuple[str, int]] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise ValueError("unsafe staged artifact")
        if path.is_file():
            size = path.stat().st_size
            total += size
            if len(found) + 1 > max_files or total > max_bytes:
                raise ValueError("artifact scan limit exceeded")
            digest = _file_digest(path)
            if path.stat().st_size != size:
                raise ValueError("artifact changed during scan")
            found[path.relative_to(root).as_posix()] = (digest.hex(), size)
    return found


def collect_manifest(
    execution_id: str,
    before: dict[str, tuple[str, int]],
    after: dict[str, tuple[str, int]],
) -> ArtifactManifest:
    """Compare trusted host scans; never trust a manifest supplied by the container."""
    entries = []
    for path in sorted(before.keys() | after.keys()):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        operation: Literal["create", "modify", "delete"] = (
            "create" if old is None else "delete" if new is None else "modify"
        )
        entries.append(
            ArtifactEntry(
                path,
                operation,
                old[0] if old else None,
                new[0] if new else None,
                new[1] if new else None,
            )
        )
    return ArtifactManifest(execution_id, tuple(entries), "collected")
