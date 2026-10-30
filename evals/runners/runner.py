"""Run fixed deterministic cases and write machine-readable reports."""

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
CASES = ROOT / "evals/cases"


def run_case(
    case_id: str,
    selector: str | list[str],
    reports: Path,
    pytest_args: list[str] | None = None,
) -> dict[str, Any]:
    """Execute one isolated pytest selector and persist its outcome."""
    reports.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    selectors = [selector] if isinstance(selector, str) else selector
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *(pytest_args or []), *selectors],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    passed = completed.returncode == 0
    report = {
        "case_id": case_id,
        "passed": passed,
        "verification": "PASS" if passed else "FAIL",
        "assertions": [
            {"name": item, "status": "pass" if passed else "fail"} for item in selectors
        ],
        "duration_ms": round((time.monotonic() - started) * 1000),
        "exit_code": completed.returncode,
        "model_calls": None,
        "tool_calls": None,
        "token_usage": None,
        "output_tail": (completed.stdout + completed.stderr)[-4000:],
    }
    (reports / f"{case_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (reports / f"{case_id}.md").write_text(
        f"# {case_id}\n\n验证：{report['verification']}\n\n用例：`{'`, `'.join(selectors)}`\n\n退出码：{completed.returncode}\n"
    )
    return report


def main() -> int:
    """Run E01–E08 and return a failing exit code if any assertion fails."""
    reports = ROOT / "evals/reports"
    results = []
    for case in sorted(CASES.glob("E*.yaml")):
        spec = json.loads(case.read_text())
        results.append(run_case(spec["id"], spec["pytest"], reports))
    if len(results) != 8:
        raise RuntimeError("expected exactly eight baseline cases")
    print(f"{sum(item['passed'] for item in results)}/{len(results)} evaluation cases passed")
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
