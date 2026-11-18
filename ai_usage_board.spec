# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build. Produces dist/AIUsageBoard/AIUsageBoard.exe (onedir)."""
import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

ROOT = Path(SPECPATH)
RESOURCES = ROOT / "src" / "ai_usage_board" / "resources"

# Read rather than import: the package is not on sys.path while the spec runs,
# and there is only supposed to be one place a version number lives.
VERSION = re.search(
    r'__version__ = "([^"]+)"',
    (ROOT / "src" / "ai_usage_board" / "__init__.py").read_text(encoding="utf-8"),
).group(1)
NUMBERS = tuple((list(int(part) for part in VERSION.split(".")) + [0, 0, 0, 0])[:4])

# Without this the exe tells Windows nothing about itself: no version in the
# file's properties, nothing for an updater or an inventory to read.
VERSION_RESOURCE = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS, mask=0x3F, flags=0x0,
                      OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "jaehoon"),
            StringStruct("FileDescription", "AI Usage Board"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "AIUsageBoard"),
            StringStruct("LegalCopyright", "Copyright (c) 2026 jaehoon. MIT License."),
            StringStruct("OriginalFilename", "AIUsageBoard.exe"),
            StringStruct("ProductName", "AI Usage Board"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[(str(RESOURCES), "ai_usage_board/resources")],
    hiddenimports=[
        "ai_usage_board.providers.browser_account",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["test", "unittest", "pydoc_data", "lib2to3"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AIUsageBoard",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(RESOURCES / "app.ico"),
    version=VERSION_RESOURCE,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AIUsageBoard",
)
