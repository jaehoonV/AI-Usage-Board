"""Start with Windows: one shortcut in the user's Startup folder.

A shortcut rather than a ``Run`` key, because the app promises to write
nothing to the registry, and a file in a folder the user can open is easy to
see and to delete by hand. Whether it is on is read from that file, never from
settings.json, so a shortcut removed in Explorer (or in Task Manager's
startup tab) shows as off in the menu too.

The shortcut is written through the shell's own ShellLink COM object with
ctypes: no extra package, and no PowerShell window flashing on screen.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

from . import APP_NAME
from .config import RESOURCE_DIR, log_exception

IS_WINDOWS = os.name == "nt"

#: Also the name Task Manager lists under its startup apps.
SHORTCUT_NAME = f"{APP_NAME}.lnk"

CSIDL_STARTUP = 7
COINIT_APARTMENTTHREADED = 2
CLSCTX_INPROC_SERVER = 1
CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
IID_ISHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
IID_IPERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"

# Vtable slots. IUnknown takes 0-2 on both interfaces.
_QUERY_INTERFACE, _RELEASE = 0, 2
_SET_DESCRIPTION, _SET_WORKING_DIRECTORY, _SET_ARGUMENTS = 7, 9, 11
_SET_ICON_LOCATION, _SET_PATH = 17, 20
_PERSIST_SAVE = 6


def startup_dir() -> Optional[Path]:
    """The current user's Startup folder, wherever it has been redirected."""
    if not IS_WINDOWS:
        return None
    buffer = ctypes.create_unicode_buffer(1024)
    with contextlib.suppress(Exception):
        if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_STARTUP, None, 0, buffer) == 0 and buffer.value:
            return Path(buffer.value)
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Optional[Path]:
    folder = startup_dir()
    return folder / SHORTCUT_NAME if folder else None


def launch_command() -> Tuple[str, str, str, str]:
    """(target, arguments, working directory, icon) that start this very build.

    The exe for a release. From source, ``pythonw run.py``: the plain
    interpreter would leave a console window open beside the widget.
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        return str(exe), "", str(exe.parent), ""
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    if windowless.is_file():
        interpreter = windowless
    script = Path(__file__).resolve().parents[2] / "run.py"
    icon = RESOURCE_DIR / "app.ico"
    return str(interpreter), f'"{script}"', str(script.parent), str(icon) if icon.is_file() else ""


def is_enabled() -> bool:
    path = shortcut_path()
    return bool(path and path.is_file())


def set_enabled(on: bool) -> bool:
    """Create or remove the shortcut and return the state it ended in.

    Never raises: a failure is logged and the menu shows what is really there.
    """
    path = shortcut_path()
    if path is None:
        return False
    try:
        if on:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_shortcut(path, *launch_command())
        else:
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - a menu click must not take the widget down
        log_exception("autostart")
    return is_enabled()


# ----------------------------------------------------------------------
# ShellLink through raw COM
# ----------------------------------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    guid = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    return guid


def _method(obj: ctypes.c_void_p, slot: int, *argtypes):
    """Bind vtable slot ``slot`` of a COM interface pointer.

    HRESULT as the return type makes ctypes raise OSError on any failure.
    """
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    function = prototype(vtable[slot])
    return lambda *args: function(obj, *args)


def _release(obj: ctypes.c_void_p) -> None:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[_RELEASE])(obj)


def write_shortcut(path: Path, target: str, arguments: str, working_dir: str, icon: str = "") -> None:
    """Save a .lnk at ``path`` that runs ``target arguments`` in ``working_dir``."""
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    # S_OK or S_FALSE: ours to balance. RPC_E_CHANGED_MODE: this thread is
    # already in COM under another model, which is fine for an in-proc object.
    initialized = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED) in (0, 1)
    link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    text = ctypes.c_wchar_p
    try:
        ctypes.oledll.ole32.CoCreateInstance(
            ctypes.byref(_guid(CLSID_SHELL_LINK)), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(_guid(IID_ISHELL_LINK_W)), ctypes.byref(link),
        )
        _method(link, _SET_PATH, text)(target)
        _method(link, _SET_ARGUMENTS, text)(arguments)
        _method(link, _SET_WORKING_DIRECTORY, text)(working_dir)
        _method(link, _SET_DESCRIPTION, text)(APP_NAME)
        if icon:
            _method(link, _SET_ICON_LOCATION, text, ctypes.c_int)(icon, 0)
        _method(link, _QUERY_INTERFACE, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
            ctypes.byref(_guid(IID_IPERSIST_FILE)), ctypes.byref(persist),
        )
        _method(persist, _PERSIST_SAVE, text, ctypes.c_int)(str(path), 1)
    finally:
        for obj in (persist, link):
            if obj.value:
                _release(obj)
        if initialized:
            ole32.CoUninitialize()
