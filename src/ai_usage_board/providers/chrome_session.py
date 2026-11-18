"""A dedicated Chrome/Edge profile driven over loopback DevTools.

Design rules enforced here:

* The profile lives under our own data directory, so it never shares cookies
  with the user's everyday browser.
* The user performs the login themselves in a real browser window. We never
  read cookies, never touch login forms and never store credentials.
* DevTools listens on an ephemeral port bound to 127.0.0.1 only, and every
  endpoint we connect to is re-validated before use.
* Only the one usage URL is ever scripted.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

from .ws import WebSocket, WebSocketError

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ALLOWED_PATH_PREFIXES = ("/devtools/page/", "/devtools/browser/")


class BrowserError(RuntimeError):
    pass


def find_browser() -> Path:
    """Prefer Chrome, fall back to Edge. Both speak the same protocol."""
    program_files = os.environ.get("PROGRAMFILES", "C:/Program Files")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(program_files) / "Google/Chrome/Application/chrome.exe",
        Path(program_files_x86) / "Google/Chrome/Application/chrome.exe",
        Path(local) / "Google/Chrome/Application/chrome.exe" if local else None,
        Path(program_files_x86) / "Microsoft/Edge/Application/msedge.exe",
        Path(program_files) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise BrowserError("BrowserNotInstalled")


def browser_command(
    executable: Path, profile: Path, landing: str, *, minimized: bool, debug: bool = True
) -> List[str]:
    """Build the launch line.

    With ``debug=False`` no DevTools port is opened at all, so the browser is
    an ordinary one in every observable way. That is what the sign-in phase
    uses: nothing is driving it, so nothing can decide it is being driven.
    """
    disabled_features = ["Translate", "MediaRouter"]
    command = [
        str(executable),
        f"--user-data-dir={profile.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]
    if debug:
        # The reading browser lives hidden, and Chrome puts a page that has
        # been hidden for a few minutes on a one-wake-a-minute budget. Every
        # DevTools call then waits up to a minute for its turn, far past our
        # socket timeout, so reads fail and the card shows "사용량 화면 확인
        # 필요". Keep the renderer awake. Only the reader gets this: the
        # sign-in window is on screen and should behave like any browser.
        command += [
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ]
        disabled_features.append("IntensiveWakeUpThrottling")
    # One flag: Chrome honours only the last --disable-features it is given.
    command.append("--disable-features=" + ",".join(disabled_features))
    # Deliberately NOT passing --disable-blink-features=AutomationControlled.
    # It would not help: navigator.webdriver is already false with only a
    # debugging port. It costs two things instead - Chrome shows the user an
    # "unsupported command-line flag" warning bar, and the flag is itself a
    # well-known evasion marker that bot checks look for. Both work against a
    # sign-in window whose whole point is to look exactly like any other.
    if debug:
        command += ["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"]
    if minimized:
        command.append("--start-minimized")
    command.extend(["--new-window", landing])
    return command


def validate_socket(address: str, port: int) -> str:
    parsed = urlparse(address)
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != port
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(ALLOWED_PATH_PREFIXES)
    ):
        raise BrowserError("InvalidBrowserEndpoint")
    return address


class BrowserSession:
    """Owns one browser process and its DevTools connection."""

    def __init__(self, profile: Path) -> None:
        self.profile = Path(profile)
        self.process: Optional[subprocess.Popen] = None
        self.port = 0
        self.socket_url = ""
        #: False while the sign-in browser is running without a DevTools port.
        self.debug = True
        #: True once the reading browser has been taken off the screen.
        self.hidden = False
        # Never let a system proxy intercept a loopback DevTools call.
        self.http = build_opener(ProxyHandler({}))

    # -- lifecycle --------------------------------------------------------
    def _mark_clean_exit(self) -> None:
        """Tell Chrome the last run ended normally.

        Otherwise it greets us with "Restore pages?", which both nags the user
        and pops the window back out of the taskbar - defeating a minimised
        reading browser. Cheaper and less invasive than a command-line flag.
        """
        preferences = self.profile / "Default" / "Preferences"
        try:
            data = json.loads(preferences.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        profile_section = data.get("profile")
        if not isinstance(profile_section, dict):
            return
        profile_section["exit_type"] = "Normal"
        profile_section["exited_cleanly"] = True
        with contextlib.suppress(OSError):
            preferences.write_text(json.dumps(data), encoding="utf-8")

    SW_HIDE, SW_SHOW, SW_RESTORE = 0, 5, 9

    def _apply_window_state(self, action: int) -> int:
        """ShowWindow(action) on every browser window of this process."""
        if os.name != "nt" or self.process is None or self.process.poll() is not None:
            return 0
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        target = self.process.pid
        touched = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _parameter):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != target:
                return True
            name = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, name, 64)
            # Only real browser frames. Chrome's helper windows stay as they
            # are, so showing never pops a stray popup into view.
            if name.value == "Chrome_WidgetWin_1":
                user32.ShowWindow(hwnd, action)
                touched.append(hwnd)
            return True

        with contextlib.suppress(Exception):
            user32.EnumWindows(visit, 0)
        return len(touched)

    def hide(self, timeout: float = 10.0) -> None:
        """Take the browser off the screen entirely - taskbar included.

        The reading browser is machinery; the user asked for a usage widget,
        not a second browser. SW_HIDE removes the window completely while the
        process keeps running, which is what CDP needs.
        """
        self.hidden = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._apply_window_state(self.SW_HIDE):
                return
            time.sleep(0.3)

    def keep_hidden(self) -> None:
        """Re-hide windows Chrome creates or re-shows while we are not looking."""
        if self.hidden:
            self._apply_window_state(self.SW_HIDE)

    def reveal(self) -> None:
        self.hidden = False
        self._apply_window_state(self.SW_SHOW)
        self._apply_window_state(self.SW_RESTORE)

    def start(self, *, minimized: bool, landing: str, debug: bool = True) -> None:
        self.profile.mkdir(parents=True, exist_ok=True)
        port_file = self.profile / "DevToolsActivePort"
        with contextlib.suppress(OSError):
            port_file.unlink()
        self._mark_clean_exit()

        self.debug = debug
        self.port = 0
        self.socket_url = ""

        startup_info = None
        if os.name == "nt":
            startup_info = subprocess.STARTUPINFO()
            startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup_info.wShowWindow = 7 if minimized else 1  # SW_SHOWMINNOACTIVE / SW_NORMAL

        self.process = subprocess.Popen(
            browser_command(find_browser(), self.profile, landing, minimized=minimized, debug=debug),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startup_info,
            creationflags=NO_WINDOW,
        )

        if not debug:
            # Nothing to wait for: no port is published. Just confirm it lives.
            time.sleep(1.0)
            if self.process.poll() is not None:
                raise BrowserError("BrowserExited")
            return

        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise BrowserError("BrowserExited")
            try:
                lines = port_file.read_text(encoding="utf-8").splitlines()
                port = int(lines[0])
                path = lines[1] if len(lines) > 1 else ""
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
                continue
            if not 1 <= port <= 65535:
                raise BrowserError("InvalidPort")
            self.port = port
            self.socket_url = validate_socket(f"ws://127.0.0.1:{port}{path}", port)
            return
        raise BrowserError("BrowserStartTimeout")

    def close(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        # Always ask politely first. A clean exit is what flushes cookies to
        # the profile, and the sign-in they just completed lives in those
        # cookies - killing the process here would throw the login away.
        if self.debug:
            with contextlib.suppress(Exception):
                self.call("Browser.close")
        else:
            self._close_windows()
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=3)
        self.port = 0
        self.socket_url = ""

    def _close_windows(self) -> None:
        """Close a port-less browser the way the user would: WM_CLOSE."""
        if os.name != "nt" or self.process is None:
            return
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        target = self.process.pid

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _parameter):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == target and user32.IsWindowVisible(hwnd):
                name = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, name, 64)
                if name.value == "Chrome_WidgetWin_1":
                    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            return True

        with contextlib.suppress(Exception):
            user32.EnumWindows(visit, 0)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    # -- protocol ---------------------------------------------------------
    def pages(self) -> List[Dict[str, Any]]:
        if not self.debug:
            raise BrowserError("BrowserNotInspectable")
        if not self.port:
            raise BrowserError("BrowserNotReady")
        with self.http.open(f"http://127.0.0.1:{self.port}/json/list", timeout=3) as response:
            payload = json.loads(response.read(1 << 20))
        return [p for p in payload if isinstance(p, dict) and p.get("type") == "page"]

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, page: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        address = validate_socket(
            (page or {}).get("webSocketDebuggerUrl") or self.socket_url, self.port
        )
        # No Origin header is sent, which is what Chrome wants from a
        # non-browser DevTools client.
        connection: Optional[WebSocket] = None
        try:
            connection = WebSocket(address, timeout=5)
            connection.send_json({"id": 1, "method": method, "params": params or {}})
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                reply = connection.recv_json()
                if reply.get("id") != 1:
                    continue  # an event arrived first
                if "error" in reply:
                    raise BrowserError("BrowserCommandFailed")
                return reply.get("result") or {}
            raise BrowserError("BrowserReplyTimeout")
        except WebSocketError as error:
            raise BrowserError(f"BrowserSocket:{error}") from error
        except (TimeoutError, socket.timeout) as error:
            # A slow page surfaces as a bare socket timeout (an OSError, not a
            # WebSocketError). Name it, so the state report says what it was.
            raise BrowserError("BrowserReplyTimeout") from error
        except OSError as error:
            raise BrowserError(f"BrowserSocket:{type(error).__name__}") from error
        finally:
            if connection is not None:
                with contextlib.suppress(Exception):
                    connection.close()

    # -- window handling --------------------------------------------------
    def bring_to_front(self) -> None:
        """Raise the browser window through Win32, never through DevTools.

        Showing the window is the one thing we do while a bot check is on
        screen, so it must not attach a debugger to that page - that is what
        makes the check repeat forever. All windows of this process are ours
        anyway, because the profile is ours.
        """
        if os.name != "nt" or self.process is None or self.process.poll() is not None:
            return
        import ctypes
        from ctypes import wintypes

        self.reveal()  # a hidden window cannot be brought forward
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        target = self.process.pid

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _parameter):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != target or not user32.IsWindowVisible(hwnd):
                return True
            name = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, name, 64)
            if name.value != "Chrome_WidgetWin_1":
                return True
            user32.SetForegroundWindow(hwnd)
            return False

        with contextlib.suppress(Exception):
            user32.EnumWindows(visit, 0)
