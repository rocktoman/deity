from pathlib import Path

import pytest

from deity.adapters.execution.local import LocalExecutionBackend
from deity.core.contracts.execution import (
    BackendKind,
    ExecutionRequest,
    ExecutionStatus,
    ResourceBudget,
    SandboxSpec,
)


@pytest.mark.asyncio
async def test_local_backend_declares_no_isolation_and_bounds_cwd(tmp_path: Path) -> None:
    backend = LocalExecutionBackend(tmp_path)
    capabilities = await backend.capabilities()
    assert capabilities.available
    assert not capabilities.process_isolation
    request = ExecutionRequest(
        "local-execution",
        "local-run",
        "local-call",
        ("python", "-c", "print('ok')"),
        ".",
        SandboxSpec(
            BackendKind.LOCAL,
            None,
            ResourceBudget(5, None, None, None, 100, None),
        ),
    )
    handle = await backend.prepare(request)
    result = await backend.execute(handle, request)
    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.output.strip() == "ok"
    assert not (await backend.collect(handle, result)).entries
    assert (await backend.cleanup(handle)).status == "cleaned"

    outside = ExecutionRequest(
        request.execution_id,
        request.run_id,
        request.call_id,
        request.argv,
        "..",
        request.spec,
    )
    with pytest.raises(ValueError, match="outside"):
        await backend.execute(handle, outside)
