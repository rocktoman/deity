"""Run M01-M12 deterministic read-only delegation checks."""

import json
import platform
import subprocess
import sys
import time

from evals.runners.runner import ROOT


def main() -> int:
    """Execute each fixed case in isolation and persist machine-readable facts."""
    cases = sorted((ROOT / "evals/cases").glob("M*.yaml"))
    if [case.stem for case in cases] != [f"M{number:02d}" for number in range(1, 13)]:
        raise RuntimeError("expected exactly M01-M12")
    reports = ROOT / "evals/reports"
    reports.mkdir(parents=True, exist_ok=True)
    failed = False
    for case in cases:
        spec = json.loads(case.read_text(encoding="utf-8"))
        selectors = spec["pytest"] if isinstance(spec["pytest"], list) else [spec["pytest"]]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *selectors],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            status = "PASS" if completed.returncode == 0 else "FAIL"
            output = (completed.stdout + completed.stderr)[-4000:]
            exit_code: int | None = completed.returncode
        except subprocess.TimeoutExpired:
            status, output, exit_code = "FAIL", "case timeout", None
        report = {
            "case_id": spec["id"],
            "name": spec["name"],
            "status": status,
            "pytest": spec["pytest"],
            "duration_ms": round((time.monotonic() - started) * 1000),
            "platform": platform.platform(),
            "exit_code": exit_code,
            "output_tail": output,
        }
        (reports / f"{spec['id']}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"{spec['id']}: {status}")
        failed |= status != "PASS"
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
