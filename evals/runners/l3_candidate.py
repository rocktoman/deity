"""Explicit, isolated DeepSeek V0.3 candidate gate. Never runs in PR CI."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import Config, ModelConfig, RunConfig
from deity.application.run_service import RunService
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError

ROOT = Path(__file__).parents[2]
REPORTS = ROOT / "evals/reports"
MAX_CALLS = 80
MAX_INPUT = 300_000
MAX_OUTPUT = 30_000
MAX_SECONDS = 45 * 60


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=10, check=False
    )


def _fixture(root: Path, case: str) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "deity-eval@example.invalid")
    _git(root, "config", "user.name", "Deity Eval")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n")
    if case == "E01":
        (root / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    elif case == "E02":
        (root / "README.md").write_text("Small Python package. Add multiply.py.\n")
    elif case == "E03":
        (root / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a - b\n")
        (root / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
        )
    else:
        (root / "calc.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a - b\n\n"
            "def multiply(a: int, b: int) -> int:\n    return a + b\n\n"
            "def safe_divide(a: int, b: int) -> float | None:\n    return a / b\n"
        )
        (root / "test_calc.py").write_text(
            "from calc import add, multiply, safe_divide\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n\n"
            "def test_multiply():\n    assert multiply(3, 4) == 12\n\n"
            "def test_divide():\n    assert safe_divide(8, 2) == 4\n"
            "    assert safe_divide(8, 0) is None\n"
        )
    assert _git(root, "add", ".").returncode == 0
    assert _git(root, "commit", "-qm", "fixture").returncode == 0


def _approved(case: str, name: str, args: dict[str, Any]) -> bool:
    if case == "E01":
        return False
    if name in {"write_file", "apply_patch"}:
        allowed = "multiply.py" if case == "E02" else "calc.py"
        return args.get("path") == allowed
    if name == "shell" and case in {"E03", "LONG"}:
        argv = args.get("argv", [])
        return isinstance(argv, list) and bool(argv) and "pytest" in " ".join(argv)
    return False


def _limits(case: str) -> RunConfig:
    turns, calls, seconds = {
        "E01": (3, 2, 120),
        "E02": (8, 8, 150),
        "E03": (8, 8, 180),
        "LONG": (20, 20, 600),
    }[case]
    return RunConfig(
        max_turns=turns,
        max_tool_calls=calls,
        deadline_seconds=seconds,
        max_output_tokens=512,
        compaction_enabled=case == "LONG",
        resume_enabled=True,
        max_input_tokens_total={"E01": 30_000, "E02": 60_000, "E03": 90_000, "LONG": 120_000}[case],
        max_output_tokens_total={"E01": 3_000, "E02": 6_000, "E03": 9_000, "LONG": 12_000}[case],
    )


def _prompt(case: str) -> str:
    return {
        "E01": "读取 calc.py，简要说明 add 函数，并在回答中引用 calc.py。不要修改文件。",
        "E02": "创建 multiply.py，定义 multiply(a, b)，返回两数乘积。只修改该文件。",
        "E03": "test_calc.py 当前失败。修复 calc.py 中的错误，运行 pytest -q 确认通过。",
        "LONG": "calc.py 中三个函数有错误。逐项修复 add、multiply、safe_divide（除数为零返回 None），运行 pytest -q，直到全部通过；只修改 calc.py。",
    }[case]


def _assert_case(root: Path, case: str, final_text: str | None) -> tuple[bool, str]:
    changed = [line for line in _git(root, "status", "--porcelain").stdout.splitlines() if line]
    names = {line[3:] for line in changed}
    if case == "E01":
        return not changed and "calc.py" in (final_text or ""), "read_only_and_citation"
    if case == "E02":
        if names != {"multiply.py"}:
            return False, f"unexpected_diff:{sorted(names)}"
        check = subprocess.run(
            [sys.executable, "-c", "from multiply import multiply; assert multiply(2,3)==6"],
            cwd=root,
            capture_output=True,
            timeout=10,
            check=False,
        )
        return check.returncode == 0, "multiply_assertion"
    if names != {"calc.py"}:
        return False, f"unexpected_diff:{sorted(names)}"
    check = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=root,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return check.returncode == 0, "pytest_exit_0"


async def _run_case(case: str, key: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"deity-l3-{case.lower()}-") as temporary:
        base = Path(temporary)
        workspace = base / "workspace"
        workspace.mkdir()
        _fixture(workspace, case)
        db = base / "deity.db"
        config = Config(
            run=_limits(case),
            model=ModelConfig(
                provider="openai_compatible",
                name="deepseek-v4-flash",
                api_key_env="DEEPSEEK_API_KEY",
                base_url="https://api.deepseek.com",
                context_window_tokens=8192,
            ),
        )
        os.environ["DEEPSEEK_API_KEY"] = key
        service = RunService.from_config(
            config,
            db_path=db,
            workspace=workspace,
            approve=lambda name, args: _approved(case, name, args),
        )
        started = time.monotonic()
        final_text = None
        error = None
        try:
            result = await service.run(_prompt(case), model_name="deepseek-v4-flash")
            run_id = result.run_id
            final_text = result.final_text
            completed = result.status.value == "completed"
        except (BudgetExceeded, ModelFailure, ProtocolError, ValueError, OSError) as exc:
            error = type(exc).__name__
            completed = False
            run_id = None
        repo = await SQLiteSessionRepository.open(db)
        try:
            if run_id is None:
                async with repo.db.execute(
                    "SELECT id FROM runs ORDER BY rowid DESC LIMIT 1"
                ) as cursor:
                    row = await cursor.fetchone()
                run_id = row[0] if row else None
            events = await repo.events(run_id) if run_id else []
        finally:
            await repo.close()
        usage = [event["payload"] for event in events if event["type"] == "ModelUsage"]
        input_tokens = sum(
            item["input_tokens"] for item in usage if item["input_tokens"] is not None
        )
        output_tokens = sum(
            item["output_tokens"] for item in usage if item["output_tokens"] is not None
        )
        reported = bool(usage) and all(
            item["input_tokens"] is not None and item["output_tokens"] is not None for item in usage
        )
        feature_ok, assertion = _assert_case(workspace, case, final_text)
        return {
            "case_id": case,
            "passed": completed and feature_ok and reported,
            "run_id": run_id,
            "error_type": error,
            "assertion": assertion,
            "feature_passed": feature_ok,
            "model_calls": sum(event["type"] == "ModelTurnCompleted" for event in events),
            "tool_calls": sum(event["type"] == "ToolClaimed" for event in events),
            "compactions": sum(event["type"] == "ContextCompacted" for event in events),
            "tool_results": [
                event["payload"].get("status")
                for event in events
                if event["type"] == "ToolCompleted"
            ],
            "input_tokens": input_tokens if reported else None,
            "output_tokens": output_tokens if reported else None,
            "cost": "UNKNOWN",
            "duration_seconds": round(time.monotonic() - started, 2),
        }


async def main() -> int:
    """Run four fixed cases, stopping at the configured operational caps."""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        print("DEEPSEEK_API_KEY is required", file=sys.stderr)
        return 2
    REPORTS.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    started = time.monotonic()
    for case in ("E01", "E02", "E03", "LONG"):
        if time.monotonic() - started >= MAX_SECONDS:
            break
        if sum(item["model_calls"] for item in results) >= MAX_CALLS:
            break
        if sum(item["input_tokens"] or 0 for item in results) >= MAX_INPUT:
            break
        if sum(item["output_tokens"] or 0 for item in results) >= MAX_OUTPUT:
            break
        result = await _run_case(case, key)
        results.append(result)
        print(f"{case}: {'PASS' if result['passed'] else 'FAIL'}")
        if result["input_tokens"] is None or result["output_tokens"] is None:
            break
    totals = {
        "model_calls": sum(item["model_calls"] for item in results),
        "input_tokens": sum(item["input_tokens"] or 0 for item in results),
        "output_tokens": sum(item["output_tokens"] or 0 for item in results),
        "duration_seconds": round(time.monotonic() - started, 2),
    }
    passed = (
        len(results) == 4
        and all(item["passed"] for item in results)
        and (
            totals["model_calls"] <= MAX_CALLS
            and totals["input_tokens"] <= MAX_INPUT
            and totals["output_tokens"] <= MAX_OUTPUT
            and totals["duration_seconds"] <= MAX_SECONDS
        )
    )
    report = {"passed": passed, "cases": results, "totals": totals, "cost": "UNKNOWN"}
    (REPORTS / "L3-v0.3-candidate.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    (REPORTS / "L3-v0.3-candidate.md").write_text(
        "# V0.3 DeepSeek L3 候选版\n\n"
        + "\n".join(
            f"- {item['case_id']}: {'PASS' if item['passed'] else 'FAIL'}" for item in results
        )
        + f"\n\n总结果：{'PASS' if passed else 'FAIL'}；费用：UNKNOWN。\n"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
