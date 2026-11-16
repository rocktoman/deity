"""诊断专用 conftest（仅 diag 分支）：把失败用例以 ::error 注解输出。"""

_failures: list[str] = []


def pytest_runtest_logreport(report: object) -> None:
    if getattr(report, "failed", False) and getattr(report, "when", "") in {
        "call",
        "setup",
        "teardown",
    }:
        text = str(getattr(report, "longrepr", ""))[-400:].replace("\n", " | ")
        _failures.append(f"{getattr(report, 'nodeid', '?')} :: {text}")


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    config = getattr(session, "config", None)
    manager = getattr(config, "pluginmanager", None)
    reporter = manager.get_plugin("terminalreporter") if manager else None
    if reporter is None:
        return
    for line in _failures[:40]:
        reporter.write_line(f"::error title=DIAGFAIL::{line}")
    reporter.write_line(f"::error title=DIAGSUMMARY::failures={len(_failures)}")
