import asyncio
import os
import sys
from pathlib import Path

import pytest

from deity.adapters.verification import ProjectVerificationDetector, VerificationExecutor
from deity.core.contracts.verification import (
    VerificationCommand,
    VerificationPlan,
    VerificationStatus,
)
from deity.core.verification import aggregate_verification


@pytest.mark.asyncio
async def test_detector_uses_only_declared_project_commands(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths=['tests']\n[tool.ruff]\nline-length=100\n"
        "[tool.mypy]\nstrict=true\n",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("version = 1", encoding="utf-8")
    python = await ProjectVerificationDetector(tmp_path).detect()
    assert [item.argv for item in python.commands] == [
        ("uv", "run", "--locked", "pytest", "-q"),
        ("uv", "run", "--locked", "ruff", "check", "."),
        ("uv", "run", "--locked", "ruff", "format", "--check", "."),
        ("uv", "run", "--locked", "mypy", "src"),
    ]

    java = tmp_path / "java"
    java.mkdir()
    wrapper = java / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    (java / "build.gradle").write_text("", encoding="utf-8")
    detected_java = await ProjectVerificationDetector(java).detect()
    assert [item.argv for item in detected_java.commands] == [("./gradlew", "test")]

    typescript = tmp_path / "typescript"
    typescript.mkdir()
    (typescript / "package.json").write_text(
        '{"scripts":{"test":"vitest","lint":"eslint .","typecheck":"tsc --noEmit"}}',
        encoding="utf-8",
    )
    (typescript / "pnpm-lock.yaml").write_text("lockfileVersion: 9", encoding="utf-8")
    detected_ts = await ProjectVerificationDetector(typescript).detect()
    assert [item.argv for item in detected_ts.commands] == [
        ("pnpm", "run", "test"),
        ("pnpm", "run", "lint"),
        ("pnpm", "run", "typecheck"),
    ]

    custom = await ProjectVerificationDetector(
        tmp_path, custom_commands=(("python", "-m", "compileall", "src"),)
    ).detect()
    assert custom.commands[-1] == VerificationCommand(
        "custom", ("python", "-m", "compileall", "src")
    )


@pytest.mark.asyncio
async def test_executor_reports_pass_fail_not_run_and_timeout(tmp_path: Path) -> None:
    passed = VerificationPlan((VerificationCommand("test", (sys.executable, "-c", "print('ok')")),))
    executor = VerificationExecutor(
        tmp_path,
        permission="auto",
        timeout_seconds=1,
        allowed_commands=(sys.executable,),
    )
    pass_evidence = await executor.run(passed)
    assert pass_evidence[0].status == VerificationStatus.PASSED
    assert aggregate_verification(pass_evidence) == VerificationStatus.PASSED

    failed = VerificationPlan(
        (VerificationCommand("test", (sys.executable, "-c", "raise SystemExit(3)")),)
    )
    fail_evidence = await executor.run(failed)
    assert fail_evidence[0].exit_code == 3
    assert aggregate_verification(fail_evidence) == VerificationStatus.FAILED

    denied = await VerificationExecutor(tmp_path, permission="deny").run(passed)
    assert aggregate_verification(denied) == VerificationStatus.NOT_RUN

    timeout = VerificationPlan(
        (VerificationCommand("test", (sys.executable, "-c", "import time; time.sleep(2)")),)
    )
    timed = await executor.run(timeout)
    assert timed[0].status == VerificationStatus.INCONCLUSIVE
    assert timed[0].reason == "timeout"


@pytest.mark.asyncio
async def test_missing_dependency_and_flaky_repeat_are_inconclusive(tmp_path: Path) -> None:
    missing = VerificationPlan((VerificationCommand("test", ("missing-deity-command",)),))
    missing_result = await VerificationExecutor(
        tmp_path, permission="auto", allowed_commands=("missing-deity-command",)
    ).run(missing)
    assert missing_result[0].status == VerificationStatus.INCONCLUSIVE
    assert missing_result[0].reason == "dependency_missing"

    script = (
        "from pathlib import Path; p=Path('marker'); "
        "exists=p.exists(); p.write_text('x'); raise SystemExit(1 if exists else 0)"
    )
    plan = VerificationPlan((VerificationCommand("test", (sys.executable, "-c", script)),))
    repeated = await VerificationExecutor(
        tmp_path,
        permission="auto",
        repeat=2,
        allowed_commands=(sys.executable,),
    ).run(plan)
    assert repeated[0].status == VerificationStatus.INCONCLUSIVE
    assert repeated[0].reason == "flaky"


def test_no_checks_cannot_be_passed() -> None:
    assert aggregate_verification(()) == VerificationStatus.NOT_RUN


@pytest.mark.asyncio
async def test_verification_cancellation_cleans_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = (
        "import os,time; from pathlib import Path; "
        "Path('pid').write_text(str(os.getpid())); time.sleep(30)"
    )
    plan = VerificationPlan((VerificationCommand("test", (sys.executable, "-c", script)),))
    executor = VerificationExecutor(
        tmp_path,
        permission="auto",
        timeout_seconds=60,
        allowed_commands=(sys.executable,),
    )
    task = asyncio.create_task(executor.run(plan))
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
