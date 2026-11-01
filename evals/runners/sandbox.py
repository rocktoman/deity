"""Run S01-S12 with explicit Docker requirements and honest NOT_RUN results."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time

from evals.runners.runner import ROOT


def docker_ready(image: str) -> bool:
    """Require a responsive daemon and a locally present fixture image."""
    if shutil.which("docker") is None:
        return False
    for command in (
        ["docker", "info", "--format", "{{.OSType}}"],
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
    ):
        try:
            result = subprocess.run(command, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
    return True


def docker_fact(*args: str) -> str:
    """Return one small Docker fact or UNKNOWN without leaking daemon configuration."""
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"


def main() -> int:
    """Run every case; a required Docker case cannot silently skip."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-docker", action="store_true")
    parser.add_argument("--backend", choices=("all", "fake", "docker"), default="all")
    args = parser.parse_args()
    cases = sorted((ROOT / "evals/cases").glob("S*.yaml"))
    if [case.stem for case in cases] != [f"S{number:02d}" for number in range(1, 13)]:
        raise RuntimeError("expected exactly S01-S12")
    reports = ROOT / "evals/reports"
    reports.mkdir(parents=True, exist_ok=True)
    image = os.environ.get("DEITY_SANDBOX_TEST_IMAGE", "alpine:3.22")
    ready = docker_ready(image)
    server_version = (
        docker_fact("version", "--format", "{{.Server.Version}}") if ready else "NOT_RUN"
    )
    image_id = docker_fact("image", "inspect", image, "--format", "{{.Id}}") if ready else "NOT_RUN"
    results = []
    for case in cases:
        spec = json.loads(case.read_text(encoding="utf-8"))
        started = time.monotonic()
        status = "NOT_RUN"
        output = "Docker daemon or local fixture image unavailable"
        exit_code = None
        execution_facts: list[dict[str, object]] = []
        selected = args.backend == "all" or spec["backend"] == args.backend
        if not selected:
            output = "backend filtered"
        elif spec["backend"] != "docker" or ready:
            selectors = spec["pytest"]
            if isinstance(selectors, str):
                selectors = [selectors]
            env = {**os.environ, "DEITY_REQUIRE_DOCKER": "1" if ready else "0"}
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-s", "-r", "s", *selectors],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            output = (completed.stdout + completed.stderr)[-4000:]
            for line in completed.stdout.splitlines():
                marker = "DEITY_EVAL_RECORD:"
                if marker in line:
                    try:
                        execution_facts.append(json.loads(line.split(marker, 1)[1]))
                    except json.JSONDecodeError:
                        pass
            exit_code = completed.returncode
            status = "PASS" if exit_code == 0 and " skipped" not in output else "FAIL"
        report = {
            "case_id": spec["id"],
            "status": status,
            "backend": spec["backend"],
            "selected": selected,
            "platform": platform.platform(),
            "docker_image": image if ready and spec["backend"] == "docker" else "NOT_RUN",
            "docker_server_version": (
                server_version if ready and spec["backend"] == "docker" else "NOT_RUN"
            ),
            "image_id": image_id if ready and spec["backend"] == "docker" else "NOT_RUN",
            "executions": execution_facts,
            "execution_id": [item["execution_id"] for item in execution_facts]
            if execution_facts
            else "NOT_APPLICABLE"
            if spec["backend"] == "fake"
            else "UNKNOWN",
            "container_exit_code": [item["container_exit_code"] for item in execution_facts]
            if execution_facts
            else "NOT_APPLICABLE"
            if spec["backend"] == "fake"
            else "UNKNOWN",
            "cleanup_status": [item["cleanup_status"] for item in execution_facts]
            if execution_facts
            else "NOT_APPLICABLE"
            if spec["backend"] == "fake"
            else "UNKNOWN",
            "artifact_status": [item["artifact_status"] for item in execution_facts]
            if execution_facts
            else "NOT_APPLICABLE"
            if spec["backend"] == "fake"
            else "UNKNOWN",
            "duration_ms": round((time.monotonic() - started) * 1000),
            "exit_code": exit_code,
            "output_tail": output,
        }
        (reports / f"{spec['id']}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        results.append(report)
    print(
        f"{sum(item['status'] == 'PASS' for item in results)}/"
        f"{sum(item['selected'] for item in results)} selected sandbox cases passed"
    )
    return (
        0
        if all(
            item["status"] == "PASS"
            or (item["status"] == "NOT_RUN" and (not item["selected"] or not args.require_docker))
            for item in results
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
