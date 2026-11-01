"""Exercise the installed Plan/Build CLI against a deterministic local model."""

import json
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class PlanBuildModelHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        tools = {item["function"]["name"] for item in body["tools"]}
        prior = [item for item in body["messages"] if item["role"] == "tool"]
        name = ""
        arguments: dict[str, object] = {}
        if "submit_plan" in tools and not prior:
            name = "submit_plan"
            arguments = {
                "schema_version": 1,
                "goal": "更新示例值",
                "assumptions": [],
                "steps": [
                    {
                        "ordinal": 1,
                        "title": "更新文件",
                        "description": "把旧值替换为新值",
                        "files": ["feature.py"],
                        "verification": ["读取文件确认"],
                    }
                ],
                "risks": [],
                "verification": ["读取文件确认"],
            }
        elif "submit_plan" not in tools and len(prior) == 0:
            name, arguments = "read_file", {"path": "feature.py"}
        elif "submit_plan" not in tools and len(prior) == 1:
            name = "apply_patch"
            arguments = {"path": "feature.py", "old": 'VALUE = "old"', "new": 'VALUE = "new"'}
        if name:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call-{len(prior) + 1}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta, finish = {"content": "V0.5 CLI 完成"}, "stop"
        chunks = [
            {
                "id": "local",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": "local",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            },
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        payload = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        self.wfile.write((payload + "data: [DONE]\n\n").encode())

    def log_message(self, *_args: object) -> None:
        pass


def test_plan_show_and_build_cli(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PlanBuildModelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (tmp_path / "feature.py").write_text('VALUE = "old"\n')
        config = tmp_path / "config.toml"
        config.write_text(
            '[model]\nprovider="openai_compatible"\nname="test"\n'
            'api_key_env="DEITY_TEST_API_KEY"\n'
            f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
            '[permission]\nworkspace_write="ask"\nshell="deny"\n'
            "[coding]\nplan_build_enabled=true\ngit_guard_enabled=true\n"
        )
        executable = shutil.which("deity")
        assert executable is not None
        database = tmp_path / "deity.db"
        environment = {**os.environ, "DEITY_TEST_API_KEY": "fixture"}

        def cli(*arguments: str, answer: str = "") -> str:
            completed = subprocess.run(
                [executable, *arguments],
                input=answer,
                text=True,
                capture_output=True,
                env=environment,
                timeout=45,
                check=False,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            return completed.stdout

        common = ("--config", str(config), "--workspace", str(tmp_path), "--db", str(database))
        planned = cli("plan", "更新示例", *common, answer="n\n")
        match = re.search(r"[0-9a-f]{8}-[0-9a-f-]{27}", planned)
        assert match is not None
        plan_id = match.group(0)
        shown = cli("plans", "show", plan_id, "--db", str(database))
        assert plan_id in shown and "更新示例值" in shown
        cancelled = cli("build", "--plan", plan_id, *common, answer="n\n")
        assert "已取消 Build" in cancelled
        assert (tmp_path / "feature.py").read_text() == 'VALUE = "old"\n'
        built = cli("build", "--plan", plan_id, *common, answer="y\n")
        assert "V0.5 CLI 完成" in built
        assert built.count("批准此计划并执行 Build？") == 1
        assert "允许本次调用" not in built
        assert (tmp_path / "feature.py").read_text() == 'VALUE = "new"\n'
        (tmp_path / "feature.py").write_text('VALUE = "old"\n')
        automatic = cli("plan", "再次更新示例", *common, answer="y\n")
        assert "V0.5 CLI 完成" in automatic
        assert automatic.count("批准此计划并立即执行 Build？") == 1
        assert "允许本次调用" not in automatic
        assert (tmp_path / "feature.py").read_text() == 'VALUE = "new"\n'
    finally:
        server.shutdown()
        server.server_close()
