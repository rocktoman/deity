"""回归门禁：关键安全/协议门禁任一失败即 gate 失败。"""

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = ["GateResult", "RegressionGate"]

# 关键门禁白名单：客户端协议（C01/C02）、权限边界（E05/B05）、恢复完整性（R06）。
DEFAULT_REQUIRED: tuple[str, ...] = ("C01", "C02", "E05", "B05", "R06")


@dataclass(frozen=True)
class GateResult:
    """门禁结果：是否通过、已查 case、失败 case。"""

    passed: bool
    checked: tuple[str, ...]
    failures: tuple[str, ...]


class RegressionGate:
    """读 evals/reports 下的 case 报告，任一关键门禁失败或缺失即失败。"""

    @staticmethod
    def check(reports_dir: Path, required: tuple[str, ...] = DEFAULT_REQUIRED) -> GateResult:
        """检查必需报告；结果写回 regression-gate.json。"""
        failures: list[str] = []
        for case_id in required:
            path = reports_dir / f"{case_id}.json"
            if not path.is_file():
                failures.append(f"{case_id}:missing")
                continue
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                failures.append(f"{case_id}:unreadable")
                continue
            passed = bool(report.get("passed", report.get("verification") == "PASS"))
            if not passed:
                failures.append(case_id)
        result = GateResult(passed=not failures, checked=tuple(required), failures=tuple(failures))
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "regression-gate.json").write_text(
            json.dumps(
                {
                    "passed": result.passed,
                    "checked": list(result.checked),
                    "failures": list(result.failures),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return result
