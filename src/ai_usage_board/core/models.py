"""Service registry and the one validator every snapshot must pass.

Snapshots arrive from three untrusted-ish places: a child CLI process, a page
script evaluated in a browser, and an HTTP request on loopback. They are all
funnelled through :func:`validate_snapshot`, which rebuilds a clean dict rather
than trusting the incoming shape. Nothing downstream sees a raw payload.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List

MAX_WINDOWS = 8
LABEL_LIMIT = 60
NOTE_LIMIT = 120
RESET_LIMIT = 60


@dataclass(frozen=True)
class Service:
    key: str
    name: str
    #: Two letters for the collapsed strip and tray tooltip, where "Codex" and
    #: "Claude" would otherwise both read as "C".
    short: str
    usage_url: str
    #: How the widget obtains numbers for this service.
    mode: str  # "cli" | "browser" | "local"
    poll_seconds: int


SERVICES: Dict[str, Service] = {
    "codex": Service(
        key="codex",
        name="Codex",
        short="CX",
        usage_url="https://chatgpt.com/settings/usage?tab=overview",
        mode="cli",
        poll_seconds=15,
    ),
    "claude": Service(
        key="claude",
        name="Claude",
        short="CL",
        usage_url="https://claude.ai/settings/usage",
        mode="browser",
        poll_seconds=60,
    ),
    "gemini": Service(
        key="gemini",
        name="Gemini",
        short="GM",
        usage_url="https://gemini.google.com/usage",
        mode="browser",
        poll_seconds=60,
    ),
    "antigravity": Service(
        key="antigravity",
        name="Antigravity",
        short="AG",
        #: Antigravity publishes no usage page; its numbers live in the IDE.
        #: The link opens the app's own download/sign-in page instead.
        usage_url="https://antigravity.google/",
        mode="local",
        poll_seconds=60,
    ),
}

#: Default top-to-bottom order of the cards.
SERVICE_ORDER = ("claude", "codex", "gemini", "antigravity")

#: Services whose numbers come from this machine rather than from an account
#: window the widget owns, so they never report an account state.
LOCAL_SERVICES = ("codex", "antigravity")


def order_services(connected, base=SERVICE_ORDER) -> List[str]:
    """Connected services first, everything else after, both in base order.

    The partition is stable on purpose: a card only ever moves when a service
    actually connects or drops, never because two services swapped refresh
    ticks. With only Gemini connected the deck reads gemini, claude, codex.
    """
    live = set(connected or ())
    return [k for k in base if k in live] + [k for k in base if k not in live]


@dataclass
class UsageWindow:
    label: str
    #: Percentage of the window already consumed, 0-100. Every source speaks
    #: in used percent - Codex sends usedPercent, both web pages print
    #: "N% 사용됨" - so this is the number as given, not a derived one.
    used: float
    reset_text: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "used": self.used, "resetText": self.reset_text}


class SnapshotError(ValueError):
    """Raised when an incoming payload cannot be trusted."""


def number(value: Any, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SnapshotError("Invalid numeric value")
    value = float(value)
    if not math.isfinite(value):
        raise SnapshotError("Invalid numeric value")
    if value < minimum or value > maximum:
        raise SnapshotError("Numeric value outside range")
    return value


def clean_text(value: Any, limit: int = NOTE_LIMIT) -> str:
    if not isinstance(value, str):
        raise SnapshotError("Invalid text")
    # Collapse whitespace, drop private-use glyphs some web fonts inject.
    stripped = "".join(ch for ch in value if not ("" <= ch <= ""))
    return " ".join(stripped.split())[:limit]


def validate_snapshot(value: Any, *, source: str = "browser") -> Dict[str, Any]:
    """Return a freshly built snapshot dict, or raise :class:`SnapshotError`."""
    if not isinstance(value, dict):
        raise SnapshotError("Invalid snapshot")

    provider = clean_text(value.get("provider"), 16)
    if provider not in SERVICES:
        raise SnapshotError("Unknown provider")

    raw_windows = value.get("windows")
    if not isinstance(raw_windows, list) or len(raw_windows) > MAX_WINDOWS:
        raise SnapshotError("Invalid windows")

    windows: List[Dict[str, Any]] = []
    for item in raw_windows:
        if not isinstance(item, dict):
            raise SnapshotError("Invalid window")
        label = clean_text(item.get("label", ""), LABEL_LIMIT)
        if not label:
            raise SnapshotError("Missing label")
        windows.append(
            UsageWindow(
                label=label,
                used=number(item.get("used"), 0, 100),
                reset_text=clean_text(item.get("resetText", ""), RESET_LIMIT),
            ).as_dict()
        )

    note = clean_text(value.get("note", ""), NOTE_LIMIT)
    updated_at = value.get("pageUpdatedAt")
    try:
        stamp = number(updated_at, 0, 4102444800)
    except SnapshotError:
        stamp = time.time()
    # A page clock that is wildly ahead of ours is not trustworthy.
    if abs(stamp - time.time()) > 600:
        stamp = time.time()

    return {
        "provider": provider,
        "windows": windows,
        "note": note,
        "updatedAt": stamp,
        "source": source,
        "error": None,
    }

