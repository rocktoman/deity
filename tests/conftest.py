"""Diag-only conftest (never merge to main): emits failures as GH annotations."""

_failures: list[str] = []


def pytest_runtest_logreport(report):
    if report.failed:
        text = ""
        if report.longrepr:
            text = " ".join(str(report.longrepr).split())[-300:]
        _failures.append(f"{report.nodeid} :: {text}")


def pytest_sessionfinish(session, exitstatus):
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    lines = [
        f"::error title=DIAGFAIL::{item.replace('%', '%25').replace(chr(10), ' ')}"
        for item in _failures[:45]
    ]
    lines.append(
        f"::error title=DIAGSUMMARY::exit={exitstatus} "
        f"collected={session.testscollected} failed={len(_failures)}"
    )
    for line in lines:
        if tr is not None:
            tr.write_line(line)
