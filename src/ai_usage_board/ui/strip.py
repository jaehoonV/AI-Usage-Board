"""A wide, short companion window that lives along the taskbar.

Windows 11 removed deskbands, the API that let a program put its own content
*inside* the taskbar, and nothing replaced it. So this is the honest version of
that idea: a separate always-on-top window, no taller than a taskbar button,
parked in the monitor's work area directly above the taskbar. It reads as part
of the bar without pretending to be one.

It carries one figure per service - the worst of that service's windows, the
same number the tray tooltip uses - so a glance says whether anything is close
to its limit. Everything else stays in the deck.
"""
from __future__ import annotations

import contextlib
import tkinter as tk
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..theme import SPACE_1, SPACE_2, SPACE_3, TYPE, level_color, px
from .components import Dot
from .win32 import (
    clamp_to,
    covered_by_taskbar,
    cursor_work_area,
    raise_topmost,
    round_corners,
    virtual_screen,
)

#: Gap kept between the strip and the edge of the work area.
MARGIN = 12
#: How often to check that the taskbar has not slid over the strip.
GUARD_MS = 400


def figure_color(palette, used: Optional[float], stale: bool) -> str:
    """Colour of one service's figure, shared by the strip and the footer."""
    if used is None:
        return palette.faint
    if stale:
        return palette.muted
    return level_color(palette, used)


class TaskbarStrip:
    """One row of service figures, parked above the taskbar."""

    def __init__(
        self,
        root: tk.Misc,
        palette,
        settings: Dict[str, Any],
        on_click: Callable[[], None],
        on_move: Callable[[int, int], None],
    ) -> None:
        self.palette = palette
        self.settings = settings
        self.on_click = on_click
        self.on_move = on_move
        self._signature: Optional[str] = None
        self._drag: Optional[Tuple[int, int]] = None
        self._placed = False
        self._guard: Optional[str] = None

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=palette.border)

        self.shell = tk.Frame(self.win, bg=palette.canvas)
        self.shell.pack(fill="both", expand=True, padx=max(1, px(1)), pady=max(1, px(1)))
        self.row = tk.Frame(self.shell, bg=palette.canvas)
        self.row.pack(padx=px(SPACE_2), pady=px(SPACE_1))

        for target in (self.win, self.shell, self.row):
            target.bind("<ButtonPress-1>", self._press)
            target.bind("<B1-Motion>", self._drag_to)
            target.bind("<ButtonRelease-1>", self._release)
            target.bind("<Double-Button-1>", lambda _e: self.on_click())

        # Same Windows 11 corners as the main window.
        self.win.update_idletasks()
        with contextlib.suppress(tk.TclError, ValueError):
            round_corners(int(self.win.wm_frame(), 16))

        self._guard = self.win.after(GUARD_MS, self._keep_above_taskbar)

    # -- contents ---------------------------------------------------------
    def update(self, items: List[Dict[str, Any]]) -> None:
        """Redraw, but only when something actually changed.

        The widget repaints on a one-second tick; rebuilding this row that
        often would make it flicker and would fight the user's drag.
        """
        signature = repr([(i["key"], i["used"], i["stale"]) for i in items])
        if signature == self._signature:
            return
        self._signature = signature

        for child in self.row.winfo_children():
            child.destroy()

        if not items:
            tk.Label(
                self.row, text="연결 대기", font=TYPE.meta,
                bg=self.palette.canvas, fg=self.palette.faint,
            ).pack(side="left", padx=px(SPACE_1))
        for index, item in enumerate(items):
            cell = tk.Frame(self.row, bg=self.palette.canvas)
            cell.pack(side="left", padx=(0 if index == 0 else px(SPACE_3), 0))
            Dot(cell, self.palette, item["accent"]).pack(side="left", pady=(px(2), 0))
            tk.Label(
                cell, text=item["short"], font=TYPE.meta,
                bg=self.palette.canvas, fg=self.palette.muted,
            ).pack(side="left", padx=(px(SPACE_1), 0))
            used = item["used"]
            text = "–" if used is None else f"{used:g}%"
            colour = figure_color(self.palette, used, item["stale"])
            tk.Label(
                cell, text=text, font=TYPE.label, bg=self.palette.canvas, fg=colour,
            ).pack(side="left", padx=(px(SPACE_1), 0))
            for part in (cell,) + tuple(cell.winfo_children()):
                part.bind("<ButtonPress-1>", self._press)
                part.bind("<B1-Motion>", self._drag_to)
                part.bind("<ButtonRelease-1>", self._release)
                part.bind("<Double-Button-1>", lambda _e: self.on_click())

        self.win.update_idletasks()
        self._place()
        self.win.deiconify()
        with contextlib.suppress(tk.TclError):
            self.win.attributes("-topmost", True)

    # -- staying visible -------------------------------------------------
    def _keep_above_taskbar(self) -> None:
        """Lift the strip back over the taskbar whenever the bar covers it.

        Users park the strip on the taskbar itself. The taskbar is topmost as
        well, and clicking it, opening Start or hovering a flyout lifts it to
        the top of that band - over the strip, which then looked like it had
        vanished until a figure changed and the redraw re-asserted -topmost.
        Only the taskbar is checked, so menus and flyouts that open over the
        strip are left alone.
        """
        self._guard = None
        try:
            if self._drag is None and self.win.winfo_viewable():
                hwnd = int(self.win.wm_frame(), 16)
                if covered_by_taskbar(hwnd):
                    raise_topmost(hwnd)
            self._guard = self.win.after(GUARD_MS, self._keep_above_taskbar)
        except (tk.TclError, ValueError):
            pass  # destroyed mid-check

    # -- placement --------------------------------------------------------
    def _place(self) -> None:
        """Size the window, and choose a position only the first time.

        Re-deciding the position on every redraw moved the strip back to the
        primary monitor every time a figure changed, because the fallback
        bounds are that monitor. Once it is somewhere - placed here or dragged
        by the user - a redraw only resizes it.
        """
        width = max(1, self.win.winfo_reqwidth())
        height = max(1, self.win.winfo_reqheight())
        if self._placed:
            self.win.geometry(f"{width}x{height}+{self.win.winfo_x()}+{self.win.winfo_y()}")
            return

        x = self.settings.get("strip_x")
        y = self.settings.get("strip_y")
        if isinstance(x, int) and isinstance(y, int):
            # A remembered position may be on any monitor, so it is held to
            # the whole desktop rather than to the first screen.
            bounds = virtual_screen() or self._screen_area()
        else:
            bounds = cursor_work_area() or self._screen_area()
            left, top, right, bottom = bounds
            # Default: hard against the bottom-right of that monitor's work
            # area, which on a stock Windows layout is just above the clock.
            x = right - width - px(MARGIN)
            y = bottom - height - px(MARGIN) // 2
        x, y = clamp_to(bounds, x, y, width, height)
        self.win.geometry(f"{width}x{height}+{x}+{y}")
        self._placed = True

    def _screen_area(self) -> Tuple[int, int, int, int]:
        return (0, 0, self.win.winfo_screenwidth(), self.win.winfo_screenheight())

    # -- dragging ---------------------------------------------------------
    def _press(self, event) -> None:
        self._drag = (event.x_root - self.win.winfo_x(), event.y_root - self.win.winfo_y())

    def _drag_to(self, event) -> None:
        if self._drag is None:
            return
        offset_x, offset_y = self._drag
        self.win.geometry(f"+{event.x_root - offset_x}+{event.y_root - offset_y}")

    def _release(self, _event) -> None:
        if self._drag is None:
            return
        self._drag = None
        self.on_move(self.win.winfo_x(), self.win.winfo_y())

    # -- lifecycle --------------------------------------------------------
    def destroy(self) -> None:
        if self._guard is not None:
            with contextlib.suppress(Exception):
                self.win.after_cancel(self._guard)
            self._guard = None
        with contextlib.suppress(Exception):
            self.win.destroy()
