"""Reusable pieces of the widget's visual language.

Tk has no styling layer worth the name, so each component takes the palette and
paints itself. Keeping them here means the look is defined once.
"""
from __future__ import annotations

import time
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable, Optional

from ..theme import METER_HEIGHT, SPACE_1, SPACE_2, SPACE_3, TYPE, Palette, px


class Hairline(tk.Frame):
    """A 1px rule. Used instead of shadows to separate regions."""

    def __init__(self, parent: tk.Misc, palette: Palette, **kwargs) -> None:
        super().__init__(parent, bg=palette.border, height=max(1, px(1)), **kwargs)
        self.pack_propagate(False)


class Card(tk.Frame):
    """A hairline-bordered surface. The border is the outer frame; the inner
    frame is the content area, inset by exactly one pixel."""

    def __init__(self, parent: tk.Misc, palette: Palette) -> None:
        super().__init__(parent, bg=palette.border)
        self.body = tk.Frame(self, bg=palette.surface)
        self.body.pack(fill="both", expand=True, padx=max(1, px(1)), pady=max(1, px(1)))


class GhostButton(tk.Label):
    """A text button with no chrome until you hover it."""

    def __init__(
        self,
        parent: tk.Misc,
        palette: Palette,
        text: str,
        command: Callable[[], None],
        *,
        font=None,
        foreground: Optional[str] = None,
        padx: int = SPACE_2,
        pady: int = 3,
    ) -> None:
        self._palette = palette
        self._base_bg = parent.cget("bg")
        self._fg = foreground or palette.muted
        super().__init__(
            parent,
            text=text,
            font=font or TYPE.action,
            bg=self._base_bg,
            fg=self._fg,
            padx=px(padx),
            pady=px(pady),
            cursor="hand2",
        )
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", lambda _event: command())

    def _enter(self, _event) -> None:
        self.configure(bg=self._palette.surface_alt, fg=self._palette.text)

    def _leave(self, _event) -> None:
        self.configure(bg=self._base_bg, fg=self._fg)

    def settle(self) -> None:
        """Drop the hover look. A popover's grab swallows the <Leave> event,
        so whoever opened one calls this when it closes."""
        self._leave(None)

    def set_enabled(self, enabled: bool, text: Optional[str] = None) -> None:
        self._fg = self._palette.muted if enabled else self._palette.faint
        self.configure(
            fg=self._fg,
            cursor="hand2" if enabled else "arrow",
            text=text if text is not None else self.cget("text"),
        )


class Dot(tk.Canvas):
    """The single spot of brand colour on a card."""

    SIZE = 8

    def __init__(self, parent: tk.Misc, palette: Palette, color: str) -> None:
        size = px(self.SIZE)
        super().__init__(
            parent,
            width=size,
            height=size,
            bg=parent.cget("bg"),
            highlightthickness=0,
            bd=0,
        )
        self.create_oval(0, 0, size - 1, size - 1, fill=color, outline="")


class Meter(tk.Canvas):
    """A thin rounded progress track with an eased fill animation."""

    def __init__(self, parent: tk.Misc, palette: Palette, width: int) -> None:
        height = px(METER_HEIGHT)
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=parent.cget("bg"),
            highlightthickness=0,
            bd=0,
        )
        self._palette = palette
        self._width = width
        self._height = height
        self._value = 0.0
        self._job = None
        self._track = self._rounded(0, width, palette.track)
        self._fill = self._rounded(0, 0, palette.ok)

    def _rounded(self, start: int, end: int, color: str):
        """Draw a pill. Tk has no rounded rect, so: two circles plus a body."""
        height = self._height
        items = []
        if end - start <= 0:
            return items
        items.append(self.create_oval(start, 0, start + height, height, fill=color, outline=""))
        items.append(self.create_oval(end - height, 0, end, height, fill=color, outline=""))
        items.append(
            self.create_rectangle(start + height / 2, 0, end - height / 2, height, fill=color, outline="")
        )
        return items

    def _redraw_fill(self, value: float, color: str) -> None:
        for item in self._fill:
            self.delete(item)
        span = max(0.0, min(100.0, value)) / 100.0 * self._width
        if span < self._height:
            span = self._height if value > 0 else 0
        self._fill = self._rounded(0, int(span), color)

    def set(self, value: float, color: str, *, animate: bool = True, duration: float = 0.45) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:  # noqa: BLE001 - widget may be gone
                pass
            self._job = None
        if not animate:
            self._value = value
            self._redraw_fill(value, color)
            return

        start = self._value
        began = time.monotonic()

        def step() -> None:
            if not self.winfo_exists():
                return
            progress = min(1.0, (time.monotonic() - began) / duration)
            eased = 1 - (1 - progress) ** 3  # ease-out cubic
            current = start + (value - start) * eased
            self._redraw_fill(current, color)
            if progress < 1.0:
                self._job = self.after(16, step)
            else:
                self._value = value
                self._job = None

        step()


#: Icon-font code points (Segoe Fluent Icons / Segoe MDL2 Assets share them).
GLYPH_SETTINGS = ""
GLYPH_CHECK = ""
GLYPH_REFRESH = ""
GLYPH_INFO = ""
GLYPH_POWER = ""
GLYPH_FEEDBACK = ""
#: Plain-text stand-ins for machines without either icon font.
_FALLBACK = {
    GLYPH_SETTINGS: "⚙", GLYPH_CHECK: "✓", GLYPH_REFRESH: "↻", GLYPH_INFO: "?", GLYPH_POWER: "×",
    GLYPH_FEEDBACK: "✉",
}


def icon_font(root: tk.Misc):
    """The icon font to use, or None when neither is installed."""
    families = set(tkfont.families(root))
    for name in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if name in families:
            return (name, TYPE.glyph[1], "normal")
    return None


def glyph(code: str, font) -> str:
    return code if font else _FALLBACK.get(code, code)


class Popover(tk.Toplevel):
    """A small palette-styled menu floating next to the widget that opened it.

    Tk's native menu ignores colours on Windows, so it would stay light in a
    dark widget. This draws the same hairline card as the rest of the window.
    It closes on a click outside, Escape, or losing focus to another app.
    Toggle rows leave it open so several can be flipped in one go; action rows
    close it.
    """

    def __init__(
        self,
        anchor: tk.Misc,
        palette: Palette,
        *,
        width: int,
        icons=None,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(anchor)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=palette.border)
        self._palette = palette
        self._anchor = anchor
        self._width = width
        self._icons = icons
        self._on_close = on_close
        self._toggles: list = []
        self.closed = False

        self.body = tk.Frame(self, bg=palette.surface)
        self.body.pack(fill="both", expand=True, padx=max(1, px(1)), pady=max(1, px(1)))
        tk.Frame(self.body, bg=palette.surface, height=px(SPACE_1)).pack(fill="x")

        self.bind("<Escape>", lambda _event: self.close())
        self.bind("<ButtonPress>", self._press, add="+")
        self.bind("<FocusOut>", lambda _event: self.after(80, self._check_focus), add="+")

    # -- rows -------------------------------------------------------------
    def _row(self, on_click: Callable[[], None]):
        palette = self._palette
        row = tk.Frame(self.body, bg=palette.surface, cursor="hand2")
        row.pack(fill="x", padx=px(SPACE_1))
        parts = [row]

        def paint(color: str) -> None:
            for part in parts:
                part.configure(bg=color)

        def click(_event):
            on_click()
            return "break"

        def hook(part: tk.Misc) -> None:
            if part not in parts:
                parts.append(part)
            part.bind("<Enter>", lambda _e: paint(palette.surface_alt))
            part.bind("<Leave>", lambda _e: paint(palette.surface))
            part.bind("<Button-1>", click)

        return row, hook

    def _icon_cell(self, row: tk.Frame, text: str, color: str) -> tk.Label:
        return tk.Label(
            row, text=text, font=self._icons or TYPE.label, width=2,
            bg=self._palette.surface, fg=color,
        )

    def add_action(self, text: str, command: Callable[[], None], icon: str = "") -> None:
        def run() -> None:
            self.close()
            command()

        row, hook = self._row(run)
        hook(row)
        cell = self._icon_cell(row, glyph(icon, self._icons) if icon else "", self._palette.muted)
        cell.pack(side="left", padx=(px(SPACE_1), 0), pady=px(SPACE_1))
        hook(cell)
        label = tk.Label(row, text=text, font=TYPE.action, bg=self._palette.surface, fg=self._palette.text)
        label.pack(side="left", padx=(px(SPACE_1), 0), pady=px(SPACE_1))
        hook(label)

    def add_toggle(
        self,
        text: str,
        is_on: Callable[[], bool],
        toggle: Callable[[], None],
        dot: Optional[str] = None,
    ) -> None:
        row, hook = self._row(lambda: (toggle(), self.sync()))
        hook(row)
        cell = self._icon_cell(row, "", self._palette.accent)
        self._toggles.append((cell, is_on))
        self.sync()
        cell.pack(side="left", padx=(px(SPACE_1), 0), pady=px(SPACE_1))
        hook(cell)
        if dot:
            mark = Dot(row, self._palette, dot)
            mark.pack(side="left", padx=(px(SPACE_1), 0))
            hook(mark)
        label = tk.Label(row, text=text, font=TYPE.action, bg=self._palette.surface, fg=self._palette.text)
        label.pack(side="left", padx=(px(SPACE_2) if dot else px(SPACE_1), 0), pady=px(SPACE_1))
        hook(label)

    def sync(self) -> None:
        """Redraw every check from its source of truth, so a change made
        elsewhere (or one the toggle refused) never leaves a stale mark."""
        check = glyph(GLYPH_CHECK, self._icons)
        for cell, is_on in self._toggles:
            if cell.winfo_exists():
                cell.configure(text=check if is_on() else "")

    def add_heading(self, text: str) -> None:
        tk.Label(
            self.body, text=text, font=TYPE.meta, bg=self._palette.surface, fg=self._palette.faint, anchor="w",
        ).pack(fill="x", padx=px(SPACE_3), pady=(px(SPACE_1), 0))

    def add_separator(self) -> None:
        Hairline(self.body, self._palette).pack(fill="x", padx=px(SPACE_2), pady=px(SPACE_1))

    # -- placement and lifetime -------------------------------------------
    def place_near_anchor(self) -> None:
        """Sit just above the anchor, left edges aligned; below if no room."""
        if self.closed:
            return
        self.update_idletasks()
        height = self.winfo_reqheight()
        anchor = self._anchor
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() - height - px(SPACE_1)
        if y < anchor.winfo_vrooty():
            y = anchor.winfo_rooty() + anchor.winfo_height() + px(SPACE_1)
        self.geometry(f"{self._width}x{height}+{x}+{y}")

    def show(self) -> None:
        tk.Frame(self.body, bg=self._palette.surface, height=px(SPACE_1)).pack(fill="x")
        self.place_near_anchor()
        self.deiconify()
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except tk.TclError:
            pass  # another grab is active; Escape / focus loss still close it

    def _press(self, event):
        if self.closed:
            return None
        left, top = self.winfo_rootx(), self.winfo_rooty()
        inside = left <= event.x_root < left + self.winfo_width() and top <= event.y_root < top + self.winfo_height()
        if not inside:
            self.close()
            return "break"
        return None

    def _check_focus(self) -> None:
        if self.closed:
            return
        try:
            focus = self.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if focus is None or focus.winfo_toplevel() is not self:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.grab_release()
            self.destroy()
        except tk.TclError:
            pass
        if self._on_close is not None:
            self._on_close()


def relative_time(stamp: float) -> str:
    """'방금', '3분 전', '14:02' - whichever reads fastest."""
    if not stamp:
        return "—"
    age = max(0, int(time.time() - stamp))
    if age < 45:
        return "방금"
    if age < 3600:
        return f"{age // 60}분 전"
    return time.strftime("%H:%M", time.localtime(stamp))


__all__ = [
    "Card",
    "Dot",
    "GhostButton",
    "Hairline",
    "Meter",
    "Popover",
    "icon_font",
    "glyph",
    "relative_time",
    "SPACE_1",
    "SPACE_2",
    "SPACE_3",
]
