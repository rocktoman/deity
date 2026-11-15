"""诊断专用 conftest（仅 diag 分支）：把失败用例以 ::error 注解输出。

注解消息必须净化：nodeid 里的 :: 与 longrepr 里的 % 都会破坏
GitHub workflow command 解析（v2 因此只剩 DIAGSUMMARY），故 nodeid
与错误正文分行输出且统一转义。
"""

_failures: list[tuple[str, str]] = []


def _clean(text: str) -> str:
    return text.replace("%", "%25").replace("\r", " ").replace("\n", " | ").replace("::", "//")


def pytest_runtest_logreport(report: object) -> None:
    if getattr(report, "failed", False) and getattr(report, "when", "") in {
        "call",
        "setup",
        "teardown",
    }:
        nodeid = _clean(str(getattr(report, "nodeid", "?")))
        text = _clean(str(getattr(report, "longrepr", ""))[-300:])
        _failures.append((nodeid, text))


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    config = getattr(session, "config", None)
    manager = getattr(config, "pluginmanager", None)
    reporter = manager.get_plugin("terminalreporter") if manager else None
    if reporter is None:
        return
    for nodeid, text in _failures[:40]:
        reporter.write_line(f"::error title=DIAGNODE::{nodeid}")
        reporter.write_line(f"::error title=DIAGFAIL::{text}")
    reporter.write_line(f"::error title=DIAGSUMMARY::failures={len(_failures)}")
