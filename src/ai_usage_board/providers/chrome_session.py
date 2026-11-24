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
import functools
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
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


Rect = Tuple[int, int, int, int]

#: How far past the desktop's left edge a parked reader window sits.
PARK_MARGIN = 200


def is_browser_frame(style: int) -> bool:
    """A top-level browser frame, as opposed to a menu, tooltip or dropdown."""
    caption = BrowserSession.WS_CAPTION
    return not style & BrowserSession.WS_POPUP and style & caption == caption


def overlaps(rect: Rect, area: Rect) -> bool:
    """True if any part of ``rect`` lies inside ``area`` (left, top, right, bottom)."""
    return rect[0] < area[2] and rect[2] > area[0] and rect[1] < area[3] and rect[3] > area[1]


def parking_spot(desktop: Rect, width: int) -> Tuple[int, int]:
    """Top-left corner that puts a ``width``-wide window wholly left of the desktop.

    The desktop is every monitor together and need not start at (0, 0): a
    monitor left of the primary one has negative x.
    """
    return desktop[0] - width - PARK_MARGIN, desktop[1]


def _virtual_screen() -> Rect:
    import ctypes

    metric = ctypes.windll.user32.GetSystemMetrics
    left, top = metric(76), metric(77)  # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN
    return left, top, left + metric(78), top + metric(79)  # SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN


def _default_home(width: int, height: int) -> Rect:
    """Somewhere sensible on the primary monitor's work area.

    In workspace coordinates - relative to the work area, which is what a
    window placement uses for a window that is not a tool window.
    """
    import ctypes
    from ctypes import wintypes

    area = wintypes.RECT(0, 0, 1280, 800)
    with contextlib.suppress(Exception):
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)  # SPI_GETWORKAREA
    width = max(400, min(width, area.right - area.left - 80))
    height = max(300, min(height, area.bottom - area.top - 60))
    return 80, 40, 80 + width, 40 + height


if os.name == "nt":
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    class _Placement(_ctypes.Structure):
        _fields_ = [
            ("length", _wintypes.UINT),
            ("flags", _wintypes.UINT),
            ("showCmd", _wintypes.UINT),
            ("ptMinPosition", _wintypes.POINT),
            ("ptMaxPosition", _wintypes.POINT),
            ("rcNormalPosition", _wintypes.RECT),
        ]


class BrowserSession:
    """Owns one browser process and its DevTools connection."""

    def __init__(self, profile: Path, *, park_visible: bool = True) -> None:
        self.profile = Path(profile)
        #: Keep the reader *shown* beyond the desktop's edge (see "keeping the
        #: reader out of sight") rather than hidden with SW_HIDE. A shown page
        #: keeps painting, so this is for sites whose bot check needs it.
        self.park_visible = park_visible
        self.process: Optional[subprocess.Popen] = None
        self.port = 0
        self.socket_url = ""
        #: False while the sign-in browser is running without a DevTools port.
        self.debug = True
        #: True once the reading browser has been taken off the screen.
        self.hidden = False
        #: Where each parked window stood before, to put it back on "창 열기".
        self._home: Dict[int, Rect] = {}
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

    # -- keeping the reader out of sight ----------------------------------
    #
    # The reading browser is machinery; the user asked for a usage widget,
    # not a second browser. It used to be hidden with SW_HIDE (and the user
    # sometimes minimised it). Both make Chrome mark the page hidden, and a
    # hidden page cannot get through a Cloudflare check: Turnstile never
    # finishes, retries every two minutes and every retry counts against the
    # profile, until the site stops trusting it at all - the "보안 확인이 계속
    # 반복" loop. A history of one such profile showed exactly that: one
    # check, then five hours of retries two minutes apart.
    #
    # So the window stays *shown*, but parked beyond the edge of the desktop
    # and dropped from the taskbar and Alt+Tab (WS_EX_TOOLWINDOW). To the
    # page it is an ordinary visible tab - --disable-backgrounding-occluded-
    # windows keeps Chrome from treating an unseen window as hidden - so a
    # check clears on its own, the way it does in any browser left open.

    SW_HIDE, SW_SHOWNORMAL, SW_SHOWNOACTIVATE = 0, 1, 4
    GWL_STYLE, GWL_EXSTYLE = -16, -20
    WS_POPUP, WS_CAPTION = 0x80000000, 0x00C00000
    WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def _user32():
        # Built once: keep_hidden() runs every 0.4 seconds.
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # Chrome measures its windows in physical pixels (per-monitor aware).
        # Unaware, this helper would see scaled coordinates, and on mixed-DPI
        # monitors "is it parked?" could come out wrong every time - a window
        # moved again and again. The helper has no UI of its own to blur.
        with contextlib.suppress(Exception):
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        long_ptr = ctypes.c_ssize_t
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        get_long.argtypes, get_long.restype = [wintypes.HWND, ctypes.c_int], long_ptr
        set_long.argtypes, set_long.restype = [wintypes.HWND, ctypes.c_int, long_ptr], long_ptr
        user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_Placement)]
        user32.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_Placement)]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.GetWindow.argtypes, user32.GetWindow.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        return user32, get_long, set_long

    def _frames(self) -> List[int]:
        """The browser's top-level frames: captioned, unowned, not popups.

        Menus, tooltips and the omnibox dropdown share the window class but
        are popups; showing one of those would put a stray box on screen.
        """
        if os.name != "nt" or self.process is None or self.process.poll() is not None:
            return []
        import ctypes
        from ctypes import wintypes

        user32, get_long, _set_long = self._user32()
        target = self.process.pid
        found: List[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _parameter):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != target:
                return True
            name = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, name, 64)
            if name.value != "Chrome_WidgetWin_1" or user32.GetWindow(hwnd, 4):  # GW_OWNER
                return True
            style = get_long(hwnd, self.GWL_STYLE) & 0xFFFFFFFF
            if is_browser_frame(style):
                found.append(hwnd)
            return True

        with contextlib.suppress(Exception):
            user32.EnumWindows(visit, 0)
        return found

    def _tucked(self, hwnd: int) -> bool:
        import ctypes
        from ctypes import wintypes

        user32, get_long, _set_long = self._user32()
        if not self.park_visible:
            return not user32.IsWindowVisible(hwnd)
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return False
        if not get_long(hwnd, self.GWL_EXSTYLE) & self.WS_EX_TOOLWINDOW:
            return False
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return not overlaps((rect.left, rect.top, rect.right, rect.bottom), _virtual_screen())

    #: SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
    _RESTYLED = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020

    def _tuck(self, hwnd: int) -> None:
        """Shown, unminimised, beyond the desktop's edge, off the taskbar."""
        import ctypes

        user32, get_long, set_long = self._user32()
        if not self.park_visible:
            user32.ShowWindow(hwnd, self.SW_HIDE)
            return
        placement = _Placement()
        placement.length = ctypes.sizeof(_Placement)
        if not user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
            return
        normal = placement.rcNormalPosition
        bounds = (normal.left, normal.top, normal.right, normal.bottom)
        if hwnd not in self._home and overlaps(bounds, _virtual_screen()):
            self._home[hwnd] = bounds
        width, height = max(400, bounds[2] - bounds[0]), max(300, bounds[3] - bounds[1])

        extended = get_long(hwnd, self.GWL_EXSTYLE) & 0xFFFFFFFF
        wanted = (extended | self.WS_EX_TOOLWINDOW) & ~self.WS_EX_APPWINDOW
        if extended != wanted:
            # The taskbar only notices a style change across hide and show.
            user32.ShowWindow(hwnd, self.SW_HIDE)
            set_long(hwnd, self.GWL_EXSTYLE, wanted)
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, self._RESTYLED)

        left, top = parking_spot(_virtual_screen(), width)
        placement.flags = 0
        placement.showCmd = self.SW_SHOWNOACTIVATE  # also lifts a minimised window
        placement.rcNormalPosition.left, placement.rcNormalPosition.top = left, top
        placement.rcNormalPosition.right, placement.rcNormalPosition.bottom = left + width, top + height
        user32.SetWindowPlacement(hwnd, ctypes.byref(placement))

    def _bring_home(self, hwnd: int, show: int) -> None:
        """Undo :meth:`_tuck`: back on the desktop and the taskbar."""
        import ctypes

        user32, get_long, set_long = self._user32()
        if not self.park_visible:
            user32.ShowWindow(hwnd, show)  # a hidden window kept its place
            return
        placement = _Placement()
        placement.length = ctypes.sizeof(_Placement)
        if not user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
            return
        normal = placement.rcNormalPosition
        width, height = normal.right - normal.left, normal.bottom - normal.top
        home = self._home.get(hwnd)
        if home is None or not overlaps(home, _virtual_screen()):
            # Never remembered, or its monitor has since been unplugged:
            # "창 열기" must put the window somewhere the user can see it.
            home = _default_home(width, height)

        extended = get_long(hwnd, self.GWL_EXSTYLE) & 0xFFFFFFFF
        if extended & self.WS_EX_TOOLWINDOW:
            user32.ShowWindow(hwnd, self.SW_HIDE)
            set_long(hwnd, self.GWL_EXSTYLE, extended & ~self.WS_EX_TOOLWINDOW)
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, self._RESTYLED)
        placement.flags = 0
        placement.showCmd = show
        (placement.rcNormalPosition.left, placement.rcNormalPosition.top,
         placement.rcNormalPosition.right, placement.rcNormalPosition.bottom) = home
        user32.SetWindowPlacement(hwnd, ctypes.byref(placement))

    def hide(self, timeout: float = 10.0) -> None:
        """Put the reading browser out of sight (see the note above)."""
        self.hidden = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frames = self._frames()
            if frames:
                for hwnd in frames:
                    with contextlib.suppress(Exception):
                        self._tuck(hwnd)
                return
            time.sleep(0.3)

    def keep_hidden(self) -> None:
        """Re-park windows Chrome creates, restores or moves back on screen."""
        if not self.hidden:
            return
        for hwnd in self._frames():
            with contextlib.suppress(Exception):
                if not self._tucked(hwnd):
                    self._tuck(hwnd)

    def reveal(self) -> None:
        self.hidden = False
        for hwnd in self._frames():
            with contextlib.suppress(Exception):
                self._bring_home(hwnd, self.SW_SHOWNORMAL)

    def _send_home_quietly(self) -> None:
        """Before closing: leave a sensible window position in the profile.

        Chrome saves where its window was. The next time this profile opens
        it is as the sign-in window, which should not start out past the
        edge of the desktop.
        """
        if not self.hidden or not self.park_visible:
            return
        for hwnd in self._frames():
            with contextlib.suppress(Exception):
                self._bring_home(hwnd, self.SW_HIDE)

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

        try:
            self._await_ready(port_file, debug)
        except BrowserError:
            # The caller never gets a session for a browser that failed to
            # start, so nothing else would ever close it. Left running, it
            # sits there with an open DevTools port and no owner.
            self._abandon()
            raise

    def _abandon(self) -> None:
        """Stop a browser that never became usable. No goodbye: there is
        no DevTools connection to say it over."""
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)

    def _await_ready(self, port_file: Path, debug: bool) -> None:
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
        with contextlib.suppress(Exception):
            self._send_home_quietly()
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
        with self._conversation(page) as ask:
            return ask(method, params)

    def evaluate_isolated(self, expression: str, page: Dict[str, Any]) -> Dict[str, Any]:
        """Run ``expression`` in a world of its own on ``page``'s main frame.

        The DOM is shared, the JavaScript is not: the page cannot see our
        code, the globals it leaves behind, or our calls through DOM methods
        it may have wrapped. Run in the page's own world, the reader used to
        leave ``window.AIUsageBoardReader`` there every 15 seconds - exactly
        the kind of stray global a bot check looks for.
        """
        with self._conversation(page) as ask:
            tree = ask("Page.getFrameTree")
            frame_id = ((tree.get("frameTree") or {}).get("frame") or {}).get("id")
            if not isinstance(frame_id, str) or not frame_id:
                raise BrowserError("FrameNotFound")
            world = ask("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "reader"})
            context = world.get("executionContextId")
            if not isinstance(context, int):
                raise BrowserError("IsolatedWorldFailed")
            return ask(
                "Runtime.evaluate",
                {"expression": expression, "contextId": context, "returnByValue": True, "awaitPromise": False},
            )

    @contextlib.contextmanager
    def _conversation(self, page: Optional[Dict[str, Any]]) -> Iterator[Callable[..., Dict[str, Any]]]:
        """One short DevTools connection carrying one or more commands."""
        address = validate_socket(
            (page or {}).get("webSocketDebuggerUrl") or self.socket_url, self.port
        )
        # No Origin header is sent, which is what Chrome wants from a
        # non-browser DevTools client.
        connection: Optional[WebSocket] = None
        counter = [0]

        def ask(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            counter[0] += 1
            message_id = counter[0]
            connection.send_json({"id": message_id, "method": method, "params": params or {}})
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                reply = connection.recv_json()
                if reply.get("id") != message_id:
                    continue  # an event arrived first
                if "error" in reply:
                    raise BrowserError("BrowserCommandFailed")
                return reply.get("result") or {}
            raise BrowserError("BrowserReplyTimeout")

        try:
            connection = WebSocket(address, timeout=5)
            yield ask
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
