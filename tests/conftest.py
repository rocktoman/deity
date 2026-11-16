"""诊断专用 conftest v4（仅 diag 分支）：失败用例以转义后的 ::error 注解输出。"""

_failures: list[str] = []


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def pytest_runtest_logreport(report: object) -> None:
    if getattr(report, "failed", False) and getattr(report, "when", "") in {
        "call",
        "setup",
        "teardown",
    }:
        nodeid = str(getattr(report, "nodeid", "?"))
        text = str(getattr(report, "longrepr", "")).replace("\n", " | ")[-250:]
        _failures.append(f"{nodeid} :: {text}")


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    config = getattr(session, "config", None)
    manager = getattr(config, "pluginmanager", None)
    reporter = manager.get_plugin("terminalreporter") if manager else None
    if reporter is None:
        return
    reporter.write_line("")
    for line in _failures[:40]:
        reporter.write_line(f"::error title=DIAGFAIL::{_escape(line)}")
    reporter.write_line(f"::error title=DIAGSUMMARY::failures={len(_failures)}")
