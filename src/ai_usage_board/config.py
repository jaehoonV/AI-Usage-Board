"""Filesystem layout, persisted settings and the single-instance guard.

Everything AI Usage Board writes lives under one per-user directory so the app can be
removed by deleting a folder. Nothing is written outside it and nothing is
written to the registry.
"""
from __future__ import annotations

import contextlib
import html
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator

from . import APP_SLUG

def _resource_dir() -> Path:
    """Where the read-only files that ship with the build live.

    They sit next to this module both in the source tree and in the
    PyInstaller bundle, which keeps the package layout
    (``_internal/ai_usage_board/resources``, see ai_usage_board.spec) - not
    ``_MEIPASS/resources``.
    """
    here = Path(__file__).resolve().parent / "resources"
    if here.is_dir():
        return here
    bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent / "_internal"))
    return bundle / "ai_usage_board" / "resources"


#: Read-only files that ship with the build.
RESOURCE_DIR = _resource_dir()

#: Per-user writable state: settings, cache, browser profiles, logs.
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / APP_SLUG

SETTINGS_FILE = DATA_DIR / "settings.json"
CACHE_FILE = DATA_DIR / "usage-cache.json"
TOKEN_FILE = DATA_DIR / ".bridge-token"
LOG_FILE = DATA_DIR / "runtime.log"
ACCOUNTS_DIR = DATA_DIR / "accounts"
GUIDE_FILE = DATA_DIR / "connect-guide.html"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "x": None,
    "y": None,
    "pinned": True,
    "compact": False,
    #: A second, wide strip along the bottom of the screen, above the taskbar,
    #: carrying one figure per service.
    "strip": False,
    "strip_x": None,
    "strip_y": None,
    "theme": "auto",  # auto | light | dark
    #: Service keys whose cards the user switched off in the settings menu.
    #: Checked against the service registry by the widget, not here: this
    #: module sits below ``core`` and must not import it.
    "hidden": [],
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write via a sibling temp file so a crash never leaves a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding=encoding)
    temporary.replace(path)


def load_settings() -> Dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    settings["hidden"] = []  # never hand out the shared default list
    try:
        # utf-8-sig, not utf-8: anything that edits this file on Windows -
        # PowerShell's Set-Content, Notepad - may leave a byte-order mark, and
        # json.loads refuses one. Losing every setting over three bytes is a
        # poor trade for a file a user is invited to look at.
        stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return settings
    if not isinstance(stored, dict):
        return settings
    for key, default in DEFAULT_SETTINGS.items():
        value = stored.get(key, default)
        if key in ("x", "y", "strip_x", "strip_y"):
            number = isinstance(value, (int, float)) and not isinstance(value, bool)
            settings[key] = int(value) if number else None
        elif key in ("pinned", "compact", "strip"):
            settings[key] = bool(value)
        elif key == "theme" and value in ("auto", "light", "dark"):
            settings[key] = value
        elif key == "hidden":
            items = value if isinstance(value, list) else []
            settings[key] = list(dict.fromkeys(i for i in items if isinstance(i, str) and 0 < len(i) <= 16))
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        atomic_write(SETTINGS_FILE, json.dumps(settings, ensure_ascii=False, indent=2))


def prepare_user_files() -> None:
    """Render the connection guide into the data folder with its path filled in.

    The guide is opened with the default browser, which cannot read files out
    of a PyInstaller bundle, so it needs a copy of its own.
    """
    ensure_dirs()
    guide = RESOURCE_DIR / "connect-guide.html"
    if guide.is_file():
        text = guide.read_text(encoding="utf-8")
        text = text.replace("{{DATA_FOLDER}}", html.escape(str(DATA_DIR)))
        atomic_write(GUIDE_FILE, text)


@contextlib.contextmanager
def single_instance(name: str = "Local\\AIUsageBoard.SingleInstance") -> Iterator[bool]:
    """Yield True when this process owns the named mutex, False otherwise."""
    if os.name != "nt":
        yield True
        return

    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]

    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise OSError("Could not create the startup lock")

    acquired = kernel.WaitForSingleObject(handle, 0) in (0, 128)
    try:
        yield acquired
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)
