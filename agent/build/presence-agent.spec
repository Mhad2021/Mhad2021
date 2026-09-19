# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the Presence tracker agent.

Build from the agent directory:
    pyinstaller build/presence-agent.spec --noconfirm --clean

Produces a single windowed executable (no console window) at
dist/PresenceTracker.exe, around 20MB.
"""
import sys
from pathlib import Path

AGENT_ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(AGENT_ROOT / "run_agent.py")],
    pathex=[str(AGENT_ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "tracker.idle.windows",
        "tracker.idle.macos",
        "tracker.idle.linux",
        "pystray._win32" if sys.platform == "win32" else "pystray._base",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Trim the standard library modules Tkinter drags in but we never use.
    excludes=[
        "matplotlib", "numpy", "pandas", "scipy", "PIL.ImageQt",
        "test", "unittest", "pydoc_data", "distutils",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PresenceTracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # UPX-packed binaries trip many AV engines
    console=False,            # tray application, no console window
    disable_windowed_traceback=False,
    icon=str(AGENT_ROOT / "build" / "icon.ico")
         if (AGENT_ROOT / "build" / "icon.ico").exists() else None,
    version=str(AGENT_ROOT / "build" / "version_info.txt")
            if (AGENT_ROOT / "build" / "version_info.txt").exists() else None,
)
