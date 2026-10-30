import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        body = (
            'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"test",'
            '"choices":[{"index":0,"delta":{"content":"local ok"},"finish_reason":null}]}\n\n'
            'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"test",'
            '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_cli_runs_against_local_openai_protocol(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = tmp_path / "config.toml"
        config.write_text(
            '[model]\nprovider="openai_compatible"\nname="test"\n'
            f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
            'api_key_env="DEITY_TEST_API_KEY"\n'
        )
        env = {**os.environ, "DEITY_TEST_API_KEY": "test"}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "deity.interfaces.cli.main",
                "run",
                "hello",
                "--config",
                str(config),
                "--workspace",
                str(tmp_path),
                "--db",
                str(tmp_path / "deity.db"),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "local ok" in result.stdout
    finally:
        server.shutdown()
        server.server_close()
