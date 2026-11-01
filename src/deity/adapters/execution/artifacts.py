"""Apply a reviewed sandbox text artifact against its captured preimage."""

import hashlib
import os
import tempfile
from pathlib import Path

from deity.adapters.git import SafePatchApplier
from deity.core.contracts.execution import ArtifactEntry
from deity.core.contracts.workspace import FileChange, PatchApplyStatus


class ArtifactApplier:
    """Perform one bounded, validated file effect; caller handles policy and audit."""

    def __init__(
        self,
        workspace: Path,
        staging: Path,
        baseline: dict[str, tuple[str, int]],
        *,
        max_file_bytes: int = 2_000_000,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.staging = staging.resolve(strict=True)
        self.baseline = baseline
        self.max_file_bytes = max_file_bytes

    def apply(self, entry: ArtifactEntry, call_id: str) -> FileChange:
        relative = Path(entry.path)
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or ".git" in relative.parts
        ):
            raise ValueError("protected artifact path")
        host = self._safe_path(self.workspace, relative)
        source = self._safe_path(self.staging, relative)
        old = self.baseline.get(entry.path)
        before = self._digest(host) if host.is_file() else None
        if before != (old[0] if old else None):
            raise ValueError("preimage mismatch")
        if entry.operation == "delete":
            if old is None or not host.is_file():
                raise ValueError("delete target missing")
            host.unlink()
            return FileChange(entry.path, "delete", before, None, call_id)
        if (
            not source.is_file()
            or source.is_symlink()
            or source.stat().st_size > self.max_file_bytes
        ):
            raise ValueError("invalid staged file")
        data = source.read_bytes()
        if b"\0" in data:
            raise ValueError("binary artifact")
        try:
            new_text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("unknown artifact encoding") from None
        if hashlib.sha256(data).hexdigest() != entry.after_sha256:
            raise ValueError("staged artifact drift")
        if entry.operation == "modify":
            if old is None or not host.is_file():
                raise ValueError("modify target missing")
            old_data = host.read_bytes()
            try:
                old_text = old_data.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValueError("unknown host encoding") from None
            result = SafePatchApplier(self.workspace, max_file_bytes=self.max_file_bytes).apply(
                entry.path,
                old_text,
                new_text,
                expected_sha256=before or "",
            )
            if result.status != PatchApplyStatus.APPLIED:
                raise ValueError(result.error_code or "patch rejected")
        elif entry.operation == "create":
            if old is not None or host.exists() or not host.parent.is_dir():
                raise ValueError("create target exists or parent missing")
            descriptor, name = tempfile.mkstemp(prefix=f".{host.name}.", dir=host.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o644)
                os.link(temporary, host)
                self._fsync_dir(host.parent)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            raise ValueError("unknown artifact operation")
        return FileChange(entry.path, entry.operation, before, self._digest(host), call_id)

    @staticmethod
    def _digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _safe_path(root: Path, relative: Path) -> Path:
        candidate = root
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink():
                raise ValueError("symlink artifact boundary")
        if not candidate.resolve(strict=False).is_relative_to(root):
            raise ValueError("artifact outside workspace")
        return candidate

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
