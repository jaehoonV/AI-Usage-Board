"""Small Win32 helpers: app identity, rounded corners and a tray icon.

Only our own window is created and styled. Nothing modifies other
applications' windows; the one thing read from outside is whether a taskbar
sits on top of the strip, by its class name and bounds.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

IS_WINDOWS = os.name == "nt"

# DwmSetWindowAttribute
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

# Shell_NotifyIcon
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
WM_APP_TRAY = 0x0400 + 17
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_DESTROY, WM_CLOSE = 0x0002, 0x0010
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
SM_CXSMICON = 49


def enable_dpi_awareness() -> None:
    """Opt into per-monitor DPI so Windows does not bitmap-stretch the widget.

    Must run before the first window is created. Falls back through the older
    APIs on Windows 8.1 / 7.
    """
    if not IS_WINDOWS:
        return
    try:  # Windows 10 1703+
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:  # noqa: BLE001
        pass
    try:  # Windows 8.1
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:  # noqa: BLE001
        pass
    with contextlib.suppress(Exception):
        ctypes.windll.user32.SetProcessDPIAware()


MONITOR_DEFAULTTONEAREST = 2


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


#: GetSystemMetrics indices for the bounding box of every monitor together.
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


def virtual_screen():
    """Bounds of the whole desktop, across every monitor, or None.

    Tk's ``winfo_screenwidth``/``winfo_screenheight`` describe the primary
    monitor alone, and the desktop does not start at (0, 0): a monitor placed
    higher than the primary one has negative coordinates. Clamping a window to
    ``max(0, ...)`` therefore stops it short of the top edge of such a monitor,
    by exactly the offset in the display arrangement.
    """
    if not IS_WINDOWS:
        return None
    try:
        user32 = ctypes.windll.user32
        left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if width <= 0 or height <= 0:
            return None
        return left, top, left + width, top + height
    except Exception:  # noqa: BLE001
        return None


def clamp_to(bounds, x: int, y: int, width: int, height: int) -> tuple:
    """Put a window of that size inside ``bounds`` (left, top, right, bottom).

    Separate from the Windows calls so the arithmetic can be tested against a
    real multi-monitor arrangement without one attached.
    """
    left, top, right, bottom = bounds
    return (
        int(max(left, min(x, right - width))),
        int(max(top, min(y, bottom - height))),
    )


def work_area_at(x: int, y: int):
    """Usable bounds of the monitor containing that point, or None."""
    if not IS_WINDOWS:
        return None
    try:
        user32 = ctypes.windll.user32
        point = wintypes.POINT(int(x), int(y))
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        monitor = user32.MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        work = info.rcWork
        return work.left, work.top, work.right, work.bottom
    except Exception:  # noqa: BLE001
        return None


def cursor_work_area():
    """Usable bounds of the monitor the mouse is on, or None.

    With several monitors the widget can sit perfectly visible on one the
    user is not looking at, which reads exactly like the app failing to
    start. Launching it again should put it where their attention already is.
    """
    if not IS_WINDOWS:
        return None
    try:
        user32 = ctypes.windll.user32
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        monitor = user32.MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        work = info.rcWork
        return work.left, work.top, work.right, work.bottom
    except Exception:  # noqa: BLE001
        return None


def window_rect(hwnd: int):
    """True screen bounds of a window, or None.

    Tk's winfo_x/y report the geometry Tk last applied, which is not where the
    window is if anything moved it behind Tk's back.
    """
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:  # noqa: BLE001
        return None


#: Window classes of the Windows taskbar: the primary one and the copies
#: shown on every other monitor.
TASKBAR_CLASSES = ("Shell_TrayWnd", "Shell_SecondaryTrayWnd")
GW_HWNDPREV = 3
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x0001, 0x0002, 0x0010, 0x0200


def covered_by_taskbar(hwnd: int) -> bool:
    """True when a taskbar is above ``hwnd`` in the z-order and overlaps it.

    The taskbar is a topmost window too, and Windows lifts it to the top of
    that band whenever it is clicked or hovered for a flyout. Anything else of
    ours parked on the bar then sits underneath it, invisible.
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        mine = window_rect(hwnd)
        if mine is None:
            return False
        name = ctypes.create_unicode_buffer(64)
        above = user32.GetWindow(wintypes.HWND(hwnd), GW_HWNDPREV)
        # Only the topmost band can be above a topmost window, and it is short.
        for _ in range(256):
            if not above:
                return False
            if user32.IsWindowVisible(above):
                user32.GetClassNameW(above, name, len(name))
                if name.value in TASKBAR_CLASSES:
                    rect = window_rect(above)
                    if rect and rect[0] < mine[2] and mine[0] < rect[2] and rect[1] < mine[3] and mine[1] < rect[3]:
                        return True
            above = user32.GetWindow(above, GW_HWNDPREV)
    except Exception:  # noqa: BLE001
        return False
    return False


def raise_topmost(hwnd: int) -> None:
    """Put ``hwnd`` back at the top of the topmost band without focusing it."""
    if not IS_WINDOWS or not hwnd:
        return
    with contextlib.suppress(Exception):
        ctypes.windll.user32.SetWindowPos(
            wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
        )


def set_app_user_model_id(app_id: str) -> None:
    if not IS_WINDOWS:
        return
    with contextlib.suppress(Exception):
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)


def round_corners(hwnd: int) -> None:
    """Windows 11 rounded corners for a frameless window. No-op elsewhere."""
    if not IS_WINDOWS or not hwnd:
        return
    with contextlib.suppress(Exception):
        preference = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(preference),
            ctypes.sizeof(preference),
        )


def drop_shadow(hwnd: int) -> None:
    """Give a borderless window the standard system shadow."""
    if not IS_WINDOWS or not hwnd:
        return
    with contextlib.suppress(Exception):
        class MARGINS(ctypes.Structure):
            _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int),
                        ("top", ctypes.c_int), ("bottom", ctypes.c_int)]

        margins = MARGINS(0, 0, 1, 0)
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(margins))


class TrayIcon:
    """A notification-area icon with a small context menu.

    Runs its own hidden window and message loop on a daemon thread so it never
    blocks Tk. Callbacks are invoked on that thread; marshal to Tk with
    ``after``.
    """

    def __init__(
        self,
        tooltip: str,
        icon_path: Optional[Path],
        on_activate: Callable[[], None],
        menu_items: Optional[list] = None,
    ) -> None:
        self.tooltip = tooltip[:127]
        self.icon_path = icon_path
        self.on_activate = on_activate
        self.menu_items = menu_items or []
        self._hwnd = 0
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._nid = None
        self._proc_ref = None

    # -- public -----------------------------------------------------------
    def start(self) -> bool:
        if not IS_WINDOWS:
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name="ai_usage_board-tray")
        self._thread.start()
        return self._ready.wait(3.0)

    def set_tooltip(self, text: str) -> None:
        """Change the hover text. The widget calls this every second with
        mostly the same figures, so an unchanged text is not sent to the
        shell again."""
        text = text[:127]
        if text == self.tooltip:
            return
        self.tooltip = text
        if self._nid is None or not self._hwnd:
            return
        self._nid.szTip = self.tooltip
        with contextlib.suppress(Exception):
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def stop(self) -> None:
        if self._hwnd:
            with contextlib.suppress(Exception):
                ctypes.windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    # -- internals ---------------------------------------------------------
    def _run(self) -> None:
        """Thread body. A failure here used to kill the thread silently and
        leave start() waiting out its full timeout; now it is logged and
        start() is released at once."""
        try:
            self._serve()
        except Exception:  # noqa: BLE001 - a missing tray must not take the widget down
            from ..config import log_exception

            log_exception("tray icon")
        finally:
            self._ready.set()

    def _serve(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("proc", WNDPROC),
                ("clsExtra", ctypes.c_int),
                ("wndExtra", ctypes.c_int),
                ("instance", wintypes.HINSTANCE),
                ("icon", wintypes.HICON),
                ("cursor", wintypes.HANDLE),
                ("background", wintypes.HBRUSH),
                ("menuName", wintypes.LPCWSTR),
                ("className", wintypes.LPCWSTR),
            ]

        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
            ]

        # Without explicit signatures ctypes truncates 64-bit HWND/LPARAM
        # values and every message raises inside the callback.
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadIconW.restype = wintypes.HICON
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.TrackPopupMenu.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        # The module handle is a 64-bit address. ctypes' default int return
        # kept only its low 32 bits; whenever those came out negative, the
        # value read back from WNDCLASS no longer fitted CreateWindowExW's
        # default int argument, the call raised, and the tray never appeared.
        # Where the image loads decides that, so it failed on some launches only.
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]

        #: Broadcast to every top-level window when Explorer starts again
        #: after a crash or an update. The new taskbar knows no icons, so each
        #: program has to add its own back - otherwise ours is simply gone,
        #: and with the window sent to the tray there is no way back to it.
        taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

        def window_proc(hwnd, message, wparam, lparam):
            if taskbar_created and message == taskbar_created:
                self._add_icon()
                return 0
            if message == WM_APP_TRAY:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONUP:
                    with contextlib.suppress(Exception):
                        self.on_activate()
                elif event == WM_RBUTTONUP:
                    self._show_menu(hwnd)
                return 0
            if message == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._proc_ref = WNDPROC(window_proc)  # keep alive
        klass = WNDCLASS()
        klass.proc = self._proc_ref
        klass.instance = kernel32.GetModuleHandleW(None)
        klass.className = "AIUsageBoardTray"
        if not user32.RegisterClassW(ctypes.byref(klass)):
            self._ready.set()
            return

        self._hwnd = user32.CreateWindowExW(
            0, "AIUsageBoardTray", "AI Usage Board", 0, 0, 0, 0, 0, None, None, klass.instance, None
        ) or 0
        if not self._hwnd:
            raise OSError(ctypes.get_last_error() or "CreateWindowExW failed")

        hicon = 0
        if self.icon_path and Path(self.icon_path).is_file():
            # The tray draws small icons. LR_DEFAULTSIZE picks the 32px
            # image and lets the shell shrink it; asking for the small-icon
            # size (DPI-scaled, since the process is DPI aware) picks the
            # image drawn for it.
            small = user32.GetSystemMetrics(SM_CXSMICON) or 16
            hicon = user32.LoadImageW(
                None, str(self.icon_path), IMAGE_ICON, small, small, LR_LOADFROMFILE
            )
        if not hicon:
            hicon = user32.LoadIconW(None, ctypes.c_wchar_p(32512))  # IDI_APPLICATION

        self._nid = NOTIFYICONDATA()
        self._nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        self._nid.hWnd = self._hwnd
        self._nid.uID = 1
        self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self._nid.uCallbackMessage = WM_APP_TRAY
        self._nid.hIcon = hicon
        self._nid.szTip = self.tooltip
        self._add_icon()
        self._ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

        with contextlib.suppress(Exception):
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        self._hwnd = 0

    def _add_icon(self) -> None:
        """Put the icon in the notification area, with the current tooltip."""
        if self._nid is None:
            return
        self._nid.szTip = self.tooltip
        with contextlib.suppress(Exception):
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid))

    def _show_menu(self, hwnd: int) -> None:
        if not self.menu_items:
            with contextlib.suppress(Exception):
                self.on_activate()
            return
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        for index, (label, _callback) in enumerate(self.menu_items, start=1):
            if label == "-":
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(menu, MF_STRING, index, label)

        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(hwnd)
        choice = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, hwnd, None
        )
        user32.DestroyMenu(menu)
        if 1 <= choice <= len(self.menu_items):
            with contextlib.suppress(Exception):
                self.menu_items[choice - 1][1]()
