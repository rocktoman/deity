"""Approve one immutable plan and create its Build Run atomically."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from deity.adapters.git import GitWorkspaceAdapter
from deity.adapters.storage import SQLiteSessionRepository
from deity.core.contracts.workspace import WorkspaceSnapshot


@dataclass(frozen=True)
class BuildStart:
    plan_id: str
    build_run_id: str
    change_set_id: str


class BuildService:
    def __init__(self, db_path: Path, workspace: Path, *, plan_ttl_seconds: int) -> None:
        self.db_path = db_path
        self.workspace = workspace.resolve(strict=True)
        self.plan_ttl_seconds = plan_ttl_seconds

    async def initial_dirty_targets(self, plan_id: str) -> tuple[str, ...]:
        """List plan targets with local Git changes before asking for explicit consent."""
        repo = await SQLiteSessionRepository.open(self.db_path)
        try:
            plan = await repo.get_plan(plan_id)
            if plan.status != "draft":
                raise ValueError("plan is not buildable")
            age = (datetime.now(UTC) - datetime.fromisoformat(plan.created_at)).total_seconds()
            if age > self.plan_ttl_seconds:
                raise ValueError("plan expired")
            baseline = await repo.get_workspace_snapshot(plan.workspace_snapshot_id)
            targets = {path for step in plan.draft.steps for path in step.files}
            current = await GitWorkspaceAdapter(self.workspace, ignored=(self.db_path,)).capture(
                tuple(sorted(targets))
            )
            if current.digest != baseline.digest:
                raise ValueError("workspace drift")
            return tuple(sorted(self._dirty_targets(baseline, targets)))
        finally:
            await repo.close()

    async def start(self, plan_id: str, *, allow_dirty_paths: tuple[str, ...] = ()) -> BuildStart:
        """Validate plan freshness and persist approval, run and ChangeSet together."""
        allowed = self._validate_allowlist(allow_dirty_paths)
        repo = await SQLiteSessionRepository.open(self.db_path)
        try:
            plan = await repo.get_plan(plan_id)
            if plan.status == "approved":
                raise ValueError("plan already approved")
            if plan.status != "draft":
                raise ValueError("plan is not buildable")
            age = (datetime.now(UTC) - datetime.fromisoformat(plan.created_at)).total_seconds()
            if age > self.plan_ttl_seconds:
                await repo.mark_plan_stale(plan_id, "expired")
                raise ValueError("plan expired")
            baseline = await repo.get_workspace_snapshot(plan.workspace_snapshot_id)
            adapter = GitWorkspaceAdapter(self.workspace, ignored=(self.db_path,))
            targets = tuple(sorted({path for step in plan.draft.steps for path in step.files}))
            current = await adapter.capture(targets)
            if current.digest != baseline.digest:
                await repo.mark_plan_stale(plan_id, "workspace_drift")
                raise ValueError("workspace drift")
            blocked = sorted(self._dirty_targets(baseline, set(targets)) - allowed)
            if blocked:
                raise ValueError(f"initial dirty path requires allowlist: {blocked[0]}")
            await repo.create_workspace_snapshot(current)
            build_run_id, change_set_id = await repo.approve_plan_and_create_build(
                plan, current.snapshot_id, tuple(sorted(allowed))
            )
            return BuildStart(plan_id, build_run_id, change_set_id)
        finally:
            await repo.close()

    @staticmethod
    def _dirty_targets(baseline: WorkspaceSnapshot, targets: set[str]) -> set[str]:
        return {
            item.path
            for item in baseline.files
            if baseline.git_head is not None
            and item.path in targets
            and item.status not in {"!", "clean", "absent"}
        }

    def _validate_allowlist(self, values: tuple[str, ...]) -> set[str]:
        allowed: set[str] = set()
        for value in values:
            relative = Path(value)
            candidate = (self.workspace / relative).resolve(strict=False)
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or any(char in value for char in "*?[]")
                or not candidate.is_relative_to(self.workspace)
                or candidate.is_dir()
                or candidate.is_symlink()
            ):
                raise ValueError("invalid allow-dirty path")
            allowed.add(relative.as_posix())
        return allowed
