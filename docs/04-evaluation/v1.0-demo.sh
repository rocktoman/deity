#!/usr/bin/env bash
# Deity V1.0 端到端演示：安装 → 配置 → 分析 → 修改 → 验证 → 恢复。
# 全程 FakeModel 离线可跑，不调真实模型、不联网。
# 用法：bash docs/04-evaluation/v1.0-demo.sh
# 日志：evals/reports/v1.0-demo.log
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"
LOG="$ROOT/evals/reports/v1.0-demo.log"
mkdir -p "$ROOT/evals/reports"
exec > >(tee "$LOG") 2>&1

say() { echo "=== $* ==="; }

say "1/6 安装：同步依赖并核对版本"
uv sync --locked --dev --extra tui >/dev/null
uv run --locked deity --version
uv run --locked python -c "import deity; print('deity', deity.__version__)"

say "2/6 配置：写演示配置并跑 doctor"
DEMO="$(mktemp -d /tmp/deity-v10-demo-XXXXXX)"
trap 'rm -rf "$DEMO"' EXIT
cp -r tests/fixtures/python-mini-repo "$DEMO/ws"
cp -r tests/fixtures/python-mini-repo "$DEMO/pristine"
cat > "$DEMO/config.toml" <<'EOF'
version = 1

[run]
max_turns = 10

[permission]
shell = "ask"
workspace_write = "ask"

[model]
provider = "openai_compatible"
name = "fake"
api_key_env = "DEITY_DEMO_API_KEY"
EOF
uv run --locked deity doctor --config "$DEMO/config.toml" --workspace "$DEMO/ws"

say "3/6 分析 + 4/6 修改：FakeModel 读 calc.py，发现 a-b 之误并打补丁"
uv run --locked python - "$DEMO/ws" "$DEMO/deity.db" <<'PY'
import asyncio
import sys
from pathlib import Path

from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, RunStatus


class FakeModel:
    def __init__(self) -> None:
        self._turns = iter(
            [
                [
                    ModelEvent("tool_call_started", call_id="d1", name="read_file"),
                    ModelEvent(
                        "tool_call_arguments_delta",
                        call_id="d1",
                        text='{"path": "calc.py"}',
                    ),
                    ModelEvent("message_completed"),
                ],
                [
                    ModelEvent("tool_call_started", call_id="d2", name="apply_patch"),
                    ModelEvent(
                        "tool_call_arguments_delta",
                        call_id="d2",
                        text='{"path": "calc.py", "old": "return a - b", "new": "return a + b"}',
                    ),
                    ModelEvent("message_completed"),
                ],
                [ModelEvent("text_delta", text="已修复 add：a-b 改为 a+b"), ModelEvent("message_completed")],
            ]
        )

    async def stream(self, request):  # type: ignore[no-untyped-def]
        for event in next(self._turns):
            yield event


async def main() -> None:
    workspace, db = Path(sys.argv[1]), Path(sys.argv[2])
    service = RunService(
        db_path=db, workspace=workspace, model=FakeModel(),  # type: ignore[arg-type]
        approve=lambda name, args: True,
    )
    result = await service.run("分析 calc.py 的 add 实现，修复其中的错误", model_name="fake")
    assert result.status == RunStatus.COMPLETED, result.status
    print("run status:", result.status.value)
    print("final text:", result.final_text)


asyncio.run(main())
PY
grep -n "return a + b" "$DEMO/ws/calc.py"

say "5/6 验证：跑 fixture 自带测试"
uv run --locked python -m pytest -q "$DEMO/ws/test_calc.py"

say "6/6 恢复：用干净副本恢复工作区，确认回到修改前"
cp "$DEMO/pristine/calc.py" "$DEMO/ws/calc.py"
grep -n "return a - b" "$DEMO/ws/calc.py"
uv run --locked python -m pytest -q "$DEMO/ws/test_calc.py" && echo "UNEXPECTED: 恢复后测试仍通过" || echo "恢复确认：原始 bug 已回来，测试按预期失败"
uv run --locked deity eval replay --help >/dev/null && echo "deity eval replay 可用"

say "演示完成，日志：$LOG"
