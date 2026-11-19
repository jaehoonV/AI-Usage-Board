"""Design tokens.

One flat palette, one spacing scale, one type scale. Colour is used only to
carry meaning (which service, how urgent); everything structural is neutral.
Both schemes are tuned so the widget sits quietly on a desktop instead of
competing with it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Tuple

# --- spacing -------------------------------------------------------------
# A 4px base keeps every gap on the same rhythm.
SPACE_1, SPACE_2, SPACE_3, SPACE_4, SPACE_5 = 4, 8, 12, 16, 24

# --- geometry ------------------------------------------------------------
# All sizes below are in *logical* pixels at 100% scaling. Run them through
# :func:`px` before handing them to Tk so the widget stays crisp at 125/150%.
WINDOW_WIDTH = 304
METER_HEIGHT = 5

#: Vertical rhythm inside a card. Tighter than the base spacing scale so four
#: cards fit on a laptop screen without dominating it. These are the vertical
#: steps only - the horizontal gutters
#: stay on SPACE_3, because they set the meter width every card is drawn to.
CARD_PAD_Y = 7  # above the card's title row, and below its last line
ROW_GAP = 4  # above each label/value row
METER_GAP = 2  # between a label row and its meter
META_GAP = 2  # between a meter and its reset line
CARD_GAP = 4  # between one card and the next

#: Overall size of the widget, as a multiple of the sizes above. One knob for
#: the whole interface: it scales spacing, geometry and type together, so the
#: proportions never drift. 1.0 is the design size.
UI_SCALE = 0.90

_scale = 1.0


def set_scale(dpi_factor: float) -> None:
    """Combine the display's DPI factor with :data:`UI_SCALE`."""
    global _scale
    _scale = max(0.5, min(4.0, float(dpi_factor) * UI_SCALE))


def get_scale() -> float:
    return _scale


def px(value: float) -> int:
    """Logical pixels -> device pixels for the current display scaling."""
    return int(round(value * _scale))

# --- type ----------------------------------------------------------------
# Segoe UI Variable on Windows 11, Segoe UI elsewhere, 맑은 고딕 for Hangul.
_UI = "Segoe UI Variable Display"
_UI_FALLBACK = "Segoe UI"
_KR = "맑은 고딕"


def _family() -> str:
    return _UI if os.name == "nt" else _UI_FALLBACK


@dataclass(frozen=True)
class Type:
    title: Tuple[str, int, str] = field(default_factory=lambda: (_family(), 11, "bold"))
    #: "ver 1.1.3" beside the title: same family, small and quiet.
    version: Tuple[str, int, str] = field(default_factory=lambda: (_family(), 8, "normal"))
    service: Tuple[str, int, str] = field(default_factory=lambda: (_family(), 10, "bold"))
    value: Tuple[str, int, str] = field(default_factory=lambda: (_family(), 14, "bold"))
    label: Tuple[str, int, str] = field(default_factory=lambda: (_KR, 9, "normal"))
    meta: Tuple[str, int, str] = field(default_factory=lambda: (_KR, 8, "normal"))
    action: Tuple[str, int, str] = field(default_factory=lambda: (_KR, 9, "normal"))
    glyph: Tuple[str, int, str] = field(default_factory=lambda: ("Segoe Fluent Icons", 10, "normal"))


TYPE = Type()


@dataclass(frozen=True)
class Palette:
    name: str
    canvas: str       # window background
    surface: str      # card background
    surface_alt: str  # hover / inset
    border: str       # hairline
    text: str
    muted: str
    faint: str
    track: str        # meter background
    ok: str
    warn: str
    crit: str
    accent: str


LIGHT = Palette(
    name="light",
    canvas="#FFFFFF",
    surface="#FFFFFF",
    surface_alt="#F4F6F8",
    border="#E4E7EC",
    text="#111827",
    muted="#616B7A",
    faint="#9AA3B0",
    track="#EDF0F3",
    ok="#2F7D6B",
    warn="#B45309",
    crit="#C0392F",
    accent="#2563EB",
)

DARK = Palette(
    name="dark",
    canvas="#15171C",
    surface="#1C1F26",
    surface_alt="#23262E",
    border="#2C313A",
    text="#E9ECF1",
    muted="#9BA4B2",
    faint="#6E7785",
    track="#2A2E37",
    ok="#4BB79C",
    warn="#E0A458",
    crit="#E8705F",
    accent="#6AA2FF",
)


#: Per-service identity. A single 8px dot carries the brand; nothing else is
#: tinted, which keeps a stack of service cards readable.
SERVICE_ACCENTS: Dict[str, str] = {
    "codex": "#10A37F",
    "claude": "#D97757",
    "gemini": "#4285F4",
    #: Antigravity's own mark is monochrome, so this is picked for separation
    #: from the three above rather than taken from a brand.
    "antigravity": "#7B61FF",
}


def windows_prefers_dark() -> bool:
    """Read the Windows app theme. Defaults to light when unreadable."""
    if os.name != "nt":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        with key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except OSError:
        return False


def resolve(preference: str = "auto") -> Palette:
    if preference == "dark":
        return DARK
    if preference == "light":
        return LIGHT
    return DARK if windows_prefers_dark() else LIGHT


def level_color(palette: Palette, used: float) -> str:
    """Used percentage -> meaning colour. Quiet until it matters."""
    if used >= 90:
        return palette.crit
    if used >= 65:
        return palette.warn
    return palette.ok
