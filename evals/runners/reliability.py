"""Run R01–R06 in isolated pytest workers and emit JSON/Markdown reports."""

import json
import math
import re

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
        result = run_case(spec["id"], spec["pytest"], reports, spec.get("pytest_args"))
        if spec["id"] == "R04":
            observations: dict[str, list[float]] = {}
            for kind, value in re.findall(
                r"R04_LATENCY (shell|approval|backoff|model) ([0-9.]+)",
                result["output_tail"],
            ):
                observations.setdefault(kind, []).append(float(value))
            if any(
                len(observations.get(kind, [])) != 5
                for kind in ("shell", "approval", "backoff", "model")
            ):
                result["passed"] = False
                result["verification"] = "FAIL"
            result["latency_ms"] = {
                kind: {
                    "p50": sorted(values)[2],
                    "p95": sorted(values)[math.ceil(len(values) * 0.95) - 1],
                    "max": max(values),
                    "samples": len(values),
                }
                for kind, values in observations.items()
            }
            (reports / "R04.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            (reports / "R04.md").write_text(
                "# R04\n\n验证："
                + result["verification"]
                + "\n\n取消时延（毫秒；每类 5 次）：\n\n"
                + "\n".join(
                    f"- {kind}: P50={stats['p50']}, P95={stats['p95']}, max={stats['max']}"
                    for kind, stats in result["latency_ms"].items()
                )
                + "\n"
            )
        results.append(result)
    print(f"{sum(item['passed'] for item in results)}/{len(results)} reliability cases passed")
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
