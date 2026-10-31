"""Run X01–X06 deterministic extension acceptance cases."""

import json

from evals.runners.runner import ROOT, run_case


def main() -> int:
    """Fail if any extension case fails or is missing."""
    cases = sorted((ROOT / "evals/cases").glob("X*.yaml"))
    if len(cases) != 6:
        raise RuntimeError("expected exactly six extension cases")
    reports = ROOT / "evals/reports"
    results = []
    for case in cases:
        spec = json.loads(case.read_text(encoding="utf-8"))
        results.append(run_case(spec["id"], spec["pytest"], reports))
    print(f"{sum(item['passed'] for item in results)}/{len(results)} extension cases passed")
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
