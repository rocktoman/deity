"""Diag-only conftest (never merge to main): POSTs failure summary to ntfy."""

import urllib.request

TOPIC = "https://ntfy.sh/deity-diag-9f3k7x2"
_failures: list[str] = []


def pytest_runtest_logreport(report):
    if report.failed:
        text = ""
        if report.longrepr:
            text = str(report.longrepr)[-600:]
        _failures.append(f"{report.nodeid} :: {text}")


def pytest_sessionfinish(session, exitstatus):
    body = f"exit={exitstatus} collected={session.testscollected} failed={len(_failures)}\n"
    body += "\n---\n".join(_failures[:40])
    for i in range(0, len(body), 3500):
        chunk = body[i : i + 3500]
        try:
            req = urllib.request.Request(
                TOPIC, data=chunk.encode("utf-8", "replace"), method="POST"
            )
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:  # noqa: BLE001, S110
            pass
