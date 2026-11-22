"""The desktop widget.

Layout, top to bottom: a draggable header, one card per service, a footer with
the refresh action. Everything is flat, hairline-bordered and neutral; the only
colour is one 8px dot per service and the meter fill, which turns amber then
red as a quota runs down.
"""
from __future__ import annotations

import contextlib
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from typing import Any, Dict, List, Optional

from .. import APP_ID, APP_NAME, __version__, autostart
from ..config import CONTACT_FILE, GUIDE_FILE, RESOURCE_DIR, load_settings, log_exception, save_settings
from ..core.models import LOCAL_SERVICES, SERVICE_ORDER, SERVICES, order_services, validate_snapshot
from ..providers.antigravity import (
    AntigravityError,
    AntigravityLoginRequired,
    AntigravityNotRunning,
    launch_and_read,
    read_antigravity,
)
from ..providers.codex import CodexError, CodexLoginRequired, CodexNotInstalled, read_codex
from ..theme import (
    CARD_GAP,
    CARD_PAD_Y,
    META_GAP,
    METER_GAP,
    ROW_GAP,
    SERVICE_ACCENTS,
    SPACE_1,
    SPACE_2,
    SPACE_3,
    SPACE_4,
    TYPE,
    WINDOW_WIDTH,
    level_color,
    get_scale,
    px,
    resolve,
    set_scale,
)
from .strip import TaskbarStrip, figure_color
from .components import (
    GLYPH_FEEDBACK,
    GLYPH_INFO,
    GLYPH_POWER,
    GLYPH_REFRESH,
    GLYPH_SETTINGS,
    Card,
    Dot,
    GhostButton,
    Hairline,
    Meter,
    Popover,
    glyph,
    icon_font,
    relative_time,
)
from .win32 import (
    TrayIcon,
    clamp_to,
    cursor_work_area,
    virtual_screen,
    work_area_at,
    drop_shadow,
    enable_dpi_awareness,
    restore_if_minimized,
    round_corners,
    set_app_user_model_id,
)

#: The theme menu, in the order shown. "auto" follows Windows' app theme.
THEME_CHOICES = (("auto", "자동 (Windows 설정)"), ("light", "라이트"), ("dark", "다크"))

CODEX_INTERVAL_MS = 15_000
#: Antigravity's quota moves in five-hour and weekly steps, and each read may
#: cost a PowerShell process to find the server again. A minute is plenty.
ANTIGRAVITY_INTERVAL_MS = 60_000
#: With the app closed every reading costs starting its language server, which
#: is a 200MB binary. Worth about three seconds on demand, not every minute.
ANTIGRAVITY_LAUNCH_INTERVAL_MS = 10 * 60_000
TICK_MS = 1_000
STALE_AFTER = 600  # seconds before data is called out as old


class Widget:
    def __init__(self, store, accounts) -> None:
        self.store = store
        self.accounts = accounts
        self.settings = load_settings()
        self.palette = resolve(self.settings.get("theme", "auto"))

        self.closing = threading.Event()
        self.busy = False
        self.codex_thread: Optional[threading.Thread] = None
        self.codex_timer = None
        self.login_urls: "queue.Queue[str]" = queue.Queue()
        self.codex_login_requested = False
        #: Set once the CLI says the account is not signed in, so the card
        #: offers 계정 연결 instead of a retry that would fail identically.
        self.codex_needs_login = False
        #: True while Codex failed in a way only the user can fix; the timer
        #: then stops and only an explicit retry queries again.
        self.codex_paused = False
        #: Antigravity keeps its own loop. It has no login handshake and a
        #: much slower cadence, so folding it into the Codex timer would mean
        #: one of the two polling at the wrong rate.
        self.agy_thread: Optional[threading.Thread] = None
        self.agy_timer = None
        self.agy_busy = False
        self.agy_paused = False
        #: Slows down by itself while the readings have to start their own
        #: server; back to the quick cadence as soon as the app is up.
        self.agy_interval = ANTIGRAVITY_INTERVAL_MS
        self.agy_last = 0.0
        self.rendered: Dict[str, str] = {}
        self.cards: Dict[str, Card] = {}
        self.rows: Dict[str, tk.Frame] = {}
        self.meters: Dict[str, Dict[str, Meter]] = {}
        self.order: List[str] = list(SERVICE_ORDER)
        self._fitted: Optional[tuple] = None
        self.tray: Optional[TrayIcon] = None
        self.strip: Optional[TaskbarStrip] = None
        self.popover: Optional[Popover] = None
        #: Shown in place of the summary until the requested refresh lands.
        self.refresh_pending = False
        self.summary_text = ""
        #: Per-service figures behind the footer summary, as the strip gets them.
        self.summary_items: List[Dict[str, Any]] = []
        self._summary_signature: Optional[str] = None
        #: Last fault logged from the tick, so a repeating one is written once.
        self._last_tick_error = ""

        set_app_user_model_id(APP_ID)
        enable_dpi_awareness()

        self.root = tk.Tk()
        self.root.withdraw()
        # Tk reports device pixels once the process is DPI aware; derive the
        # scaling factor from the display and let fonts follow it.
        set_scale(max(1.0, self.root.winfo_fpixels("1i") / 96.0))
        self.scale = get_scale()
        # Point-sized fonts follow the same factor, so type shrinks with the
        # rest of the interface instead of staying stubbornly large.
        with contextlib.suppress(Exception):
            self.root.tk.call("tk", "scaling", self.scale * 96.0 / 72.0)
        self.root.title(APP_NAME)
        self.root.configure(bg=self.palette.canvas)
        self.root.overrideredirect(True)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", bool(self.settings.get("pinned", True)))
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        icon = RESOURCE_DIR / "app.ico"
        if icon.is_file():
            with contextlib.suppress(Exception):
                self.root.iconbitmap(str(icon))

        self._build()
        self.root.bind("<Configure>", self._follow_popover, add="+")
        self._place_window()
        self.root.deiconify()
        self.root.update_idletasks()
        self._apply_window_chrome()
        self._show_on_start()
        self._start_tray()
        if self.settings.get('strip'):
            self._open_strip()

        # Draw the real cards once before the first tick, so the opening size
        # is measured against actual content instead of the placeholders.
        self._paint_cards()
        self.root.update_idletasks()
        self._fitted = None
        self._fit()

        self.refresh_codex()
        self.refresh_antigravity()
        self.root.after(TICK_MS, self.tick)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build(self) -> None:
        palette = self.palette
        outer = tk.Frame(self.root, bg=palette.border)
        outer.pack(fill="both", expand=True)
        #: Everything inside the window hangs off this; a theme switch
        #: destroys it and builds the whole tree again in the new colours.
        self.outer = outer
        shell = tk.Frame(outer, bg=palette.canvas)
        shell.pack(fill="both", expand=True, padx=max(1, px(1)), pady=max(1, px(1)))

        # -- header ----------------------------------------------------
        header = tk.Frame(shell, bg=palette.canvas, height=px(32))
        header.pack(fill="x")
        header.pack_propagate(False)

        unit = px(14)
        bar = max(2, px(4))
        mark = tk.Canvas(header, width=unit, height=unit, bg=palette.canvas, highlightthickness=0, bd=0)
        mark.create_rectangle(0, unit * 0.30, bar, unit, fill=palette.accent, outline="")
        mark.create_rectangle(bar + px(1), 0, bar * 2 + px(1), unit, fill=palette.muted, outline="")
        mark.create_rectangle(bar * 2 + px(2), unit * 0.52, bar * 3 + px(2), unit, fill=palette.faint, outline="")
        mark.pack(side="left", padx=(px(SPACE_3), px(SPACE_2)))

        title = tk.Label(header, text=APP_NAME, font=TYPE.title, bg=palette.canvas, fg=palette.text)
        title.pack(side="left")
        # Nudged down so the small text sits on the title's baseline, not its middle.
        version = tk.Label(header, text=f"ver {__version__}", font=TYPE.version, bg=palette.canvas, fg=palette.faint)
        version.pack(side="left", padx=(px(SPACE_1), 0), pady=(px(3), 0))

        for widget in (header, title, version, mark):
            widget.bind("<ButtonPress-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.drag)
            widget.bind("<ButtonRelease-1>", self.end_drag)

        self.close_button = GhostButton(header, palette, "✕", self.hide, padx=SPACE_2)
        self.close_button.pack(side="right", padx=(0, px(SPACE_1)))
        self.compact_button = GhostButton(
            header, palette, "–" if not self.settings.get("compact") else "+", self.toggle_compact
        )
        self.compact_button.pack(side="right")
        self.pin_button = GhostButton(header, palette, self._pin_label(), self.toggle_pin)
        self.pin_button.pack(side="right")

        Hairline(shell, palette).pack(fill="x")

        # -- cards -------------------------------------------------------
        # The deck lives on a canvas so the window can be shorter than its
        # contents. The canvas asks for exactly the deck's height, which keeps
        # the fit-to-content behaviour working unchanged; only a height the
        # user dragged makes the window smaller than that, and only then does
        # anything scroll.
        self.body = tk.Frame(shell, bg=palette.canvas)

        self.order = self._visible()
        for key in SERVICE_ORDER:
            card = Card(self.body, palette)
            if key in self.order:
                card.pack(fill="x", pady=(0, px(CARD_GAP)))
            self.cards[key] = card
            self.rows[key] = card.body
            self.meters[key] = {}
            self._render_placeholder(key)

        # What the body shows when every card is switched off.
        self.empty = tk.Label(
            self.body, text="표시할 서비스가 없습니다\n왼쪽 아래 설정에서 켤 수 있습니다",
            font=TYPE.label, bg=palette.canvas, fg=palette.muted, justify="center",
        )
        if not self.order:
            self.empty.pack(fill="x", pady=px(SPACE_4))

        # -- footer ------------------------------------------------------
        # Everything below here is packed from the bottom up, and before the
        # deck, so the chrome keeps its rows at any window height.
        footer = tk.Frame(shell, bg=palette.canvas)
        self.footer_rule = Hairline(shell, palette)

        self.icons = icon_font(self.root)
        self.settings_button = GhostButton(
            footer, palette, glyph(GLYPH_SETTINGS, self.icons), self.open_settings,
            font=self.icons, padx=SPACE_2,
        )
        self.settings_button.pack(side="left")

        # One label per piece, so each figure can carry its level colour.
        self.status_row = tk.Frame(footer, bg=palette.canvas)
        self.status_row.pack(side="right", padx=(0, px(SPACE_2)))
        self._summary_signature = None

        self.footer = footer
        self._layout()

    def _layout(self) -> None:
        """Pack the shell bottom-up so the footer keeps its row.

        Collapsed, the footer is the whole body: the gear and the coloured
        figures on one line under the header.
        """
        compact = bool(self.settings.get("compact"))
        for part in (self.footer, self.footer_rule, self.body):
            part.pack_forget()
        self.footer.pack(fill="x", side="bottom", padx=px(SPACE_2), pady=px(SPACE_1))
        if not compact:
            # The header's own hairline already sits right above a collapsed footer.
            self.footer_rule.pack(fill="x", side="bottom")
            self.body.pack(fill="both", expand=True, padx=px(SPACE_3), pady=px(SPACE_3))

    def _card_inner_width(self) -> int:
        """Usable width inside a card: window minus every border and gutter."""
        border = max(1, px(1))
        return px(WINDOW_WIDTH) - 2 * border - 2 * px(SPACE_3) - 2 * border - 2 * px(SPACE_3)

    def _pin_label(self) -> str:
        return "📌" if self.settings.get("pinned", True) else "📍"

    def _apply_window_chrome(self) -> None:
        with contextlib.suppress(Exception):
            hwnd = int(self.root.wm_frame(), 16) if os.name == "nt" else 0
            round_corners(hwnd)
            drop_shadow(hwnd)

    def _show_on_start(self) -> None:
        """Open on screen and in front, whatever show state the launcher asked.

        Tk gives its first window the launcher's show state, so a shortcut set
        to "minimized" or a zip tool that asks for it starts the widget
        minimized. Frameless, it then has no taskbar button and looks like it
        never started.
        """
        with contextlib.suppress(Exception):
            if self.root.state() == "iconic":
                self.root.state("normal")
            restore_if_minimized(int(self.root.wm_frame(), 16) if os.name == "nt" else 0)
            self.root.update_idletasks()
        self.reveal()

    def _window_size(self) -> tuple:
        self.root.update_idletasks()
        width = px(WINDOW_WIDTH)
        # No scroll area, so a fixed cap would simply cut off the last card.
        # Only the screen limits it, leaving room for the taskbar.
        height = min(self.root.winfo_reqheight(), self.root.winfo_screenheight() - px(96))
        return width, height

    def _place_window(self) -> None:
        width, height = self._window_size()
        screen_w = self.root.winfo_screenwidth()
        x = self.settings.get("x")
        y = self.settings.get("y")
        if not isinstance(x, int):
            x = screen_w - width - px(24)
        if not isinstance(y, int):
            y = px(72)
        # Clamped to the whole desktop, so a window last closed on a second
        # monitor opens there again instead of being dragged back to the first.
        x, y = self._on_desktop(x, y)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _fit(self) -> None:
        """Re-measure after the cards change so nothing is clipped."""
        width, height = self._window_size()
        if (width, height) == self._fitted:
            return
        self._fitted = (width, height)
        x, y = self.root.winfo_x(), self.root.winfo_y()
        # Measured against the monitor the window is actually on, whose work
        # area already excludes that monitor's taskbar.
        area = work_area_at(x + width // 2, y + min(height, 1) )
        if area is not None:
            _, area_top, _, area_bottom = area
            if y + height > area_bottom:
                y = max(area_top, area_bottom - height)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _start_tray(self) -> None:
        icon = RESOURCE_DIR / "app.ico"
        self.tray = TrayIcon(
            APP_NAME,
            icon if icon.is_file() else None,
            on_activate=lambda: self.root.after(0, self.reveal),
            menu_items=[
                ("열기", lambda: self.root.after(0, self.reveal)),
                ("전체 새로고침", lambda: self.root.after(0, self.refresh_all)),
                ("-", lambda: None),
                ("Claude 연결 초기화", lambda: self.root.after(0, lambda: self.reset_account("claude"))),
                ("Gemini 연결 초기화", lambda: self.root.after(0, lambda: self.reset_account("gemini"))),
                ("-", lambda: None),
                ("종료", lambda: self.root.after(0, self.close)),
            ],
        )
        with contextlib.suppress(Exception):
            self.tray.start()

    # ------------------------------------------------------------------
    # window behaviour
    # ------------------------------------------------------------------
    def start_drag(self, event) -> None:
        self._drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag(self, event) -> None:
        offset = getattr(self, "_drag_offset", None)
        if not offset:
            return
        x, y = event.x_root - offset[0], event.y_root - offset[1]
        self.root.geometry("+{}+{}".format(*self._on_desktop(x, y)))

    def end_drag(self, _event=None) -> None:
        """Write the new position down the moment the user lets go.

        Only 종료 used to save it. The widget normally lives in the tray until
        the machine shuts down, and a shutdown never calls close(), so a
        window moved during the day reopened where it had been before.
        """
        if getattr(self, "_drag_offset", None):
            self._drag_offset = None
            self._remember_position()

    def _remember_position(self) -> None:
        self.settings["x"] = self.root.winfo_x()
        self.settings["y"] = self.root.winfo_y()
        save_settings(self.settings)

    def _on_desktop(self, x: int, y: int) -> tuple:
        """Keep a position inside the desktop - all monitors, not just the first.

        A monitor sitting higher than the primary one starts at a negative y,
        so clamping at zero stops the window short of its top edge by exactly
        that offset. Same for a monitor to the left.
        """
        bounds = virtual_screen()
        if bounds is None:
            return max(0, int(x)), max(0, int(y))
        width, height = self._window_size()
        return clamp_to(bounds, x, y, width, height)

    def toggle_pin(self) -> None:
        pinned = not self.settings.get("pinned", True)
        self.settings["pinned"] = pinned
        self.root.attributes("-topmost", pinned)
        self.pin_button.configure(text=self._pin_label())
        save_settings(self.settings)

    def toggle_compact(self) -> None:
        compact = not self.settings.get("compact", False)
        self.settings["compact"] = compact
        save_settings(self.settings)
        self._layout()
        self.compact_button.configure(text="+" if compact else "–")
        self._fitted = None
        self._fit()

    def reveal(self, to_cursor: bool = False) -> None:
        """Show the widget. With ``to_cursor``, also move it to the monitor
        the user is actually looking at."""
        self.root.deiconify()
        if to_cursor:
            self._move_to_cursor_monitor()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.focus_force()
        if not self.settings.get("pinned", True):
            self.root.after(200, lambda: self.root.attributes("-topmost", False))

    def _move_to_cursor_monitor(self) -> None:
        area = cursor_work_area()
        if not area:
            return
        left, top, right, bottom = area
        self.root.update_idletasks()
        width, height = self.root.winfo_width(), self.root.winfo_height()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if left <= x and x + width <= right and top <= y and y + height <= bottom:
            return  # already on that monitor
        # Put it back in its usual resting place: top-right of the work area.
        x = max(left, right - width - px(24))
        y = max(top, top + px(72))
        self.root.geometry(f"+{int(x)}+{int(y)}")

    def hide(self) -> None:
        """Close button sends the widget to the tray, never to the grave."""
        self.close_settings()
        self.root.withdraw()

    # ------------------------------------------------------------------
    # settings menu
    # ------------------------------------------------------------------
    def _hidden(self) -> List[str]:
        return [key for key in self.settings.get("hidden") or () if key in SERVICES]

    def _visible(self) -> List[str]:
        hidden = set(self._hidden())
        return [key for key in SERVICE_ORDER if key not in hidden]

    def open_settings(self) -> None:
        """The gear in the footer: actions first, then which cards to show,
        and 종료 at the very bottom."""
        if self.popover is not None and not self.popover.closed:
            self.close_settings()
            return
        popover = Popover(
            self.settings_button, self.palette, width=px(188), icons=self.icons,
            on_close=self._settings_closed,
        )
        popover.add_action("전체 새로고침", self.refresh_all, icon=GLYPH_REFRESH)
        popover.add_action("연결 안내", self.open_guide, icon=GLYPH_INFO)
        popover.add_action("문의하기", self.open_contact, icon=GLYPH_FEEDBACK)
        popover.add_separator()
        popover.add_heading("표시할 서비스")
        for key in SERVICE_ORDER:
            popover.add_toggle(
                SERVICES[key].name,
                lambda k=key: k not in self._hidden(),
                lambda k=key: self.toggle_service(k),
                dot=SERVICE_ACCENTS[key],
            )
        popover.add_separator()
        popover.add_heading("보기")
        popover.add_toggle(
            "작업 표시줄 막대",
            lambda: bool(self.settings.get("strip")),
            self.toggle_strip,
        )
        popover.add_separator()
        popover.add_heading("테마")
        for choice, label in THEME_CHOICES:
            popover.add_toggle(
                label,
                lambda c=choice: self.settings.get("theme", "auto") == c,
                lambda c=choice: self.set_theme(c),
            )
        popover.add_separator()
        popover.add_heading("시작")
        # The check is read from the shortcut itself, so a failed write, or
        # one deleted by hand in the Startup folder, shows as off.
        popover.add_toggle(
            "Windows 시작 시 실행",
            autostart.is_enabled,
            lambda: autostart.set_enabled(not autostart.is_enabled()),
        )
        popover.add_separator()
        # ✕ only sends the window to the tray; this is the way out from
        # inside the window, without hunting for the tray icon.
        popover.add_action("종료", self.close, icon=GLYPH_POWER)
        self.popover = popover
        popover.show()

    def close_settings(self) -> None:
        if self.popover is not None:
            self.popover.close()

    def set_theme(self, choice: str) -> None:
        """Switch between 자동 / 라이트 / 다크 and repaint everything now."""
        if choice not in dict(THEME_CHOICES) or choice == self.settings.get("theme", "auto"):
            return
        self.settings["theme"] = choice
        save_settings(self.settings)
        palette = resolve(choice)
        if palette == self.palette:
            # e.g. 자동 while Windows is already light and so was 라이트:
            # nothing to repaint, the menu's check mark is enough.
            return
        self.palette = palette
        # The menu belongs to the tree that is about to be replaced. Close it,
        # rebuild once this click has finished, then open it again in the new
        # colours so the user sees the result where they clicked.
        self.close_settings()
        self.root.after_idle(self._rebuild, True)

    def _rebuild(self, reopen_settings: bool = False) -> None:
        """Destroy the window's contents and build them again in the
        current palette. Cards are redrawn from the store, so nothing is lost."""
        self.close_settings()
        had_strip = self.strip is not None
        self._close_strip()
        self.outer.destroy()
        self.cards, self.rows, self.meters, self.rendered = {}, {}, {}, {}
        self.root.configure(bg=self.palette.canvas)
        self._build()
        self._paint_cards()
        self.root.update_idletasks()
        self._fitted = None
        self._fit()
        if had_strip:
            self._open_strip()
        if reopen_settings:
            self.open_settings()

    def _follow_popover(self, event) -> None:
        """Keep the open menu pinned just above the gear.

        Showing or hiding a card resizes the window, which moves the footer.
        Measuring right after ``geometry()`` reads the old position, because
        Tk learns the new size only from this Configure event; so the menu is
        re-placed from here, once the footer has been laid out again.
        """
        if event.widget is self.root and self.popover is not None:
            self.root.after_idle(self._place_popover)

    def _place_popover(self) -> None:
        if self.popover is not None:
            self.popover.place_near_anchor()

    def _settings_closed(self) -> None:
        self.popover = None
        with contextlib.suppress(tk.TclError):
            self.settings_button.settle()

    def toggle_service(self, key: str) -> None:
        """Show or hide one service's card. Hidden is hidden everywhere: the
        card, the collapsed strip and the tray tooltip."""
        hidden = set(self._hidden())
        hidden.symmetric_difference_update({key})
        self.settings["hidden"] = [k for k in SERVICE_ORDER if k in hidden]
        save_settings(self.settings)
        if key not in hidden:
            # Polling stops while a card is hidden; catch up now.
            if key == "codex":
                self.refresh_codex()
            elif key == "antigravity":
                self.refresh_antigravity()
        self._paint_cards()
        self._fitted = None
        self._fit()
        if self.popover is not None:
            # Placement is left to _follow_popover: the new window height is
            # only known once Windows reports the resize back.
            self.popover.sync()

    def toggle_strip(self) -> None:
        """Show or hide the wide strip that sits along the taskbar."""
        wanted = not self.settings.get("strip", False)
        self.settings["strip"] = wanted
        save_settings(self.settings)
        if wanted:
            self._open_strip()
        else:
            self._close_strip()

    def _open_strip(self) -> None:
        if self.strip is not None:
            return
        self.strip = TaskbarStrip(
            self.root, self.palette, self.settings,
            on_click=lambda: self.reveal(to_cursor=True),
            on_move=self._strip_moved,
        )
        self._paint_cards()  # fills it immediately instead of on the next tick

    def _close_strip(self) -> None:
        if self.strip is not None:
            self.strip.destroy()
            self.strip = None

    def _strip_moved(self, x: int, y: int) -> None:
        self.settings["strip_x"] = int(x)
        self.settings["strip_y"] = int(y)
        save_settings(self.settings)

    def open_guide(self) -> None:
        if GUIDE_FILE.is_file():
            with contextlib.suppress(OSError):
                os.startfile(str(GUIDE_FILE))  # noqa: S606 - a local html file we wrote

    def open_contact(self) -> None:
        """The contact page: GitHub Issues, or mail for people without an account."""
        if CONTACT_FILE.is_file():
            with contextlib.suppress(OSError):
                os.startfile(str(CONTACT_FILE))  # noqa: S606 - a local html file we wrote

    # ------------------------------------------------------------------
    # refreshing
    # ------------------------------------------------------------------
    def refresh_all(self) -> None:
        self.accounts.request_refresh()
        self.refresh_pending = True
        self._paint_summary(self.summary_text)
        self.refresh_codex()
        self.refresh_antigravity()

    def refresh_codex(self, login: bool = False) -> None:
        """Query Codex now. Every explicit call - start-up, 다시 시도,
        전체 새로고침, showing the card again - goes through here."""
        if self.busy or self.closing.is_set():
            return
        if self.codex_timer is not None:
            with contextlib.suppress(Exception):
                self.root.after_cancel(self.codex_timer)
            self.codex_timer = None
        if "codex" in self._hidden():
            # No point starting the CLI every 15 s for a card nobody sees.
            # toggle_service() restarts the cycle when it is shown again.
            return
        self.busy = True

        def work() -> None:
            try:
                snapshot = read_codex(
                    timeout=180,
                    cancel=self.closing,
                    login_callback=self.login_urls.put if login else None,
                )
                # The CLI's reply is outside data like any page's: labels such
                # as limitName come from it verbatim. Same gate as the bridge.
                snapshot = validate_snapshot(snapshot, source="cli")
                if not self.closing.is_set():
                    self.codex_needs_login = False
                    self.codex_paused = False
                    self.store.update(snapshot)
            except InterruptedError:
                return
            except CodexLoginRequired as error:
                self.codex_needs_login = True
                self.codex_paused = True
                self.store.fail("codex", str(error), source="cli", error_type="login_required")
            except CodexNotInstalled as error:
                self.codex_paused = True
                self.store.fail("codex", str(error), source="cli", error_type="not_installed")
            except Exception as error:  # noqa: BLE001 - surfaced in the card
                # Timeouts and dropped connections are worth retrying on our own.
                self.codex_paused = False
                message = str(error) if isinstance(error, CodexError) else "조회 실패 · Codex 연결을 확인해 주세요"
                self.store.fail("codex", message, source="cli", error_type=type(error).__name__)
            finally:
                self.busy = False

        self.codex_thread = threading.Thread(target=work, daemon=True, name="codex-quota")
        self.codex_thread.start()
        self.codex_timer = self.root.after(CODEX_INTERVAL_MS, self._poll_codex)

    def _poll_codex(self) -> None:
        """The 15-second cadence. Stands down while the last answer needs the
        user to act (install the CLI, sign in): retrying cannot fix those, so
        only 다시 시도 or 전체 새로고침 tries again."""
        self.codex_timer = None
        if self.busy:
            # A slow query is still running; check back on the next beat.
            self.codex_timer = self.root.after(CODEX_INTERVAL_MS, self._poll_codex)
            return
        if not self.codex_paused:
            self.refresh_codex()

    def refresh_antigravity(self) -> None:
        """Query the Antigravity language server now.

        No login branch, unlike Codex: we cannot sign anyone in to someone
        else's application. Either the IDE is running and signed in, or the
        card says which of the two is missing.
        """
        if self.agy_busy or self.closing.is_set():
            return
        if self.agy_timer is not None:
            with contextlib.suppress(Exception):
                self.root.after_cancel(self.agy_timer)
            self.agy_timer = None
        if "antigravity" in self._hidden():
            return
        self.agy_busy = True

        def work() -> None:
            try:
                try:
                    snapshot = read_antigravity(cancel=self.closing)
                    interval = ANTIGRAVITY_INTERVAL_MS
                except AntigravityNotRunning:
                    # Nothing is listening, so run the server the app would
                    # have run, ask it once, and shut it down again.
                    snapshot = launch_and_read(cancel=self.closing)
                    interval = ANTIGRAVITY_LAUNCH_INTERVAL_MS
                snapshot = validate_snapshot(snapshot, source="local")
                if not self.closing.is_set():
                    self.agy_paused = False
                    self.agy_interval = interval
                    self.agy_last = time.time()
                    self.store.update(snapshot)
            except InterruptedError:
                # Shutting down: say nothing, write nothing.
                return
            except AntigravityNotRunning as error:
                # Opening the app is all it takes, and we would not hear about
                # it, so this one keeps polling.
                self.agy_paused = False
                self.store.fail("antigravity", str(error), source="local", error_type="not_running")
            except AntigravityLoginRequired as error:
                self.agy_paused = True
                self.store.fail("antigravity", str(error), source="local", error_type="login_required")
            except Exception as error:  # noqa: BLE001 - surfaced in the card
                self.agy_paused = False
                message = str(error) if isinstance(error, AntigravityError) else "조회 실패 · Antigravity 연결을 확인해 주세요"
                self.store.fail("antigravity", message, source="local", error_type=type(error).__name__)
            finally:
                self.agy_busy = False

        self.agy_thread = threading.Thread(target=work, daemon=True, name="antigravity-quota")
        self.agy_thread.start()
        self.agy_timer = self.root.after(ANTIGRAVITY_INTERVAL_MS, self._poll_antigravity)

    def _poll_antigravity(self) -> None:
        """The cadence, which the last reading decides.

        The timer is set before the reading finishes, so it may be due sooner
        than the interval that reading settled on. Checking the clock here
        means a slow cadence takes effect immediately instead of after one
        more needless launch.
        """
        self.agy_timer = None
        if self.agy_busy:
            self.agy_timer = self.root.after(ANTIGRAVITY_INTERVAL_MS, self._poll_antigravity)
            return
        waited = (time.time() - self.agy_last) * 1000
        if self.agy_last and waited < self.agy_interval:
            self.agy_timer = self.root.after(int(self.agy_interval - waited), self._poll_antigravity)
            return
        if not self.agy_paused:
            self.refresh_antigravity()

    def connect_account(self, provider: str) -> None:
        if provider == "codex":
            self.codex_login_requested = True
            return
        if provider == "antigravity":
            self.refresh_antigravity()
            return
        self.accounts.connect(provider)

    def confirm_login(self, provider: str) -> None:
        """Tell the helper the user has finished signing in."""
        with contextlib.suppress(Exception):
            self.accounts.confirm_login(provider)
        self.store.fail(provider, "사용량 화면을 여는 중…", source="account", error_type="starting")
        self.rendered.pop(provider, None)

    def reset_account(self, provider: str) -> None:
        """Discard the saved browser profile for a service and reconnect.

        The way out of a bot check that has latched onto one profile and keeps
        challenging it forever.
        """
        with contextlib.suppress(Exception):
            self.accounts.reset(provider)
        self.store.fail(provider, "계정 연결 필요", source="account", error_type="login_required")
        self.rendered.pop(provider, None)
        self.reveal()

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def _render_placeholder(self, key: str) -> None:
        self._render_card(key, {"provider": key, "windows": [], "note": "", "updatedAt": 0}, "login_required")

    def _render_card(self, key: str, data: Dict[str, Any], account_state: str) -> None:
        palette = self.palette
        frame = self.rows[key]
        for child in frame.winfo_children():
            child.destroy()
        self.meters[key] = {}

        service = SERVICES[key]
        inner_width = self._card_inner_width()

        head = tk.Frame(frame, bg=palette.surface)
        head.pack(fill="x", padx=px(SPACE_3), pady=(px(CARD_PAD_Y), px(2)))
        Dot(head, palette, SERVICE_ACCENTS[key]).pack(side="left", pady=(px(3), 0))
        tk.Label(
            head, text=service.name, font=TYPE.service, bg=palette.surface, fg=palette.text
        ).pack(side="left", padx=(px(SPACE_2), 0))

        error = data.get("error")
        windows = data.get("windows") or []

        if error or not windows:
            if key == "codex":
                action, handler = ("계정 연결" if self.codex_needs_login else "다시 시도"), self.connect_account
            elif key == "antigravity":
                # Nothing to connect: the IDE owns the session. Only a retry.
                action, handler = "다시 시도", self.connect_account
            elif account_state == "login_manual":
                action, handler = "로그인 완료", self.confirm_login
            elif account_state == "verify_required":
                action, handler = "창 열기", self.connect_account
            elif not windows and account_state != "connected":
                action, handler = "계정 연결", self.connect_account
            else:
                action, handler = "다시 시도", self.connect_account
            GhostButton(
                head, palette, action, lambda h=handler: h(key), padx=SPACE_2, pady=0,
                foreground=palette.accent,
            ).pack(side="right")
        else:
            tk.Label(
                head,
                text=relative_time(float(data.get("updatedAt") or 0)),
                font=TYPE.meta,
                bg=palette.surface,
                fg=palette.faint,
            ).pack(side="right")

        if not windows:
            # A closed IDE is not a failure, so it reads like "not connected
            # yet" rather than like something that broke.
            idle = data.get("errorType") == "not_running"
            waiting = account_state in ("login_required", "starting", "closed", "login_manual") or not error or idle
            busy = (key == "codex" and self.busy) or (key == "antigravity" and self.agy_busy)
            busy = busy or account_state == "starting"
            if busy:
                # A retry must visibly do something; the previous error would
                # otherwise sit there unchanged and the click look ignored.
                message = "조회 중…"
                waiting = True
            else:
                message = error or "계정을 연결하면 사용량이 표시됩니다"
                if waiting and error in ("계정 연결 필요", "계정 연결 창이 닫혔습니다"):
                    message = "계정을 연결하면 사용량이 표시됩니다"
                if account_state == "login_manual":
                    message = "열린 창에서 로그인한 뒤 '로그인 완료'를 눌러 주세요"
            tk.Label(
                frame,
                text=message,
                font=TYPE.label,
                bg=palette.surface,
                # Not-yet-connected is not an error; only real failures go red.
                fg=palette.muted if waiting else palette.crit,
                wraplength=inner_width,
                justify="left",
                anchor="w",
            ).pack(fill="x", padx=px(SPACE_3), pady=(0, px(CARD_PAD_Y)))
            return

        for index, window in enumerate(windows):
            used = float(window.get("used") or 0)
            color = level_color(palette, used)

            row = tk.Frame(frame, bg=palette.surface)
            row.pack(fill="x", padx=px(SPACE_3), pady=(px(ROW_GAP), 0))
            tk.Label(
                row, text=window.get("label", ""), font=TYPE.label,
                bg=palette.surface, fg=palette.muted,
            ).pack(side="left")
            tk.Label(
                row, text=f"{used:g}%", font=TYPE.value, bg=palette.surface, fg=color
            ).pack(side="right")

            meter = Meter(frame, palette, inner_width)
            meter.pack(padx=px(SPACE_3), pady=(px(METER_GAP), 0), anchor="w")
            meter.set(used, color)
            self.meters[key][window.get("label", str(index))] = meter

            meta = self._meta_line(window)
            if meta:
                tk.Label(
                    frame, text=meta, font=TYPE.meta, bg=palette.surface, fg=palette.faint,
                    anchor="w", wraplength=inner_width, justify="left",
                ).pack(fill="x", padx=px(SPACE_3), pady=(px(META_GAP), 0))

        note = data.get("note")
        stale = time.time() - float(data.get("updatedAt") or 0) > STALE_AFTER
        tail = " · 오래된 정보" if stale else ""
        text = ((note or "") + tail).strip()
        color = palette.faint
        if error:
            # The numbers above are the last good reading; say why they are
            # not updating instead of leaving only a button to guess from.
            text = f"{error}{tail}"
            waiting = account_state in ("verify_required", "login_manual", "starting", "login_required")
            waiting = waiting or data.get("errorType") == "not_running"
            color = palette.muted if waiting else palette.crit
        if text:
            tk.Label(
                frame, text=text, font=TYPE.meta,
                bg=palette.surface, fg=color, anchor="w",
                wraplength=inner_width, justify="left",
            ).pack(fill="x", padx=px(SPACE_3), pady=(px(ROW_GAP), px(CARD_PAD_Y)))
        else:
            tk.Frame(frame, bg=palette.surface, height=px(CARD_PAD_Y)).pack(fill="x")

    @staticmethod
    def _meta_line(window: Dict[str, Any]) -> str:
        parts = []
        reset = str(window.get("resetText") or "").strip()
        if reset:
            # Page-scraped text is already a sentence; a bare timestamp is not.
            wordy = any(token in reset.lower() for token in ("초기화", "재설정", "reset", "refill", "starts"))
            parts.append(reset if wordy else f"{reset} 초기화")
        return " · ".join(parts)

    # ------------------------------------------------------------------
    # ordering
    # ------------------------------------------------------------------
    @staticmethod
    def _connected(usage: Dict[str, Any], states: Dict[str, str], key: str) -> bool:
        """A service counts as connected once it has real numbers to show.

        One rule for all of them: Codex and Antigravity have no account window
        to report a state, and a cached reading still means the account is
        linked - the numbers came from somewhere. An account that
        the manager says is connected counts too, so the card does not drop back
        down during a refresh that returned nothing yet.
        """
        if states.get(key) == "connected":
            return True
        item = usage.get(key) or {}
        return bool(item.get("windows"))

    def _reorder(self, usage: Dict[str, Any], states: Dict[str, str]) -> bool:
        """Re-pack the cards when the connected set changes. Returns True if moved."""
        visible = self._visible()
        live = [key for key in visible if self._connected(usage, states, key)]
        wanted = order_services(live, visible)
        if wanted == self.order:
            return False
        self.order = wanted
        for card in self.cards.values():
            card.pack_forget()
        self.empty.pack_forget()
        for key in wanted:
            self.cards[key].pack(fill="x", pady=(0, px(CARD_GAP)))
        if not wanted:
            self.empty.pack(fill="x", pady=px(SPACE_4))
        return True

    def _paint_cards(self) -> tuple:
        """Redraw whatever changed and return (moved_or_redrawn, summary text).

        Split out of :meth:`tick` so a resize can repaint immediately instead
        of waiting up to a second for the next tick.
        """
        usage = self.store.snapshot()
        states = self.accounts.status()
        summary = []
        strip_items = []
        changed = self._reorder(usage, states)

        for key in self.order:
            data = usage.get(key) or {"provider": key, "windows": [], "updatedAt": 0}
            state = states.get(key, "connected" if key in LOCAL_SERVICES else "login_required")
            signature = repr(
                (
                    [(w.get("label"), w.get("used"), w.get("resetText")) for w in data.get("windows", [])],
                    data.get("error"),
                    state,
                    int(float(data.get("updatedAt") or 0) // 60),
                    # Without this the card would not repaint when a retry
                    # starts, so "조회 중…" would never appear.
                    self.agy_busy if key == "antigravity" else (self.busy if key == "codex" else False),
                    data.get("errorType"),
                )
            )
            if self.rendered.get(key) != signature:
                self.rendered[key] = signature
                self._render_card(key, data, state)
                changed = True

            values = [w.get("used") for w in data.get("windows", []) if isinstance(w.get("used"), (int, float))]
            if values:
                summary.append(f"{SERVICES[key].short} {max(values):g}%")
            strip_items.append({
                "key": key,
                "short": SERVICES[key].short,
                "accent": SERVICE_ACCENTS[key],
                # The worst window is the one worth a glance; the rest is the deck's job.
                "used": max(values) if values else None,
                "stale": time.time() - float(data.get("updatedAt") or 0) > STALE_AFTER,
            })

        if self.strip is not None:
            self.strip.update(strip_items)

        if summary:
            text = "   ".join(summary)
        else:
            text = "연결 대기" if self.order else "표시 중인 서비스 없음"
        self.summary_text = text
        self.summary_items = strip_items
        self._paint_summary(text)
        return changed, text

    def _paint_summary(self, text: str) -> None:
        """The footer line next to the gear: each service's worst figure in
        the same colours as the strip, or a status message in its place."""
        figures = [i for i in self.summary_items if i["used"] is not None]
        if self.refresh_pending:
            message, figures = "갱신 요청 중…", []
        else:
            message = "" if figures else text
        signature = repr((message, [(i["short"], i["used"], i["stale"]) for i in figures]))
        if signature == self._summary_signature:
            return
        self._summary_signature = signature

        row = self.status_row
        for child in row.winfo_children():
            child.destroy()
        canvas = self.palette.canvas
        if message:
            tk.Label(row, text=message, font=TYPE.meta, bg=canvas, fg=self.palette.faint).pack(side="left")
        for index, item in enumerate(figures):
            tk.Label(
                row, text=item["short"], font=TYPE.meta, bg=canvas, fg=self.palette.faint,
            ).pack(side="left", padx=(0 if index == 0 else px(SPACE_2), 0))
            tk.Label(
                row, text=f"{item['used']:g}%", font=TYPE.meta, bg=canvas,
                fg=figure_color(self.palette, item["used"], item["stale"]),
            ).pack(side="left", padx=(px(SPACE_1), 0))

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def tick(self) -> None:
        """Run one beat and always book the next one.

        Tk drops an exception raised in an ``after`` callback after printing
        it to stderr - which pythonw sends nowhere - and the next beat was
        only booked at the end of the beat. One stray TclError therefore froze
        every card for good with no trace at all. Now the failure is logged
        and the clock keeps running.
        """
        if self.closing.is_set():
            return
        try:
            self._tick()
        except Exception as error:  # noqa: BLE001 - the clock must survive
            # The same fault would otherwise be written once a second.
            signature = f"{type(error).__name__}: {error}"
            if signature != self._last_tick_error:
                self._last_tick_error = signature
                log_exception("widget tick")
        finally:
            if not self.closing.is_set():
                with contextlib.suppress(tk.TclError):
                    self.root.after(TICK_MS, self.tick)

    def _tick(self) -> None:
        # A queued Codex login URL opens in the user's normal browser.
        while not self.login_urls.empty():
            with contextlib.suppress(queue.Empty):
                webbrowser.open(self.login_urls.get_nowait())
        if self.codex_login_requested and not self.busy:
            self.codex_login_requested = False
            # A plain retry must not drag the user through a browser login;
            # only ask for one when the CLI told us the account needs it.
            self.refresh_codex(login=self.codex_needs_login)

        if self.store.shutdown_requested.is_set():
            self.store.shutdown_requested.clear()
            self.close()
            return

        if self.store.reveal_requested.is_set():
            # Only a second launch sets this, and "I just ran it" means
            # "show it to me here", not "raise it on some other screen".
            self.store.reveal_requested.clear()
            self.reveal(to_cursor=True)

        changed, text = self._paint_cards()
        if self.tray is not None:
            # Name on the first line, figures on the second, so the tip
            # never wraps in the middle of a figure.
            self.tray.set_tooltip(APP_NAME + "\n" + text)

        # Both local providers have to land before the request is over; with
        # only Codex checked, the indicator went out while Antigravity was
        # still hunting for its server.
        if self.refresh_pending and not self.busy and not self.agy_busy:
            self.refresh_pending = False
            self._paint_summary(text)

        if changed:
            self._fit()

    def run(self) -> None:
        self.root.mainloop()

    def close(self) -> None:
        self.closing.set()
        self.close_settings()
        self._remember_position()
        self._close_strip()
        if self.tray is not None:
            with contextlib.suppress(Exception):
                self.tray.stop()
        with contextlib.suppress(Exception):
            self.accounts.stop()
        with contextlib.suppress(Exception):
            self.root.destroy()
