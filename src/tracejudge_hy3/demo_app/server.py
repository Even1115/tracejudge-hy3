"""Localhost-only HTTP server for the recording demo page.

Standard-library only (``http.server``), bound exclusively to 127.0.0.1.  The
API surface is deliberately tiny and allowlisted:

- ``GET /`` and ``/static/{index.html,app.js,styles.css}`` -- the static page.
- ``GET /api/status`` -- per-mode availability and the public problem card.
- ``GET /api/overview`` -- public aggregate numbers (see ``overview.py``).
- ``GET /api/showcase`` -- three hash-checked public mechanism cases.
- ``GET /api/regression`` -- the versioned public-fixture regression card.
- ``POST /api/run`` with ``{"mode": "fixture" | "hy3"}`` -- starts one real
  pipeline run in a worker thread and returns a run id.
- ``GET /api/run/<id>`` -- poll that run's status/result.
- ``GET /api/export/<id>/{result.json,certificate.json,report.html}`` -- safe
  downloads for a completed in-memory run.

No endpoint reads arbitrary files, environment variables, or client-supplied
paths; nothing sensitive (API keys, endpoints, absolute paths, private
research materials) is ever serialized to the browser.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from tracejudge_hy3.demo_app.exports import (
    certificate_export_payload,
    render_result_html,
    result_export_payload,
)
from tracejudge_hy3.demo_app.overview import OverviewSourceError, load_public_overview
from tracejudge_hy3.demo_app.regression import RegressionCardError, build_regression_card
from tracejudge_hy3.demo_app.runner import DEMO_MODES, demo_status, run_demo
from tracejudge_hy3.demo_app.showcase import (
    SHOWCASE_SVG_PATH,
    ShowcaseSourceError,
    load_public_showcase,
)

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

_MAX_KEPT_RUNS = 10
_RUN_STALE_SECONDS = 3600
_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")
_EXPORT_FILENAMES = frozenset({"result.json", "certificate.json", "report.html"})


class _RunRegistry:
    """In-memory registry of demo runs executed by worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}

    def start(self, mode: str) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._prune_locked()
            self._runs[run_id] = {
                "status": "running",
                "mode": mode,
                "started_at": time.time(),
                "payload": None,
            }
        thread = threading.Thread(
            target=self._execute, args=(run_id, mode), name=f"demo-run-{run_id}", daemon=True
        )
        thread.start()
        return run_id

    def _execute(self, run_id: str, mode: str) -> None:
        payload = run_demo(mode)
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is not None:
                entry["status"] = "done"
                entry["payload"] = payload

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return None
            response = {
                "run_id": run_id,
                "status": entry["status"],
                "mode": entry["mode"],
                "elapsed_seconds": round(time.time() - entry["started_at"], 1),
            }
            if entry["status"] == "done":
                response["result"] = entry["payload"]
            return response

    def _prune_locked(self) -> None:
        stale = [
            run_id
            for run_id, entry in self._runs.items()
            if time.time() - entry["started_at"] > _RUN_STALE_SECONDS
        ]
        for run_id in stale:
            del self._runs[run_id]
        while len(self._runs) >= _MAX_KEPT_RUNS:
            oldest = min(self._runs, key=lambda key: self._runs[key]["started_at"])
            del self._runs[oldest]


class DemoHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "TraceJudgeDemo/0.1"
    registry: _RunRegistry
    repo_root: Path

    # -- helpers ---------------------------------------------------------

    def _send_bytes(
        self,
        body: bytes,
        *,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        attachment_name: str | None = None,
        content_security_policy: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if attachment_name is not None:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment_name}"')
        if content_security_policy is not None:
            self.send_header("Content-Security-Policy", content_security_policy)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        *,
        attachment_name: str | None = None,
    ) -> None:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self._send_bytes(
            body,
            content_type="application/json; charset=utf-8",
            status=status,
            attachment_name=attachment_name,
        )

    def _send_static(self, filename: str, content_type: str) -> None:
        path = STATIC_DIR / filename
        if not path.is_file() or path.is_symlink():
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self._send_bytes(
            body,
            content_type=content_type,
            content_security_policy=(
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'"
            ),
        )

    def _send_showcase_svg(self) -> None:
        path = self.repo_root / SHOWCASE_SVG_PATH
        if path.is_symlink() or not path.is_file():
            self._send_json({"error": "showcase asset not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_bytes(
            path.read_bytes(),
            content_type="image/svg+xml; charset=utf-8",
            content_security_policy="default-src 'none'; style-src 'unsafe-inline'; sandbox",
        )

    def _send_export(self, path: str) -> None:
        parts = path.split("/")
        if len(parts) != 5:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        _empty, api, export, run_id, filename = parts
        if (
            api != "api"
            or export != "export"
            or _RUN_ID_PATTERN.fullmatch(run_id) is None
            or filename not in _EXPORT_FILENAMES
        ):
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        entry = self.registry.get(run_id)
        if entry is None:
            self._send_json({"error": "unknown run id"}, HTTPStatus.NOT_FOUND)
            return
        if entry["status"] != "done":
            self._send_json({"error": "run is still in progress"}, HTTPStatus.CONFLICT)
            return
        result = entry.get("result")
        if not isinstance(result, dict):
            self._send_json({"error": "run result unavailable"}, HTTPStatus.CONFLICT)
            return
        if filename == "result.json":
            self._send_json(
                result_export_payload(run_id, result),
                attachment_name=f"tracejudge_{run_id}_result.json",
            )
            return
        if filename == "certificate.json":
            certificate = certificate_export_payload(run_id, result)
            if certificate is None:
                self._send_json({"error": "this run has no certificate"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(
                certificate,
                attachment_name=f"tracejudge_{run_id}_certificate.json",
            )
            return
        body = render_result_html(run_id, result).encode("utf-8")
        self._send_bytes(
            body,
            content_type="text/html; charset=utf-8",
            attachment_name=f"tracejudge_{run_id}_report.html",
            content_security_policy="default-src 'none'; style-src 'unsafe-inline'; sandbox",
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep request logs minimal and free of query strings.
        return

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in _STATIC_FILES:
            filename, content_type = _STATIC_FILES[path]
            self._send_static(filename, content_type)
            return
        if path == "/showcase/reasoning-swap.svg":
            self._send_showcase_svg()
            return
        if path == "/api/status":
            self._send_json(demo_status())
            return
        if path == "/api/overview":
            try:
                self._send_json(load_public_overview(self.repo_root))
            except OverviewSourceError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": "公开汇总数据不可用或一致性校验未通过。",
                        "error_type": type(exc).__name__,
                    },
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
            return
        if path == "/api/showcase":
            try:
                self._send_json(load_public_showcase(self.repo_root))
            except ShowcaseSourceError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": "公开案例数据不可用。",
                        "error_type": type(exc).__name__,
                    },
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
            return
        if path == "/api/regression":
            try:
                self._send_json(build_regression_card(self.repo_root))
            except (RegressionCardError, ShowcaseSourceError) as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": "公开回归卡片不可用。",
                        "error_type": type(exc).__name__,
                    },
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
            return
        if path.startswith("/api/export/"):
            self._send_export(path)
            return
        if path.startswith("/api/run/"):
            run_id = path.removeprefix("/api/run/")
            entry = self.registry.get(run_id)
            if entry is None:
                self._send_json({"error": "unknown run id"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(entry)
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/api/run":
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length > 1024:
            self._send_json({"error": "request too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON body"}, HTTPStatus.BAD_REQUEST)
            return
        mode = body.get("mode") if isinstance(body, dict) else None
        if mode not in DEMO_MODES:
            self._send_json({"error": "mode must be 'fixture' or 'hy3'"}, HTTPStatus.BAD_REQUEST)
            return
        run_id = self.registry.start(mode)
        self._send_json({"run_id": run_id, "status": "running"}, HTTPStatus.ACCEPTED)


def make_server(port: int = DEFAULT_PORT, repo_root: str | Path = ".") -> ThreadingHTTPServer:
    """Build the demo server bound to 127.0.0.1 only."""

    handler = type(
        "BoundDemoHTTPRequestHandler",
        (DemoHTTPRequestHandler,),
        {"registry": _RunRegistry(), "repo_root": Path(repo_root)},
    )
    return ThreadingHTTPServer((BIND_HOST, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    server = make_server(port=args.port, repo_root=".")
    host, port = server.server_address[:2]
    print(f"TraceJudge-Hy3 录屏演示服务已启动（仅本机）：http://{host}:{port}/")
    print(f"录制模式：http://{host}:{port}/?recording=1")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
