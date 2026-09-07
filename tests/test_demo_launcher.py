"""Exercise repeated PowerShell launches without starting Docker or a model."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher")


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh.exe"])
@pytest.mark.parametrize(
    ("app", "offline_server", "hy3_available", "offline_requested", "reuses"),
    [
        ("tracejudge-hy3", None, True, False, True),  # compatible legacy server
        ("tracejudge-hy3", True, False, True, True),
        ("tracejudge-hy3", True, False, False, False),
        ("tracejudge-hy3", False, True, True, False),
        ("unrelated-app", False, True, False, False),
    ],
)
def test_repeated_launcher_checks_service_and_mode(
    tmp_path, shell, app, offline_server, hy3_available, offline_requested, reuses
):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} unavailable")
    payload = {
        "app": {"name": app, "offline_mode": offline_server},
        "modes": {"fixture": {"available": True}, "hy3": {"available": hy3_available}},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/status"
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        # If early reuse breaks, Docker discovery must fail harmlessly instead
        # of touching the user's installation or starting a second demo.
        env = {
            **os.environ,
            "PATH": str(tmp_path),
            "LOCALAPPDATA": str(tmp_path),
            "ProgramFiles": str(tmp_path),
        }
        cmd = [
            executable,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts/run_recording_demo.ps1"),
            "-Port",
            str(server.server_port),
        ]
        if offline_requested:
            cmd.append("-Offline")
        result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, timeout=25)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        if reuses:
            assert result.returncode == 0, output
            assert "Reusing the existing service" in output
            assert f"http://127.0.0.1:{server.server_port}/?recording=1" in output
        else:
            assert result.returncode != 0, output
            # PowerShell 7 wraps formatted error messages at the console width.
            assert "another port" in output and "-Port" in output
        assert "Demo readiness" not in output
        assert "Starting Docker" not in output
        assert "Traceback" not in output
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
