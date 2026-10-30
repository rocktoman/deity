"""Run R01–R06 in isolated pytest workers and emit JSON/Markdown reports."""

import json

from evals.runners.runner import ROOT, run_case


def main() -> int:
    """Fail if any deterministic reliability assertion fails."""
    reports = ROOT / "evals/reports"
    cases = sorted((ROOT / "evals/cases").glob("R*.yaml"))
    if len(cases) != 6:
        raise RuntimeError("expected exactly six reliability cases")
    results = []
    for case in cases:
        spec = json.loads(case.read_text())
        results.append(run_case(spec["id"], spec["pytest"], reports))
    print(f"{sum(item['passed'] for item in results)}/{len(results)} reliability cases passed")
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
