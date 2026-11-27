"""Bounded read-only workspace fingerprint for recovery preflight."""

import hashlib
import os
import stat
import subprocess
from pathlib import Path

_IGNORED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".Trash",
    "Library",
    "System",
    "Volumes",
    "Applications",
    "dev",
    "proc",
    "sys",
    "private",
}
_MAX_FILES = 10_000
_MAX_BYTES = 100_000_000


def workspace_digest(root: Path, excluded: tuple[Path, ...] = ()) -> str:
    """Hash workspace content and Git HEAD without storing file contents.

    Gracefully skips unreadable/special entries and bounds giant directories
    (such as `/` or `~`) with a deterministic marker so chat works in any cwd.
    """
    root = root.resolve(strict=True)
    excluded_names = {
        candidate
        for path in excluded
        for candidate in (
            str(path.resolve(strict=False)),
            str(path.resolve(strict=False)) + "-wal",
            str(path.resolve(strict=False)) + "-shm",
            str(path.with_suffix(".v1.bak").resolve(strict=False)),
        )
    }
    digest = hashlib.sha256()
    seen = 0
    total = 0
    bounded = False
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda _: None):
        dirs[:] = sorted(name for name in dirs if name not in _IGNORED_DIRS)
        for name in sorted(files):
            path = Path(directory) / name
            if str(path.resolve(strict=False)) in excluded_names:
                continue
            relative = path.relative_to(root).as_posix()
            digest.update(os.fsencode(relative) + b"\0")
            try:
                st = path.lstat()
            except OSError:
                digest.update(b"unreadable:" + os.fsencode(relative))
                continue
            if stat.S_ISLNK(st.st_mode):
                try:
                    digest.update(b"symlink:" + os.fsencode(os.readlink(path)))
                except OSError:
                    digest.update(b"unreadable:" + os.fsencode(relative))
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            seen += 1
            if seen > _MAX_FILES:
                digest.update(b"bounded_limit_reached\0")
                bounded = True
                break
            try:
                with path.open("rb") as handle:
                    while chunk := handle.read(65_536):
                        total += len(chunk)
                        if total > _MAX_BYTES:
                            digest.update(b"bounded_limit_reached\0")
                            bounded = True
                            break
                        digest.update(chunk)
            except OSError:
                digest.update(b"unreadable:" + os.fsencode(relative))
            if bounded:
                break
        if bounded:
            break
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if head.returncode == 0:
            digest.update(head.stdout.strip().encode("ascii"))
    except (OSError, subprocess.SubprocessError):
        pass
    return digest.hexdigest()
