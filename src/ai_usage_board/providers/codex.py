"""Codex quota via the official `codex app-server` JSON-RPC interface.

This never touches credentials. It starts the user's own, already-signed-in
Codex CLI as a child process, speaks line-delimited JSON-RPC to it over stdio,
and asks for rate limits. Login, when needed, is handed to the user's browser
on an URL the CLI itself returns (and that we re-check against a host allowlist
before opening).
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import queue
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .. import __version__
from ..core.models import SnapshotError

LOGIN_HOSTS = ("auth.openai.com", "chatgpt.com")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_cached_path: Optional[Path] = None


class CodexError(RuntimeError):
    """Any failure to obtain Codex numbers."""


class CodexNotInstalled(CodexError):
    """No usable CLI on this machine. Retrying will not change that until the
    user installs it, so the widget stops polling and waits for 다시 시도."""


class CodexRpcError(CodexError):
    """The CLI answered a request with a JSON-RPC error.

    Says nothing by itself about the account: an unknown method, bad params
    or a backend hiccup all arrive this way. :func:`read_codex` asks the CLI
    whether anyone is signed in before calling it a login problem.
    """

    def __init__(self, code: Any, message: Any) -> None:
        self.code = code if isinstance(code, int) and not isinstance(code, bool) else None
        self.detail = message if isinstance(message, str) else ""
        super().__init__("Codex 사용량 조회에 실패했습니다 · 잠시 뒤 다시 시도합니다")


class CodexLoginRequired(CodexError):
    """The CLI answered, but the account is not usable without signing in.

    Separate from a plain failure so the widget can offer 계정 연결 instead of
    a retry that would fail the same way.
    """


# --------------------------------------------------------------------------
# locating the CLI
# --------------------------------------------------------------------------
#: PE subsystem values. A command-line tool is 3 (console); the Codex desktop
#: app ships GUI binaries called Codex.exe that would otherwise look like a
#: match and then exit the moment we hand them app-server arguments.
_SUBSYSTEM_CONSOLE = 3


def is_console_executable(path: Path) -> bool:
    """True when the PE image is a console program, read from its header.

    Checked instead of executing the candidate: running a GUI binary to find
    out what it is would pop a window at the user.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(1024)
        if head[:2] != b"MZ":
            return False
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if head[pe:pe + 4] != b"PE\0\0":
            return False
        # COFF header is 20 bytes; Subsystem sits 68 bytes into the optional
        # header, at the same offset for both PE32 and PE32+.
        subsystem = struct.unpack_from("<H", head, pe + 24 + 68)[0]
        return subsystem == _SUBSYSTEM_CONSOLE
    except (OSError, struct.error, IndexError):
        return False


def _npm_roots(on_path: Optional[str]) -> List[Path]:
    """Global node_modules folders an `npm i -g @openai/codex` may have used."""
    roots: List[Path] = []
    # The `codex` on PATH is usually npm's codex.cmd shim, which sits in the
    # prefix folder right next to node_modules.
    if on_path:
        roots.append(Path(on_path).parent / "node_modules")
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "npm" / "node_modules")  # per-user default
    npm = shutil.which("npm")
    if npm:
        roots.append(Path(npm).parent / "node_modules")  # prefix = node folder
    return list(dict.fromkeys(roots))


def _npm_binaries(root: Path) -> List[Path]:
    """Where the native codex.exe lives inside a global npm install.

    The launcher (bin/codex.js) resolves the platform package
    ``@openai/codex-win32-<arch>`` - nested under the main package or hoisted
    next to it - and runs ``vendor/<triple>/bin/codex.exe`` from it. Older
    releases shipped the binary inside the main package instead.
    """
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", "")
    arch, triple = ("arm64", "aarch64-pc-windows-msvc") if machine == "ARM64" else ("x64", "x86_64-pc-windows-msvc")
    main = root / "@openai" / "codex"
    platform = f"codex-win32-{arch}"
    return [
        main / "node_modules" / "@openai" / platform / "vendor" / triple / "bin" / "codex.exe",
        root / "@openai" / platform / "vendor" / triple / "bin" / "codex.exe",
        main / "vendor" / triple / "bin" / "codex.exe",
        main / "vendor" / triple / "codex" / "codex.exe",
        main / "vendor" / triple / "codex.exe",
    ]


def _candidates() -> List[Path]:
    found: List[Path] = []

    on_path = shutil.which("codex")
    if on_path and Path(on_path).suffix.lower() == ".exe":
        found.append(Path(on_path))

    for root in _npm_roots(on_path):
        found.extend(_npm_binaries(root))

    # The Store/desktop app bundles the CLI under app/resources.
    store = _appx_install_location("OpenAI.Codex")
    if store:
        found.append(store / "app" / "resources" / "codex.exe")
        found.append(store / "resources" / "codex.exe")
        found.append(store / "codex.exe")
    return list(dict.fromkeys(found))


def find_codex(refresh: bool = False) -> Path:
    """Locate the codex CLI: PATH, then npm global, then inside the desktop app."""
    global _cached_path
    if _cached_path is not None and not refresh and _cached_path.is_file():
        return _cached_path

    rejected_gui = False
    for candidate in _candidates():
        if not candidate.is_file():
            continue
        if not is_console_executable(candidate):
            rejected_gui = True
            continue
        _cached_path = candidate
        return candidate

    if rejected_gui:
        raise CodexNotInstalled("Codex 데스크톱 앱만 있고 CLI를 찾지 못했습니다 · 앱을 업데이트해 주세요")
    raise CodexNotInstalled("Codex CLI를 찾을 수 없습니다 · Codex 앱 또는 npm i -g @openai/codex로 설치해 주세요")


def _appx_install_location(package: str) -> Optional[Path]:
    if os.name != "nt":
        return None
    powershell = (
        Path(os.environ.get("SystemRoot", "C:/Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    if not powershell.is_file():
        return None
    try:
        completed = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
                f"Get-AppxPackage -Name {package} | Select-Object -ExpandProperty InstallLocation",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (completed.stdout or "").splitlines():
        location = Path(line.strip())
        if line.strip() and location.is_absolute() and location.is_dir():
            return location
    return None


# --------------------------------------------------------------------------
# normalising the reply
# --------------------------------------------------------------------------
_WINDOW_LABELS = {300: "5시간", 10080: "주간", 1440: "일간"}


def _limit_groups(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the limit objects, each holding primary/secondary buckets.

    The CLI answers with ``rateLimitsByLimitId: {"<id>": {primary, secondary}}``
    - one level deeper than the flat ``rateLimits`` object it also sends. Read
    the keyed form when present so a future build with several limit ids still
    works, and fall back to the flat one.
    """
    keyed = result.get("rateLimitsByLimitId")
    if isinstance(keyed, dict) and keyed:
        groups = [value for _, value in sorted(keyed.items()) if isinstance(value, dict)]
        if groups:
            return groups
    flat = result.get("rateLimits")
    return [flat] if isinstance(flat, dict) else []


def _bucket_window(bucket: Any, prefix: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(bucket, dict):
        return None
    used = bucket.get("usedPercent")
    if not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used):
        return None
    minutes = bucket.get("windowDurationMins")
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
        label = _WINDOW_LABELS.get(int(minutes)) or f"{int(minutes)}분"
    else:
        label = "사용 한도"
    return {
        "label": f"{prefix} {label}".strip(),
        "used": max(0.0, min(100.0, float(used))),
        "resetText": _reset_text(bucket.get("resetsAt")),
    }


def normalize(result: Any) -> Dict[str, Any]:
    """Turn a rateLimits reply into our snapshot shape."""
    if not isinstance(result, dict):
        raise SnapshotError("Invalid Codex reply")

    groups = _limit_groups(result)
    windows: List[Dict[str, Any]] = []
    for group in groups:
        # Only label by limit id when more than one is in play, so the common
        # single-plan case stays "5시간" rather than "codex 5시간".
        prefix = ""
        if len(groups) > 1:
            name = group.get("limitName") or group.get("limitId")
            prefix = str(name) if isinstance(name, str) else ""
        before = len(windows)
        for slot in ("primary", "secondary"):
            window = _bucket_window(group.get(slot), prefix)
            if window:
                windows.append(window)
        if len(windows) == before:
            # Tolerate a build that keys buckets directly instead of wrapping
            # them in a limit object.
            window = _bucket_window(group, prefix)
            if window:
                windows.append(window)

    return {
        "provider": "codex",
        "windows": windows[:8],
        "note": "" if windows else "조회 가능한 사용 한도가 없습니다",
        "updatedAt": time.time(),
        "source": "cli",
        "error": None,
    }


def _reset_text(resets_at: Any) -> str:
    if not isinstance(resets_at, (int, float)) or isinstance(resets_at, bool):
        return ""
    if not math.isfinite(resets_at) or not (0 < resets_at < 4102444800):
        return ""
    return time.strftime("%m/%d %H:%M", time.localtime(resets_at))


# --------------------------------------------------------------------------
# the JSON-RPC conversation
# --------------------------------------------------------------------------
class _Channel:
    """Line-delimited JSON-RPC over the child process' stdio."""

    def __init__(self, process: subprocess.Popen, cancel: threading.Event) -> None:
        self.process = process
        self.cancel = cancel
        self.messages: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True, name="codex-reader")
        self._reader.start()

    def _pump(self) -> None:
        stream = self.process.stdout
        if stream is None:
            return
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                self.messages.put(message)

    def send(self, message: Dict[str, Any]) -> None:
        stream = self.process.stdin
        if stream is None:
            raise CodexError("Codex 연결이 종료됐습니다")
        stream.write(json.dumps(message) + "\n")
        stream.flush()

    def await_id(self, request_id: int, deadline: float) -> Dict[str, Any]:
        while True:
            if self.cancel.is_set():
                raise InterruptedError("closing")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexError("Codex 사용량 조회 시간이 초과됐습니다")
            try:
                message = self.messages.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise CodexError("Codex 연결이 종료됐습니다")
                continue
            if message.get("id") != request_id:
                self._unmatched(message)
                continue
            if "error" in message:
                error = message.get("error")
                error = error if isinstance(error, dict) else {}
                raise CodexRpcError(error.get("code"), error.get("message"))
            return message.get("result") or {}

    def await_method(self, method: str, deadline: float) -> Dict[str, Any]:
        while True:
            if self.cancel.is_set():
                raise InterruptedError("closing")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexError("Codex 로그인 시간이 초과됐습니다")
            try:
                message = self.messages.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise CodexError("Codex 연결이 종료됐습니다")
                continue
            if message.get("method") == method:
                return message.get("params") or {}
            self._unmatched(message)

    def _unmatched(self, message: Dict[str, Any]) -> None:
        self._last = message  # kept for debugging only


def read_codex(
    timeout: float = 180.0,
    cancel: Optional[threading.Event] = None,
    login_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Return a Codex snapshot, optionally driving an interactive login first."""
    cancel = cancel or threading.Event()
    if cancel.is_set():
        raise InterruptedError("closing")

    executable = find_codex()
    process = subprocess.Popen(
        [str(executable), "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        creationflags=NO_WINDOW,
    )
    deadline = time.monotonic() + timeout
    channel = _Channel(process, cancel)

    try:
        channel.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "ai_usage_board", "title": "AI Usage Board", "version": __version__}
                },
            }
        )
        channel.await_id(1, deadline)
        channel.send({"method": "initialized", "params": {}})

        if login_callback is not None:
            _login(channel, deadline, login_callback)

        channel.send({"id": 2, "method": "account/rateLimits/read"})
        try:
            return normalize(channel.await_id(2, deadline))
        except CodexRpcError:
            if not _signed_in(channel, deadline):
                raise CodexLoginRequired("Codex 로그인이 필요합니다") from None
            # Signed in, so it is not something a login would fix: leave it
            # as a plain failure the widget retries on its own.
            raise
    finally:
        _shutdown(process)


def _signed_in(channel: _Channel, deadline: float) -> bool:
    """Whether the CLI has an account, per its own ``account/read``.

    The reply is ``{"account": {...} | null, ...}``. A CLI too old to know the
    method answers with an error; that case keeps the earlier reading of
    "an error means sign in", which is what those builds needed.
    """
    channel.send({"id": 4, "method": "account/read", "params": {}})
    try:
        result = channel.await_id(4, deadline)
    except CodexRpcError:
        return False
    return isinstance(result.get("account"), dict)


def _login(channel: _Channel, deadline: float, login_callback: Callable[[str], None]) -> None:
    channel.send({"id": 3, "method": "account/login/start", "params": {"type": "chatgpt"}})
    started = channel.await_id(3, deadline)
    address = started.get("authUrl") or ""
    parsed = urlparse(address if isinstance(address, str) else "")
    if parsed.scheme != "https" or parsed.hostname not in LOGIN_HOSTS:
        raise CodexError("Codex 로그인 주소를 확인할 수 없습니다")
    login_callback(address)

    params = channel.await_method("account/login/completed", deadline)
    if not params.get("success"):
        raise CodexError("Codex 로그인이 완료되지 않았습니다")


def _shutdown(process: subprocess.Popen) -> None:
    """Stop the CLI, then let go of its pipes - in that order.

    stdout is being read by the channel's pump thread. Closing a buffered
    stream waits for the lock that a blocked read holds, so closing it while
    the CLI is still alive would hang until the CLI chose to write or exit.
    Ending the process first hands the reader its EOF; only then is it closed.
    """
    stdin, stdout = process.stdin, process.stdout
    if stdin is not None:
        # Nothing reads this side but the CLI, so it never blocks; EOF also
        # gives a well-behaved server the chance to leave on its own.
        with contextlib.suppress(OSError, ValueError):
            stdin.close()
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)
    if stdout is not None:
        with contextlib.suppress(OSError, ValueError):
            stdout.close()
