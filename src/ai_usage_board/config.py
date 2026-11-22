"""Filesystem layout, persisted settings and the single-instance guard.

Everything AI Usage Board writes lives under one per-user directory so the app can be
removed by deleting a folder. Nothing is written outside it and nothing is
written to the registry - save the Startup-folder shortcut the user may turn
on from the settings menu (see autostart.py).
"""
from __future__ import annotations

import contextlib
import html
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator

from . import APP_SLUG, CONTACT_EMAIL, REPO_URL, __version__

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
CONTACT_FILE = DATA_DIR / "contact.html"

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


def log_exception(heading: str = "") -> None:
    """Append the exception being handled to runtime.log, rotating at 256 KB.

    The app runs under pythonw, where stderr goes nowhere; this file is the
    only place a failure can leave a trace.
    """
    import traceback

    with contextlib.suppress(OSError):
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 256 * 1024:
            LOG_FILE.replace(LOG_FILE.with_suffix(".previous.log"))
    with contextlib.suppress(OSError):
        with LOG_FILE.open("a", encoding="utf-8") as stream:
            if heading:
                stream.write(f"\n--- {heading} ---\n")
            traceback.print_exc(file=stream)


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
    """Render the connection guide and the contact page into the data folder.

    Both are opened with the default browser, which cannot read files out of
    a PyInstaller bundle, so they need copies of their own. They sit side by
    side so the guide can link to the contact page by its bare name.
    """
    ensure_dirs()
    values = {
        # This machine's real folder, so it can be pasted straight into Explorer.
        "DATA_FOLDER": str(DATA_DIR),
        "VERSION": __version__,
        "ISSUE_URL": new_issue_url(),
        "EMAIL": CONTACT_EMAIL,
        "MAILTO_URL": mailto_url(),
        "GMAIL_URL": gmail_compose_url(),
    }
    for source, target in (("connect-guide.html", GUIDE_FILE), ("contact.html", CONTACT_FILE)):
        page = RESOURCE_DIR / source
        if not page.is_file():
            continue
        text = page.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", html.escape(value))
        atomic_write(target, text)


#: What a report should carry, written under a rule so the report itself goes
#: above it. Just the version: the one fact nobody remembers to include.
_REPORT_FOOTER = f"\n\n---\nver {__version__}"
_MAIL_SUBJECT = "[AI Usage Board] 문의"


def new_issue_url() -> str:
    """A new GitHub issue with the app version filled in."""
    from urllib.parse import urlencode

    return f"{REPO_URL}/issues/new?{urlencode({'body': _REPORT_FOOTER})}"


def mailto_url() -> str:
    """A message in the default mail app, subject and version filled in.

    quote, not urlencode: mail apps show a '+' for a space literally.
    """
    from urllib.parse import quote

    return f"mailto:{CONTACT_EMAIL}?subject={quote(_MAIL_SUBJECT)}&body={quote(_REPORT_FOOTER)}"


def gmail_compose_url() -> str:
    """The same message in Gmail on the web, for machines with no mail app."""
    from urllib.parse import urlencode

    query = urlencode({"view": "cm", "fs": "1", "to": CONTACT_EMAIL, "su": _MAIL_SUBJECT, "body": _REPORT_FOOTER})
    return f"https://mail.google.com/mail/?{query}"


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
