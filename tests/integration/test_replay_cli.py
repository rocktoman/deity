"""S4 回归门禁与 eval CLI：gate 在关键门禁失败时失败，replay 输出同一终态。"""

import json
from pathlib import Path

from typer.testing import CliRunner

from deity.application.regression_gate import RegressionGate

REPORTS = Path(__file__).parents[2] / "evals" / "reports"


def _write_report(reports: Path, case_id: str, passed: bool) -> None:
    (reports / f"{case_id}.json").write_text(
        json.dumps(
            {"case_id": case_id, "passed": passed, "verification": "PASS" if passed else "FAIL"}
        )
    )


def test_gate_passes_when_required_reports_pass(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    for case_id in ("C01", "C02", "E05", "B05", "R06"):
        _write_report(reports, case_id, True)
    result = RegressionGate.check(reports)
    assert result.passed is True
    assert result.failures == ()
    gate_file = reports / "regression-gate.json"
    assert gate_file.is_file()
    assert json.loads(gate_file.read_text())["passed"] is True


def test_gate_fails_when_a_required_report_fails(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    for case_id in ("C01", "C02", "E05", "B05", "R06"):
        _write_report(reports, case_id, True)
    _write_report(reports, "C01", False)
    result = RegressionGate.check(reports)
    assert result.passed is False
    assert "C01" in result.failures
    assert json.loads((reports / "regression-gate.json").read_text())["passed"] is False


def test_gate_fails_when_a_required_report_is_missing(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    _write_report(reports, "C01", True)
    result = RegressionGate.check(reports)
    assert result.passed is False
    assert any("C02" in item for item in result.failures)


def test_eval_replay_cli_reports_same_verdict(tmp_path: Path) -> None:
    from deity.interfaces.cli.main import app as cli_app

    report = tmp_path / "B01.json"
    report.write_text(json.dumps({"case_id": "B01", "passed": True, "verification": "PASS"}))
    result = CliRunner().invoke(cli_app, ["eval", "replay", str(report)])
    assert result.exit_code == 0, result.output
    assert "B01" in result.output
    assert "completed" in result.output


def test_eval_gate_cli_fails_on_broken_required_case(tmp_path: Path) -> None:
    from deity.interfaces.cli.main import app as cli_app

    reports = tmp_path / "reports"
    reports.mkdir()
    for case_id in ("C01", "C02", "E05", "B05", "R06"):
        _write_report(reports, case_id, True)
    _write_report(reports, "R06", False)
    result = CliRunner().invoke(cli_app, ["eval", "gate", "--reports-dir", str(reports)])
    assert result.exit_code == 1, result.output
    assert "R06" in result.output
