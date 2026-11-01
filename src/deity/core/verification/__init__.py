"""Verification aggregation independent of process execution."""

from collections.abc import Iterable

from deity.core.contracts.verification import VerificationEvidence, VerificationStatus


def aggregate_verification(
    evidence: Iterable[VerificationEvidence],
) -> VerificationStatus:
    """Aggregate only committed check facts; an empty set is never PASSED."""
    items = tuple(evidence)
    if not items:
        return VerificationStatus.NOT_RUN
    required = tuple(item for item in items if item.command.required)
    considered = required or items
    statuses = {item.status for item in considered}
    if VerificationStatus.FAILED in statuses:
        return VerificationStatus.FAILED
    if VerificationStatus.INCONCLUSIVE in statuses:
        return VerificationStatus.INCONCLUSIVE
    if VerificationStatus.NOT_RUN in statuses:
        return VerificationStatus.NOT_RUN
    return VerificationStatus.PASSED
