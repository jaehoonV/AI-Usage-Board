"""Loopback-only control plane shared by the widget and its helper processes.

Hardening, in order of what actually stops something:

* bound to 127.0.0.1 - never reachable off the machine;
* every request needs ``Authorization: Bearer <token>`` compared with
  :func:`secrets.compare_digest`; the token is 64 hex chars generated per
  install and kept in the user's own data directory;
* ``Host`` must be ``127.0.0.1:<our port>`` so a DNS-rebinding page cannot
  reach us;
* any ``Origin`` header is rejected - every legitimate caller is one of our
  own processes and sends none, so a request carrying one came from a page;
* bodies are capped and every payload goes through ``validate_snapshot``.
"""
from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from .. import BRIDGE_PORT, __version__
from ..core.models import SERVICES, SnapshotError, validate_snapshot

MAX_BODY = 16 * 1024


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, store, accounts, token: str, port: int = BRIDGE_PORT) -> None:
        self.store = store
        self.accounts = accounts
        self.token = token
        super().__init__(("127.0.0.1", port), BridgeHandler)


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "AI Usage Board"
    sys_version = ""

    # -- plumbing ---------------------------------------------------------
    def log_message(self, *_args: Any) -> None:  # noqa: D401 - silence stderr spam
        """No request logging: the widget is not a web server."""

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "") == f"127.0.0.1:{self.server.server_port}"

    def _origin_ok(self) -> bool:
        # Our helpers are plain HTTP clients and never send one; browsers always
        # do on a cross-origin request.
        return self.headers.get("Origin") is None

    def _authorized(self) -> bool:
        expected = "Bearer " + self.server.token
        return secrets.compare_digest(expected, self.headers.get("Authorization") or "")

    def _respond(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _gate(self) -> bool:
        if not self._host_ok() or not self._origin_ok():
            self._respond(403, {"ok": False})
            return False
        return True

    def _body(self) -> Optional[Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return None

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._gate():
            return
        if self.path == "/health":
            self._respond(200, {"app": "AI Usage Board", "version": __version__})
            return
        if not self._authorized():
            self._respond(403, {"ok": False})
            return

        if self.path == "/snapshot":
            self._respond(
                200,
                {
                    "accounts": self.server.accounts.status(),
                    "accountErrors": self.server.accounts.errors(),
                    "usage": self.server.store.snapshot(),
                },
            )
            return
        if self.path.startswith("/command/"):
            provider = self.path.removeprefix("/command/")
            if provider in SERVICES:
                self._respond(200, self.server.accounts.command(provider))
                return
        self._respond(404, {"ok": False})

    def do_POST(self) -> None:  # noqa: N802
        if not self._gate():
            return
        if not self._authorized():
            self._respond(403, {"ok": False})
            return

        if self.path == "/activate":
            self.server.store.reveal_requested.set()
            self._respond(200, {"ok": True})
            return

        if self.path == "/quit":
            # Answer before the widget starts tearing things down, so the
            # caller learns the request landed rather than seeing a dropped
            # connection.
            self._respond(200, {"ok": True})
            self.server.store.shutdown_requested.set()
            return

        payload = self._body()
        if payload is None or not isinstance(payload, dict):
            self._respond(400, {"ok": False, "reason": "bad_payload"})
            return

        if self.path == "/account-status":
            provider = str(payload.get("provider") or "")
            state = str(payload.get("state") or "")
            error_type = str(payload.get("errorType") or "")
            try:
                self.server.accounts.set_status(provider, state, error_type)
            except ValueError:
                self._respond(400, {"ok": False, "reason": "bad_state"})
                return
            if state in (
                "login_required", "engine_error", "page_error",
                "closed", "verify_required", "verify_stuck", "login_manual",
            ):
                message = _ACCOUNT_MESSAGES.get(state, "연결 상태를 확인해 주세요")
                if state == "engine_error":
                    message = _ENGINE_MESSAGES.get(error_type, message)
                self.server.store.fail(
                    provider,
                    message,
                    source="account",
                    error_type=state,
                )
            else:
                self.server.store.clear_error(provider)
            self._respond(200, {"ok": True})
            return

        if self.path == "/usage":
            try:
                snapshot = validate_snapshot(payload, source="account")
            except SnapshotError as error:
                self._respond(400, {"ok": False, "reason": str(error)})
                return
            self.server.store.update(snapshot)
            self._respond(200, {"ok": True})
            return

        self._respond(404, {"ok": False})


_ACCOUNT_MESSAGES = {
    "login_manual": "열린 창에서 로그인한 뒤 '로그인 완료'를 눌러 주세요",
    "verify_required": "보안 확인 필요 · '창 열기'를 눌러 체크해 주세요",
    "verify_stuck": "보안 확인이 계속 반복됩니다 · '다시 연결'을 누르면 로그인부터 새로 시작합니다",
    "login_required": "계정 연결 필요",
    "engine_error": "연결 창을 열지 못했습니다",
    "page_error": "사용량 화면 확인 필요",
    "closed": "계정 연결 창이 닫혔습니다",
}

#: A bare "could not open" leaves the user nowhere; say what is missing.
_ENGINE_MESSAGES = {
    "BrowserNotInstalled": "Chrome 또는 Edge를 찾을 수 없습니다 · 둘 중 하나를 설치해 주세요",
    "BrowserExited": "브라우저가 바로 종료됐습니다 · '다시 연결'을 눌러 주세요",
    "BrowserStartTimeout": "브라우저가 응답하지 않습니다 · 다시 시도해 주세요",
    "HelperCrashed": "연결 창이 예기치 않게 종료됐습니다 · runtime.log를 확인해 주세요",
}
