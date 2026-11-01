"""Run A01-A12 deterministic V0.5 acceptance cases."""

import json

from evals.runners.runner import ROOT, run_case


def main() -> int:
    """Fail if any advanced case is missing or its machine assertion fails."""
    cases = sorted((ROOT / "evals/cases").glob("A*.yaml"))
    if len(cases) != 12:
        raise RuntimeError("expected exactly twelve advanced cases")
    reports = ROOT / "evals/reports"
    results = []
    for case in cases:
        spec = json.loads(case.read_text(encoding="utf-8"))
        report = run_case(spec["id"], spec["pytest"], reports)
        report.update(
            {
                "run_id": "NOT_RUN",
                "step_id": "NOT_RUN",
                "event_seq": "NOT_RUN",
                "harness_version": "0.1.0+v0.5",
                "model": "deterministic-pytest",
                "config_hash": "UNKNOWN",
                "failure_category": "NONE" if report["passed"] else "ASSERTION_FAILED",
                "budget": "NOT_RUN",
                "verification_evidence": report["assertions"],
                "token_usage": "UNKNOWN",
            }
        )
        (reports / f"{spec['id']}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        results.append(report)
    print(f"{sum(item['passed'] for item in results)}/{len(results)} advanced cases passed")
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
