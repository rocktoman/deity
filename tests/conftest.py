"""Diag-only conftest (never merge to main): emits failures as GH annotations."""

_failures: list[str] = []


def _esc(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def pytest_runtest_logreport(report):
    if report.failed:
        text = ""
        if report.longrepr:
            text = str(report.longrepr)[-1500:]
        _failures.append(f"{report.nodeid} :: {text}")


def pytest_sessionfinish(session, exitstatus):
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    lines = [
        "::error title=DIAGSUMMARY::"
        + _esc(f"exit={exitstatus} collected={session.testscollected} failed={len(_failures)}")
    ]
    lines += [f"::error title=DIAGFAIL::{_esc(item)}" for item in _failures[:45]]
    for line in lines:
        if tr is not None:
            tr.write_line(line)
