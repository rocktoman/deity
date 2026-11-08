"""Run C01–C05 in isolated pytest workers and emit JSON/Markdown reports."""

import json

from evals.runners.runner import ROOT, run_case


def main() -> int:
    """Fail if any deterministic client-protocol assertion fails."""
    reports = ROOT / "evals/reports"
    cases = sorted((ROOT / "evals/cases").glob("C*.yaml"))
    if len(cases) != 5:
        raise RuntimeError("expected exactly five client cases")
    results = []
    for case in cases:
        spec = json.loads(case.read_text())
        results.append(run_case(spec["id"], spec["pytest"], reports, spec.get("pytest_args")))
    print(f"{sum(item['passed'] for item in results)}/{len(results)} client cases passed")
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
