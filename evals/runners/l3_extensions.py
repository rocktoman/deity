"""Explicit DeepSeek acceptance for V0.4 MCP and Skill flows."""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import (
    Config,
    ExtensionConfig,
    McpServerSettings,
    ModelConfig,
    RunConfig,
)
from deity.application.run_service import RunService

ROOT = Path(__file__).parents[2]
REPORTS = ROOT / "evals/reports"


async def run_case(kind: str) -> dict[str, Any]:
    """Run one bounded real-model task in a fresh disposable workspace."""
    with tempfile.TemporaryDirectory(prefix=f"deity-v04-l3-{kind}-") as temporary:
        workspace = Path(temporary)
        skill_dir = workspace / ".deity/skills/demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: 回答验证码时使用\n---\n验证码是 SKILL-42"
        )
        script = ROOT / "tests/fixtures/mcp_echo_server.py"
        config = Config(
            model=ModelConfig(
                "openai_compatible",
                "deepseek-v4-flash",
                "DEEPSEEK_API_KEY",
                "https://api.deepseek.com",
                8192,
            ),
            run=RunConfig(
                max_turns=5,
                max_tool_calls=5,
                deadline_seconds=180,
                max_output_tokens=512,
                max_input_tokens_total=40_000,
                max_output_tokens_total=4_000,
            ),
            extensions=ExtensionConfig(mcp_enabled=kind == "mcp", skills_enabled=kind == "skill"),
            mcp_servers=(McpServerSettings("one", sys.executable, (str(script),)),)
            if kind == "mcp"
            else (),
        )

        def approve(name: str, args: dict[str, Any]) -> bool:
            if name == "mcp_server_start":
                return args.get("server") == "one"
            if name == "mcp_tool":
                return args.get("server") == "one" and args.get("tool") == "echo"
            return False

        service = RunService.from_config(
            config, db_path=workspace / "deity.db", workspace=workspace, approve=approve
        )
        prompt = (
            "必须调用 MCP 工具 mcp:one:echo，参数 text 为 MCP-42。"
            "只根据实际工具结果回答，回答包含 MCP-42。"
            if kind == "mcp"
            else "必须先调用 load_skill，name 为 demo；再根据加载的正文回答验证码。"
        )
        started = time.monotonic()
        run_id: str | None = None
        final_text: str | None = None
        error: str | None = None
        try:
            result = await service.run(prompt, model_name="deepseek-v4-flash")
            run_id, final_text = result.run_id, result.final_text
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            error = type(exc).__name__
        repo = await SQLiteSessionRepository.open(workspace / "deity.db")
        try:
            if run_id is None:
                async with repo.db.execute(
                    "SELECT id FROM runs ORDER BY rowid DESC LIMIT 1"
                ) as cursor:
                    row = await cursor.fetchone()
                run_id = row[0] if row else None
            events = await repo.events(run_id) if run_id else []
        finally:
            await repo.close()
        kinds = [event["type"] for event in events]
        expected = "ToolCompleted" if kind == "mcp" else "SkillActivated"
        marker = "MCP-42" if kind == "mcp" else "SKILL-42"
        passed = (
            error is None
            and marker in (final_text or "")
            and expected in kinds
            and any(
                event["type"] == "ToolCompleted" and event["payload"]["status"] == "ok"
                for event in events
            )
        )
        usage = [event["payload"] for event in events if event["type"] == "ModelUsage"]
        return {
            "case_id": f"L3-V04-{kind.upper()}",
            "passed": passed,
            "verification": "PASS" if passed else "FAIL",
            "run_id": run_id,
            "error_type": error,
            "final_contains_marker": marker in (final_text or ""),
            "tool_calls": kinds.count("ToolClaimed"),
            "model_calls": kinds.count("ModelTurnCompleted"),
            "input_tokens": sum(item["input_tokens"] or 0 for item in usage) if usage else None,
            "output_tokens": sum(item["output_tokens"] or 0 for item in usage) if usage else None,
            "duration_seconds": round(time.monotonic() - started, 2),
            "cost": "UNKNOWN",
        }


async def main() -> int:
    """Run both real-model extension cases outside ordinary PR CI."""
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("DEEPSEEK_API_KEY is required", file=sys.stderr)
        return 2
    REPORTS.mkdir(parents=True, exist_ok=True)
    results = [await run_case(kind) for kind in ("mcp", "skill")]
    for result in results:
        stem = result["case_id"]
        (REPORTS / f"{stem}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        (REPORTS / f"{stem}.md").write_text(
            f"# {stem}\n\n验证：{result['verification']}\n\n"
            f"工具调用：{result['tool_calls']}\n\n耗时：{result['duration_seconds']} 秒\n"
        )
    print(f"{sum(item['passed'] for item in results)}/2 V0.4 L3 cases passed")
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
