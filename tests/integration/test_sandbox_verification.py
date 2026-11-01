import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from deity.adapters.verification import VerificationExecutor
from deity.application.config import SandboxConfig
from deity.application.verification_service import VerificationService
from deity.core.contracts.verification import (
    VerificationCommand,
    VerificationPlan,
    VerificationStatus,
)


@pytest.mark.asyncio
async def test_verification_uses_docker_and_discards_generated_files(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("Docker CLI unavailable")
        pytest.skip("Docker unavailable")
    probe = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        os.environ.get("DEITY_SANDBOX_TEST_IMAGE", "alpine:3.22"),
        "--format",
        "{{.Id}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await probe.communicate()
    if probe.returncode != 0:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("local fixture image unavailable")
        pytest.skip("local fixture image unavailable")
    service = VerificationService(
        tmp_path / "deity.db",
        tmp_path,
        executor=VerificationExecutor(tmp_path, permission="auto", allowed_commands=("touch",)),
        sandbox=SandboxConfig(
            enabled=True, backend="docker", required=True, image=output.decode().strip()
        ),
    )
    report = await service.run_plan(
        VerificationPlan((VerificationCommand("custom", ("touch", "generated.txt")),))
    )
    assert report.status == VerificationStatus.PASSED
    assert report.checks[0].execution_id is not None
    assert report.checks[0].backend == "docker"
    assert report.checks[0].image_digest == output.decode().strip()
    assert report.checks[0].cleanup_status == "cleaned"
    assert not (tmp_path / "generated.txt").exists()
    evidence = report.checks[0]
    print(
        "DEITY_EVAL_RECORD:"
        + json.dumps(
            {
                "execution_id": evidence.execution_id,
                "execution_status": evidence.status.value,
                "container_exit_code": evidence.exit_code,
                "cleanup_status": evidence.cleanup_status,
                "artifact_status": "discarded",
                "artifact_count": 0,
            }
        )
    )
