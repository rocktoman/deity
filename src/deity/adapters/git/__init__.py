"""Read-only Git/workspace snapshots and safe workspace guards."""

import asyncio
import hashlib
import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from deity.core.contracts.workspace import (
    PatchApplyResult,
    PatchApplyStatus,
    WorkspaceFileState,
    WorkspaceSnapshot,
)


class SafePatchApplier:
    """Apply one exact text replacement with preimage and atomic-write checks."""

    def __init__(self, workspace: Path, *, max_file_bytes: int = 2_000_000) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.max_file_bytes = max_file_bytes

    def apply(
        self,
        relative_path: str,
        old: str,
        new: str,
        *,
        expected_sha256: str,
    ) -> PatchApplyResult:
        """Return a typed conflict/denial without partially changing the target."""
        path = Path(relative_path)
        if path.is_absolute() or not path.parts or ".." in path.parts or ".git" in path.parts:
            return self._result(PatchApplyStatus.DENIED, relative_path, "protected_path")
        candidate = self.workspace
        for part in path.parts:
            candidate /= part
            if candidate.is_symlink():
                return self._result(PatchApplyStatus.DENIED, relative_path, "symlink_boundary")
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.workspace) or not candidate.is_file():
            return self._result(PatchApplyStatus.DENIED, relative_path, "not_regular_file")
        if candidate.stat().st_size > self.max_file_bytes:
            return self._result(PatchApplyStatus.DENIED, relative_path, "file_too_large")
        original = candidate.read_bytes()
        before = hashlib.sha256(original).hexdigest()
        if before != expected_sha256:
            return PatchApplyResult(
                PatchApplyStatus.CONFLICT,
                relative_path,
                before,
                None,
                error_code="preimage_mismatch",
            )
        if b"\0" in original:
            return PatchApplyResult(
                PatchApplyStatus.DENIED,
                relative_path,
                before,
                None,
                error_code="binary_file",
            )
        bom = original.startswith(b"\xef\xbb\xbf")
        try:
            text = original.decode("utf-8-sig")
        except UnicodeDecodeError:
            return PatchApplyResult(
                PatchApplyStatus.DENIED,
                relative_path,
                before,
                None,
                error_code="unknown_encoding",
            )
        newline = "\r\n" if text.count("\r\n") > text.count("\n") - text.count("\r\n") else "\n"
        normalized_old = old.replace("\r\n", "\n").replace("\n", newline)
        normalized_new = new.replace("\r\n", "\n").replace("\n", newline)
        if not old and original:
            return PatchApplyResult(
                PatchApplyStatus.CONFLICT,
                relative_path,
                before,
                None,
                error_code="patch_context_not_unique",
            )
        if old and text.count(normalized_old) != 1:
            return PatchApplyResult(
                PatchApplyStatus.CONFLICT,
                relative_path,
                before,
                None,
                error_code="patch_context_not_unique",
            )
        updated = text.replace(normalized_old, normalized_new, 1) if old else normalized_new
        encoded = updated.encode("utf-8")
        if bom:
            encoded = b"\xef\xbb\xbf" + encoded
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != before:
            return PatchApplyResult(
                PatchApplyStatus.CONFLICT,
                relative_path,
                before,
                None,
                error_code="preimage_mismatch",
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{candidate.name}.", dir=candidate.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(candidate.stat().st_mode))
            os.replace(temporary, candidate)
            directory_fd = os.open(candidate.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()
        after = hashlib.sha256(encoded).hexdigest()
        return PatchApplyResult(PatchApplyStatus.APPLIED, relative_path, before, after)

    @staticmethod
    def _result(status: PatchApplyStatus, path: str, error_code: str) -> PatchApplyResult:
        return PatchApplyResult(status, path, None, None, error_code=error_code)


class GitWorkspaceAdapter:
    """Capture bounded workspace state without modifying Git or user files."""

    backend_version = "git-workspace-v1"

    def __init__(
        self, workspace: Path, *, ignored: tuple[Path, ...] = (), max_files: int = 20_000
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.max_files = max_files
        expanded: set[Path] = set()
        for path in ignored:
            resolved = path.resolve(strict=False)
            expanded.update(
                {
                    resolved,
                    Path(f"{resolved}-wal"),
                    Path(f"{resolved}-shm"),
                    resolved.with_suffix(".v1.bak"),
                    resolved.with_suffix(".v2.bak"),
                    resolved.with_suffix(".v3.bak"),
                }
            )
        self.ignored = expanded

    async def capture(self, extra_paths: tuple[str, ...] = ()) -> WorkspaceSnapshot:
        """Capture Git porcelain state, or file hashes for a non-Git workspace."""
        git_head = await self._git_head()
        files = await self._git_status() if git_head is not None else self._plain_files()
        if git_head is not None and extra_paths:
            states = {item.path: item for item in files}
            for value in extra_paths:
                relative = Path(value)
                candidate = (self.workspace / relative).resolve(strict=False)
                if (
                    relative.is_absolute()
                    or not relative.parts
                    or ".." in relative.parts
                    or not candidate.is_relative_to(self.workspace)
                ):
                    raise ValueError("invalid snapshot path")
                normalized = relative.as_posix()
                if normalized not in states:
                    states[normalized] = WorkspaceFileState(
                        normalized,
                        "clean" if candidate.exists() else "absent",
                        self._hash_file(candidate),
                    )
            files = tuple(sorted(states.values(), key=lambda item: item.path))
        payload = {
            "workspace": str(self.workspace),
            "git_head": git_head,
            "files": [
                {"path": item.path, "status": item.status, "sha256": item.sha256} for item in files
            ],
            "backend_version": self.backend_version,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return WorkspaceSnapshot(
            str(uuid4()),
            str(self.workspace),
            git_head,
            files,
            digest,
            self.backend_version,
            datetime.now(UTC).isoformat(),
        )

    async def compare(self, snapshot: WorkspaceSnapshot) -> tuple[str, ...]:
        current = await self.capture(tuple(item.path for item in snapshot.files))
        if current.digest == snapshot.digest:
            return ()
        before = {item.path: (item.status, item.sha256) for item in snapshot.files}
        after = {item.path: (item.status, item.sha256) for item in current.files}
        return tuple(
            sorted(
                path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
            )
        )

    async def diff(self, *, max_chars: int = 16_000) -> str:
        """Return a bounded, read-only Git diff or a non-Git snapshot summary."""
        if await self._git_head() is None:
            snapshot = await self.capture()
            return json.dumps(
                [{"path": item.path, "status": item.status} for item in snapshot.files],
                ensure_ascii=False,
                separators=(",", ":"),
            )[:max_chars]
        process = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--no-ext-diff",
            "--",
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise OSError(stderr.decode("utf-8", errors="replace")[:1000] or "git diff failed")
        return stdout.decode("utf-8", errors="replace")[:max_chars]

    async def _git_head(self) -> str | None:
        process = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--show-toplevel",
            "HEAD",
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return None
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        if len(lines) < 2 or Path(lines[0]).resolve() != self.workspace:
            return None
        return lines[1].strip() or None

    async def _git_status(self) -> tuple[WorkspaceFileState, ...]:
        process = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            raise OSError("git status failed")
        records = stdout.decode("utf-8", errors="strict").split("\0")
        states: list[WorkspaceFileState] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if record.startswith(("? ", "! ")):
                status, path = record[0], record[2:]
            elif record.startswith("1 "):
                fields = record.split(" ", 8)
                status, path = fields[1], fields[-1]
                if fields[2].startswith("S"):
                    status = f"submodule:{status}"
            elif record.startswith("2 "):
                fields = record.split(" ", 9)
                status, path = fields[1], fields[-1]
                if fields[2].startswith("S"):
                    status = f"submodule:{status}"
                index += 1  # rename/copy source path is the following NUL record
            elif record.startswith("u "):
                fields = record.split(" ", 10)
                status, path = "conflicted", fields[-1]
            else:
                continue
            candidate = (self.workspace / path).resolve(strict=False)
            if candidate in self.ignored:
                continue
            digest = self._hash_file(candidate)
            states.append(WorkspaceFileState(path, status, digest))
            if len(states) > self.max_files:
                raise ValueError("workspace status exceeds file limit")
        return tuple(sorted(states, key=lambda item: item.path))

    def _plain_files(self) -> tuple[WorkspaceFileState, ...]:
        states: list[WorkspaceFileState] = []
        for root, dirs, names in os.walk(self.workspace, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name != ".git")
            for name in sorted(names):
                path = Path(root) / name
                resolved = path.resolve(strict=False)
                if resolved in self.ignored or path.is_symlink() or not path.is_file():
                    continue
                states.append(
                    WorkspaceFileState(
                        str(path.relative_to(self.workspace)), "present", self._hash_file(path)
                    )
                )
                if len(states) > self.max_files:
                    raise ValueError("workspace snapshot exceeds file limit")
        return tuple(states)

    @staticmethod
    def _hash_file(path: Path) -> str | None:
        if not path.is_file() or path.is_symlink():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
