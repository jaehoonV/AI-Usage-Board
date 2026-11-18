"""Tiny client used by helper processes to reach the widget's bridge."""
from __future__ import annotations

import json
import re
import secrets
from typing import Any, Dict, Optional
from urllib.request import ProxyHandler, Request, build_opener

from .. import BRIDGE_PORT
from ..config import TOKEN_FILE, atomic_write

TOKEN_PATTERN = re.compile(r"[a-f0-9]{64}")
_opener = build_opener(ProxyHandler({}))


def read_token() -> str:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""
    return token if TOKEN_PATTERN.fullmatch(token) else ""


def write_token() -> str:
    """Return the existing token, or mint and persist a new one."""
    token = read_token()
    if token:
        return token
    token = secrets.token_hex(32)
    atomic_write(TOKEN_FILE, token)
    return token


class BridgeClient:
    def __init__(self, token: str = "", port: int = BRIDGE_PORT) -> None:
        self.token = token or read_token()
        self.base = f"http://127.0.0.1:{port}"

    def _call(self, path: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 3.0) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            self.base + path,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Host": f"127.0.0.1:{self.base.rsplit(':', 1)[1]}",
            },
            method="POST" if data is not None else "GET",
        )
        with _opener.open(request, timeout=timeout) as response:
            return json.load(response)

    # -- helper-process API ----------------------------------------------
    def report_status(self, provider: str, state: str, error_type: str = "") -> None:
        try:
            self._call("/account-status", {"provider": provider, "state": state, "errorType": error_type})
        except OSError:
            pass

    def publish(self, snapshot: Dict[str, Any]) -> None:
        try:
            self._call("/usage", snapshot)
        except OSError:
            pass

    #: Consecutive failed polls before we accept that the widget is gone.
    #: One missed poll means nothing - the bridge may still be starting, or
    #: the widget may be busy. Treating it as "shut down" would kill the
    #: helper on startup and leave its browser running with no owner.
    GIVE_UP_AFTER = 20

    def command(self, provider: str) -> Dict[str, Any]:
        try:
            result = self._call(f"/command/{provider}")
        except OSError:
            self._misses = getattr(self, "_misses", 0) + 1
            return {"close": True} if self._misses >= self.GIVE_UP_AFTER else {}
        self._misses = 0
        return result if isinstance(result, dict) else {}

    def activate(self) -> None:
        self._call("/activate", {})

    def quit(self) -> None:
        """Ask the running widget to shut down cleanly."""
        self._call("/quit", {})


def ping(token: str, port: int = BRIDGE_PORT, timeout: float = 2.0) -> bool:
    """True when another instance already owns the port."""
    try:
        BridgeClient(token, port)._call("/activate", {}, timeout=timeout)
        return True
    except OSError:
        return False
