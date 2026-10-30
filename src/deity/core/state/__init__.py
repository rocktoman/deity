"""Run state transitions."""

from deity.core.contracts import RunStatus

_ALLOWED = {
    RunStatus.CREATED: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_APPROVAL,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    },
    RunStatus.WAITING_APPROVAL: {
        RunStatus.RUNNING,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    },
}


def transition(current: RunStatus, target: RunStatus) -> RunStatus:
    """Return the next state or reject an illegal transition."""
    if target not in _ALLOWED.get(current, set()):
        raise ValueError(f"非法状态迁移：{current} → {target}")
    return target
