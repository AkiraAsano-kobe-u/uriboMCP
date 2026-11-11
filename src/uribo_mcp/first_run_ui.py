"""Local browser form for the first-run setup.

The form exists so a person can type their university password into the OS
keyring without it passing through a terminal transcript or an AI agent. The
server listens on the loopback interface only, for one setup, and requires a
random token on every request.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from uribo_mcp import first_run
from uribo_mcp.errors import ConfigurationError, UriboMcpError

TOKEN_HEADER = "X-Uribo-Setup-Token"
MAX_BODY_BYTES = 64 * 1024


class SetupSession:
    """State shared between the HTTP handler threads and the waiting CLI."""

    def __init__(
        self,
        *,
        root: Path,
        config_path: Path,
        initial: first_run.SetupPlan,
        secret_store=None,
    ):
        self.root = root
        self.config_path = config_path
        self.initial = initial
        self.secret_store = secret_store
        self.token = secrets.token_urlsafe(24)
        self.done = threading.Event()
        self.result: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def _credentials_stored(self) -> bool | None:
        if self.secret_store is None:
            return first_run.credentials_stored()
        from uribo_mcp.auth import PASSWORD_KEY, USERNAME_KEY

        return bool(self.secret_store.get(USERNAME_KEY) and self.secret_store.get(PASSWORD_KEY))

    def state(self) -> dict[str, Any]:
        return {
            "storage_dir": str(self.initial.storage_dir),
            "export_root": str(self.initial.export_root),
            "sep": os.sep,
            "sources": [
                {
                    "id": option.id,
                    "label": option.label,
                    "note": option.note,
                    "required": option.required,
                    "checked": option.id in self.initial.sources,
                }
                for option in first_run.SOURCES
            ],
            "credentials_stored": bool(self._credentials_stored()),
            "config_path": str(self.config_path),
            "config_exists": self.config_path.is_file(),
        }

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        storage = str(payload.get("storage_dir") or "")
        export = str(payload.get("export_root") or "").strip() or None
        sources = payload.get("sources")
        if not isinstance(sources, list):
            raise ConfigurationError("sources must be a list")
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")

        plan = first_run.build_plan(storage, export_root=export, sources=sources)
        if plan.storage_dir == self.initial.storage_dir:
            # Re-run on the same folder: keep customised sub-locations as they are.
            plan = first_run.build_plan(
                storage,
                data_dir=self.initial.data_dir,
                cache_dir=self.initial.cache_dir,
                export_root=export or self.initial.export_root,
                credentials_dir=self.initial.credentials_dir,
                sources=sources,
            )
        if bool(username) != bool(password):
            raise ConfigurationError("ログインIDとパスワードの両方を入力してください")
        if not username and not self._credentials_stored():
            raise ConfigurationError("ログインIDとパスワードを入力してください")

        with self._lock:
            if username:
                first_run.store_credentials(username, password, self.secret_store)
            result = first_run.apply_setup(
                plan,
                config_path=self.config_path,
                root=self.root,
                force=True,
                credentials_present=self._credentials_stored(),
            )
            self.result = result
        self.done.set()
        return result


def _page(token: str) -> tuple[bytes, str]:
    nonce = secrets.token_urlsafe(16)
    html = resources.files("uribo_mcp").joinpath("first_run.html").read_text(encoding="utf-8")
    body = html.replace("__NONCE__", nonce).replace("__TOKEN__", token).encode("utf-8")
    return body, nonce


def _make_handler(session: SetupSession):
    class Handler(BaseHTTPRequestHandler):
        server_version = "uriboMCP-setup"
        sys_version = ""

        def log_message(self, format, *args):  # noqa: A002 - silence request logging
            return

        # -- helpers ---------------------------------------------------------
        def _allowed_hosts(self) -> set[str]:
            port = self.server.server_address[1]
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _send(self, status: int, body: bytes, content_type: str, extra=None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: dict[str, Any]) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _guard(self, *, api: bool) -> bool:
            """Reject anything that is not this setup's own page talking to itself."""
            if self.headers.get("Host", "") not in self._allowed_hosts():
                self._json(403, {"error": "forbidden"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{host}" for host in self._allowed_hosts()}:
                self._json(403, {"error": "forbidden"})
                return False
            if api:
                supplied = self.headers.get(TOKEN_HEADER, "")
            else:
                supplied = (parse_qs(urlsplit(self.path).query).get("t") or [""])[0]
            if not secrets.compare_digest(supplied.encode(), session.token.encode()):
                self._json(403, {"error": "forbidden"})
                return False
            return True

        # -- routes ----------------------------------------------------------
        def do_GET(self):  # noqa: N802
            route = urlsplit(self.path).path
            if route == "/":
                if not self._guard(api=False):
                    return
                body, nonce = _page(session.token)
                csp = (
                    "default-src 'none'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                    f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'"
                )
                self._send(200, body, "text/html; charset=utf-8", {"Content-Security-Policy": csp})
            elif route == "/api/state":
                if self._guard(api=True):
                    self._json(200, session.state())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if urlsplit(self.path).path != "/api/save":
                self._json(404, {"error": "not found"})
                return
            if not self._guard(api=True):
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(415, {"error": "unsupported media type"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY_BYTES:
                self._json(413, {"error": "invalid body size"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("body must be an object")
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "invalid JSON"})
                return
            try:
                result = session.save(payload)
            except UriboMcpError as exc:
                self._json(400, {"error": str(exc)})
            except OSError as exc:
                self._json(400, {"error": f"フォルダを作成できません: {exc.strerror or exc}"})
            except Exception as exc:  # keyring backends raise their own types
                self._json(500, {"error": f"{type(exc).__name__}: 資格情報を保存できませんでした"})
            else:
                self._json(200, {"ok": True, "config_path": result["config_path"]})

    return Handler


def start_server(session: SetupSession) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(session))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="uribo-setup-ui", daemon=True).start()
    return server


def setup_url(server: ThreadingHTTPServer, session: SetupSession) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/?t={session.token}"


def run_setup_ui(
    *,
    root: Path,
    config_path: Path,
    initial: first_run.SetupPlan,
    open_browser: bool = True,
    timeout: float = 900.0,
) -> dict[str, Any]:
    """Serve the form until it is saved or ``timeout`` seconds pass."""
    session = SetupSession(root=root, config_path=config_path, initial=initial)
    server = start_server(session)
    url = setup_url(server, session)
    try:
        # stderr keeps stdout clean for the final JSON result.
        print(f"Setup form: {url}", file=sys.stderr, flush=True)
        print("Waiting for the form to be saved in the browser...", file=sys.stderr, flush=True)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        if not session.done.wait(timeout):
            return {
                "ok": False,
                "reason": "timeout",
                "detail": f"The setup form was not saved within {int(timeout)} seconds.",
            }
        return session.result or {"ok": False, "reason": "no result"}
    finally:
        # Let the browser receive the save response before the socket closes.
        threading.Event().wait(0.3)
        server.shutdown()
        server.server_close()


__all__ = ["SetupSession", "run_setup_ui", "setup_url", "start_server"]
