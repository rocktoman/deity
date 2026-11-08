"""V1.0 S2：发布产物本地可跑部分。

覆盖：uv build 产物存在、SHA256 校验和可验证、SBOM 含直接依赖、
uv.lock 与 pyproject 一致。CI 专属部分（Windows 矩阵、pip-audit 扫描
在 GitHub runner 上跑）不在本机验证，见 V1.0 验收记录 NOT_RUN。
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
DIRECT_DEPENDENCIES = {
    "typer",
    "rich",
    "pydantic",
    "openai",
    "anthropic",
    "aiosqlite",
    "httpx2",
    "mcp",
    "jsonschema",
}


def _uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "uv"
    assert candidate.exists(), "本机未找到 uv"
    return str(candidate)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{Path.home() / '.local' / 'bin'}:{env.get('PATH', '')}"
    return env


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
    return subprocess.run(args, cwd=ROOT, env=_env(), capture_output=True, text=True, **kwargs)


def test_lockfile_matches_pyproject() -> None:
    result = _run([_uv(), "lock", "--check"])
    assert result.returncode == 0, result.stderr[-2000:]


def test_build_produces_wheel_and_sdist(tmp_path: Path) -> None:
    out = tmp_path / "dist"
    result = _run([_uv(), "build", "--out-dir", str(out)])
    assert result.returncode == 0, result.stderr[-2000:]
    wheels = sorted(out.glob("deity-1.0.0-*.whl"))
    sdists = sorted(out.glob("deity-1.0.0.tar.gz"))
    assert len(wheels) == 1, [path.name for path in out.iterdir()]
    assert len(sdists) == 1, [path.name for path in out.iterdir()]


def test_checksums_verify(tmp_path: Path) -> None:
    out = tmp_path / "dist"
    result = _run([_uv(), "build", "--out-dir", str(out)])
    assert result.returncode == 0, result.stderr[-2000:]
    checksums = tmp_path / "SHA256SUMS"
    lines = []
    for artifact in sorted(out.iterdir()):
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        lines.append(f"{digest}  {artifact.name}\n")
    checksums.write_text("".join(lines), encoding="utf-8")
    verify = subprocess.run(
        ["sha256sum", "-c", str(checksums)],
        cwd=out,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert verify.stdout.count(": OK") == len(lines)


def test_sbom_contains_direct_dependencies(tmp_path: Path) -> None:
    sbom = tmp_path / "sbom.json"
    result = _run(
        [_uv(), "export", "--locked", "--format", "cyclonedx1.5", "--output-file", str(sbom)]
    )
    assert result.returncode == 0, result.stderr[-2000:]
    data = json.loads(sbom.read_text(encoding="utf-8"))
    names = {component.get("name") for component in data.get("components", [])}
    missing = DIRECT_DEPENDENCIES - names
    assert not missing, f"SBOM 缺直接依赖：{sorted(missing)}"
    assert data.get("bomFormat") == "CycloneDX"
