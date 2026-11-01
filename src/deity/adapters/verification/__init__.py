"""Project command detection and bounded deterministic verification execution."""

import asyncio
import json
import os
import signal
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from deity.core.contracts.verification import (
    VerificationCommand,
    VerificationEvidence,
    VerificationPlan,
    VerificationStatus,
)

DEFAULT_ALLOWED_COMMANDS = (
    "uv",
    "./mvnw",
    "./gradlew",
    "npm",
    "pnpm",
    "yarn",
)


class ProjectVerificationDetector:
    """Read project metadata and emit fixed argv only; never install dependencies."""

    def __init__(
        self, workspace: Path, *, custom_commands: tuple[tuple[str, ...], ...] = ()
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.custom_commands = custom_commands

    async def detect(self) -> VerificationPlan:
        commands: list[VerificationCommand] = []
        diagnostics: list[str] = []
        pyproject = self.workspace / "pyproject.toml"
        if pyproject.is_file():
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError):
                diagnostics.append("invalid pyproject.toml")
            else:
                tool = data.get("tool", {}) if isinstance(data, dict) else {}
                locked = (self.workspace / "uv.lock").is_file()
                prefix = ("uv", "run", "--locked") if locked else ()
                if isinstance(tool, dict) and "pytest" in tool and prefix:
                    commands.append(VerificationCommand("test", (*prefix, "pytest", "-q")))
                if isinstance(tool, dict) and "ruff" in tool and prefix:
                    commands.extend(
                        (
                            VerificationCommand("lint", (*prefix, "ruff", "check", ".")),
                            VerificationCommand(
                                "format", (*prefix, "ruff", "format", "--check", ".")
                            ),
                        )
                    )
                if isinstance(tool, dict) and "mypy" in tool and prefix:
                    commands.append(VerificationCommand("typecheck", (*prefix, "mypy", "src")))
                if not prefix:
                    diagnostics.append("Python lockfile not found; commands not inferred")
        if (self.workspace / "pom.xml").is_file() or (self.workspace / "build.gradle").is_file():
            mvnw = self.workspace / "mvnw"
            gradlew = self.workspace / "gradlew"
            if mvnw.is_file() and os.access(mvnw, os.X_OK):
                commands.append(VerificationCommand("test", ("./mvnw", "test")))
            elif gradlew.is_file() and os.access(gradlew, os.X_OK):
                commands.append(VerificationCommand("test", ("./gradlew", "test")))
            else:
                diagnostics.append("Java wrapper not found")
        package_json = self.workspace / "package.json"
        if package_json.is_file():
            try:
                package = json.loads(package_json.read_text(encoding="utf-8"))
                scripts = package.get("scripts", {})
                if not isinstance(scripts, dict):
                    raise TypeError
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                diagnostics.append("invalid package.json")
            else:
                manager = self._package_manager()
                if manager is None:
                    diagnostics.append("JavaScript lockfile not found; scripts not inferred")
                else:
                    for name, kind in (
                        ("test", "test"),
                        ("lint", "lint"),
                        ("typecheck", "typecheck"),
                        ("build", "build"),
                    ):
                        if isinstance(scripts.get(name), str):
                            commands.append(
                                VerificationCommand(kind, (manager, "run", name))  # type: ignore[arg-type]
                            )
        commands.extend(VerificationCommand("custom", command) for command in self.custom_commands)
        return VerificationPlan(tuple(commands), tuple(diagnostics))

    def _package_manager(self) -> str | None:
        if (self.workspace / "pnpm-lock.yaml").is_file():
            return "pnpm"
        if (self.workspace / "yarn.lock").is_file():
            return "yarn"
        if (self.workspace / "package-lock.json").is_file():
            return "npm"
        return None


class VerificationExecutor:
    """Run approved fixed argv with timeout, output cap and process-group cleanup."""

    def __init__(
        self,
        workspace: Path,
        *,
        permission: Literal["ask", "auto", "deny"] = "ask",
        approve: Callable[[tuple[str, ...]], bool] | None = None,
        timeout_seconds: float = 300,
        max_output_chars: int = 16_000,
        repeat: int = 1,
        allowed_commands: tuple[str, ...] = DEFAULT_ALLOWED_COMMANDS,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.permission = permission
        self.approve = approve
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.repeat = repeat
        self.allowed_commands = set(allowed_commands)

    async def run(self, plan: VerificationPlan) -> tuple[VerificationEvidence, ...]:
        results: list[VerificationEvidence] = []
        for command in plan.commands:
            invalid = self._invalid_reason(command.argv)
            if invalid is not None:
                results.append(self._not_run(command, invalid))
                continue
            if self.permission == "deny":
                results.append(self._not_run(command, "permission_denied"))
                continue
            if self.permission == "ask" and (
                self.approve is None or not await asyncio.to_thread(self.approve, command.argv)
            ):
                results.append(self._not_run(command, "approval_required"))
                continue
            attempts = [await self._execute_once(command) for _ in range(self.repeat)]
            if (
                len({item.status for item in attempts}) > 1
                or len({item.exit_code for item in attempts}) > 1
            ):
                results.append(
                    VerificationEvidence(
                        str(uuid4()),
                        command,
                        VerificationStatus.INCONCLUSIVE,
                        None,
                        sum(item.duration_ms for item in attempts),
                        "\n".join(item.output for item in attempts)[: self.max_output_chars],
                        any(item.truncated for item in attempts),
                        "flaky",
                    )
                )
            else:
                results.append(attempts[-1])
        return tuple(results)

    def _invalid_reason(self, argv: tuple[str, ...]) -> str | None:
        if not argv or argv[0] not in self.allowed_commands:
            return "command_not_allowed"
        if any(not item or "\0" in item or "\n" in item or "\r" in item for item in argv):
            return "invalid_argv"
        if argv[0] in {"sh", "bash", "zsh", "cmd", "powershell"}:
            return "shell_wrapper_denied"
        return None

    async def _execute_once(self, command: VerificationCommand) -> VerificationEvidence:
        started = time.monotonic()
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
        try:
            process = await asyncio.create_subprocess_exec(
                *command.argv,
                cwd=self.workspace,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            return VerificationEvidence(
                str(uuid4()),
                command,
                VerificationStatus.INCONCLUSIVE,
                None,
                int((time.monotonic() - started) * 1000),
                "",
                reason="dependency_missing",
            )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return VerificationEvidence(
                str(uuid4()),
                command,
                VerificationStatus.INCONCLUSIVE,
                None,
                int((time.monotonic() - started) * 1000),
                "",
                reason="timeout",
            )
        output = (stdout + stderr).decode("utf-8", errors="replace")
        return VerificationEvidence(
            str(uuid4()),
            command,
            VerificationStatus.PASSED if process.returncode == 0 else VerificationStatus.FAILED,
            process.returncode,
            int((time.monotonic() - started) * 1000),
            output[: self.max_output_chars],
            len(output) > self.max_output_chars,
        )

    @staticmethod
    def _not_run(command: VerificationCommand, reason: str) -> VerificationEvidence:
        return VerificationEvidence(
            str(uuid4()),
            command,
            VerificationStatus.NOT_RUN,
            None,
            0,
            "",
            reason=reason,
        )
