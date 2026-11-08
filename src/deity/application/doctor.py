"""deity doctor：真实环境检查，全部输出经脱敏。"""

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from deity.application.config import ConfigResolver

CheckStatus = Literal["ok", "warn", "fail"]


class Redactor:
    """脱敏：密钥键值与明显密钥模式一律替换为 ***，不读密钥值。"""

    _KEY_VALUE = re.compile(
        r"(?i)(api[_-]?key|token|secret|password|passwd|auth|access[_-]?key)"
        r"(\s*[:=]\s*[\"']?)[^\"'\s,}]+"
    )
    _TOKEN_PATTERNS = (
        re.compile(r"sk-[A-Za-z0-9]{8,}"),
        re.compile(r"ghp_[A-Za-z0-9]{20,}"),
        re.compile(r"gho_[A-Za-z0-9]{20,}"),
        re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
    )

    @classmethod
    def redact(cls, text: str) -> str:
        redacted = cls._KEY_VALUE.sub(lambda match: match.group(1) + match.group(2) + "***", text)
        for pattern in cls._TOKEN_PATTERNS:
            redacted = pattern.sub("***", redacted)
        return redacted


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    detail: str


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """任一检查为 fail 即整体失败。"""
        return any(check.status == "fail" for check in self.checks)

    def to_text(self) -> str:
        """脱敏后的报告文本。"""
        lines = [
            f"{check.name}: {check.status} {Redactor.redact(check.detail)}" for check in self.checks
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        """可序列化的报告。"""
        return {
            "failed": self.failed,
            "sources": dict(self.sources),
            "checks": [
                {"name": check.name, "status": check.status, "detail": check.detail}
                for check in self.checks
            ],
        }


def _run_command(argv: list[str], timeout_seconds: float = 10.0) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_seconds, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, Redactor.redact(str(exc))
    output = (completed.stdout + completed.stderr).strip().splitlines()
    return completed.returncode == 0, Redactor.redact(output[0] if output else "")


def run_doctor(config_path: Path, workspace: Path, *, check_docker: bool = False) -> DoctorReport:
    """真实检查 Python/依赖/SQLite/Git/rg/模型 env/配置/Docker。"""
    checks: list[DoctorCheck] = []
    try:
        config, sources = ConfigResolver.resolve(
            {}, workspace / ".deity" / "config.toml", config_path
        )
        config_error: str | None = None
    except ValueError:
        config, sources = None, {}
        config_error = "配置无效：请检查 TOML 字段和权限设置"

    if sys.version_info >= (3, 12):  # noqa: UP036 - doctor 需真实报告运行期版本
        checks.append(
            DoctorCheck("python", "ok", f"Python {sys.version_info.major}.{sys.version_info.minor}")
        )
    else:
        checks.append(
            DoctorCheck(
                "python",
                "fail",
                f"Python {sys.version_info.major}.{sys.version_info.minor} 低于 3.12",
            )
        )

    missing: list[str] = []
    for module in ("typer", "rich", "pydantic", "aiosqlite", "mcp"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        checks.append(DoctorCheck("dependencies", "fail", f"缺失依赖：{', '.join(missing)}"))
    else:
        checks.append(DoctorCheck("dependencies", "ok", "typer/rich/pydantic/aiosqlite/mcp"))

    try:
        with tempfile.TemporaryDirectory(dir=workspace) as tmpdir:
            db_path = Path(tmpdir) / "doctor.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("CREATE TABLE t (v TEXT)")
                connection.execute("INSERT INTO t VALUES ('ok')")
                connection.commit()
                row = connection.execute("SELECT v FROM t").fetchone()
                if row is None or row[0] != "ok":
                    raise sqlite3.Error("readback mismatch")
            finally:
                connection.close()
        checks.append(DoctorCheck("sqlite", "ok", "工作区可写 SQLite"))
    except (OSError, sqlite3.Error) as exc:
        checks.append(DoctorCheck("sqlite", "fail", f"SQLite 不可写：{Redactor.redact(str(exc))}"))

    if shutil.which("git") is None:
        checks.append(DoctorCheck("git", "warn", "未找到 git"))
    else:
        ok, detail = _run_command(["git", "--version"])
        checks.append(DoctorCheck("git", "ok" if ok else "warn", detail or "git 可用"))

    if shutil.which("rg") is None:
        checks.append(DoctorCheck("rg", "warn", "未找到 rg，不阻断"))
    else:
        checks.append(DoctorCheck("rg", "ok", "rg 可用"))

    if config is None:
        checks.append(DoctorCheck("model", "warn", "配置无效，无法检查模型凭据"))
    elif config.model is None:
        checks.append(DoctorCheck("model", "warn", "未配置模型"))
    elif os.environ.get(config.model.api_key_env) is not None:
        checks.append(DoctorCheck("model", "ok", f"{config.model.api_key_env} 已设置（不读值）"))
    else:
        checks.append(DoctorCheck("model", "warn", f"{config.model.api_key_env} 未设置"))

    docker_wanted = check_docker or (config is not None and config.doctor.check_docker)
    sandbox_enabled = config is not None and config.sandbox.enabled
    if not docker_wanted and not sandbox_enabled:
        checks.append(DoctorCheck("docker", "ok", "未启用检查（Docker 未启用）"))
    elif shutil.which("docker") is None:
        checks.append(DoctorCheck("docker", "fail" if sandbox_enabled else "warn", "未找到 docker"))
    else:
        ok, detail = _run_command(["docker", "info"])
        if ok:
            checks.append(DoctorCheck("docker", "ok", detail or "docker 可用"))
        else:
            checks.append(
                DoctorCheck(
                    "docker",
                    "fail" if sandbox_enabled else "warn",
                    detail or "docker info 失败",
                )
            )

    if config_error is not None:
        checks.append(DoctorCheck("config", "fail", config_error))
    else:
        annotated = "；".join(
            f"{section}: {source}"
            for section, source in sorted(sources.items())
            if source != "default"
        )
        checks.append(
            DoctorCheck("config", "ok", f"配置有效（{annotated}）" if annotated else "配置有效")
        )
    return DoctorReport(checks=checks, sources=sources)


__all__ = ["DoctorCheck", "DoctorReport", "Redactor", "run_doctor"]
