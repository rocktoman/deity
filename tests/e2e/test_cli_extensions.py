"""Launch the installed CLI against a local streamed model and stdio MCP server."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ModelHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prior = [item for item in body["messages"] if item["role"] == "tool"]
        if len(prior) == 0:
            name, args, call_id = "load_skill", {"name": "demo"}, "skill-1"
        elif len(prior) == 1:
            name = next(
                item["function"]["name"]
                for item in body["tools"]
                if item["function"]["name"].startswith("mcp_")
            )
            args, call_id = {"text": "hello"}, "mcp-1"
        else:
            name = ""
        if name:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta, finish = {"content": "CLI完整通过"}, "stop"
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


def test_full_extension_cli_walkthrough(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        skill = tmp_path / ".deity/skills/demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: demo\ndescription: CLI demo\n---\n正文示例")
        script = Path(__file__).parents[1] / "fixtures/mcp_echo_server.py"
        config = tmp_path / "config.toml"
        config.write_text(
            '[model]\nprovider="openai_compatible"\nname="test"\n'
            'api_key_env="DEITY_TEST_API_KEY"\n'
            f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
            "[extensions]\nmcp_enabled=true\nskills_enabled=true\nhooks_enabled=true\n"
            "[mcp.servers.one]\n"
            f"command={json.dumps(sys.executable)}\nargs=[{json.dumps(str(script))}]\n"
            '[[hooks.registered]]\nname="guard"\npoint="BeforeTool"\nkind="policy"\n'
            'callable="deity.examples.v0_4_hook:deny_delete"\npriority=0\n'
        )
        executable = shutil.which("deity")
        assert executable is not None
        env = {**os.environ, "DEITY_TEST_API_KEY": "fixture"}
        db = tmp_path / "deity.db"

        def cli(*args: str, answer: str = "") -> str:
            completed = subprocess.run(
                [executable, *args],
                input=answer,
                text=True,
                capture_output=True,
                env=env,
                timeout=45,
                check=False,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            return completed.stdout

        assert "mcp" in cli("--help")
        assert "one" in cli("mcp", "list", "--config", str(config), "--workspace", str(tmp_path))
        assert "mcp:one:echo" in cli(
            "mcp",
            "test",
            "--config",
            str(config),
            "--workspace",
            str(tmp_path),
            "--db",
            str(db),
            answer="y\n",
        )
        assert "mcp:one:echo" in cli(
            "mcp",
            "test",
            "--config",
            str(config),
            "--workspace",
            str(tmp_path),
            "--db",
            str(db),
        )
        assert "demo" in cli("skills", "list", "--workspace", str(tmp_path))
        assert "CLI demo" in cli("skills", "inspect", "demo", "--workspace", str(tmp_path))
        assert "CLI完整通过" in cli(
            "run",
            "使用 demo Skill 然后调用 echo",
            "--config",
            str(config),
            "--workspace",
            str(tmp_path),
            "--db",
            str(db),
            answer="y\n",
        )
        assert "Deity>" in cli(
            "run",
            "--config",
            str(config),
            "--workspace",
            str(tmp_path),
            "--db",
            str(db),
            answer="交互测试\ny\n/exit\n",
        )
        assert str(tmp_path) in cli("sessions", "--db", str(db))
        with sqlite3.connect(db) as connection:
            run_id = connection.execute(
                "SELECT id FROM runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        assert "run_not_interrupted" in cli(
            "runs",
            "inspect",
            run_id,
            "--config",
            str(config),
            "--workspace",
            str(tmp_path),
            "--db",
            str(db),
        )
        rejected = subprocess.run(
            [
                executable,
                "resume",
                run_id,
                "--config",
                str(config),
                "--workspace",
                str(tmp_path),
                "--db",
                str(db),
            ],
            input="",
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
            check=False,
        )
        assert rejected.returncode == 2 and "run_not_interrupted" in rejected.stdout
    finally:
        server.shutdown()
        server.server_close()
