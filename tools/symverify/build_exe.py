"""Bundle Symmetism Coherence into a single Windows .exe.

Produces `dist/Symmetism.exe` (windowed, no console) by running
PyInstaller against `src/symverify/__main_gui__.py`. The output is a
standalone double-clickable ~30–40 MB executable that pulls in the
entire Python runtime + customtkinter + symverify modules.

Usage:
  pip install symverify[exe]   # ensures customtkinter + pyinstaller
  python build_exe.py          # builds dist/Symmetism.exe

Requirements at runtime: only that the user's ~/.symmetism/{config,
secrets,state} directories already exist with valid contents. The exe
DOES NOT carry the daemon's Scheduled Task — install via
`sym install-service` from the same exe's Settings panel, or from
the bundled CLI `sym` if pip-installed.

Why this approach:
  - PyInstaller is the most robust single-file packager for Python
    GUI apps on Windows. py2exe, cx_Freeze are alternatives; we picked
    PyInstaller for the active maintainer + tk hidden-import support.
  - --windowed (no console) is the default for double-click UX.
  - --onefile produces a single .exe; trade-off: ~3 s startup as the
    bootstrap unpacks to %TEMP%. Acceptable for our usage profile.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
ENTRY = SRC / "symverify" / "__main_gui__.py"
DIST = HERE / "dist"
BUILD = HERE / "build"
SPEC_FILE = HERE / "Symmetism.spec"


def main() -> int:
    if not ENTRY.is_file():
        print(f"[error] entry point missing: {ENTRY}", file=sys.stderr)
        return 1

    # Wipe previous build outputs so PyInstaller doesn't pick up stale
    # bytecode from a partial earlier run.
    for d in (DIST, BUILD):
        if d.exists():
            shutil.rmtree(d)
    if SPEC_FILE.is_file():
        SPEC_FILE.unlink()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "Symmetism",
        "--onefile",                 # single .exe
        "--windowed",                # no console window
        "--noconfirm",
        # Hidden imports — customtkinter relies on dynamic class loading
        # that PyInstaller's static analyzer can miss. Be explicit.
        "--hidden-import", "customtkinter",
        "--collect-all", "customtkinter",
        # symverify drags in apscheduler + watchdog + openai; collect
        # the lot so the bundled exe runs even when the daemon's
        # heartbeat scheduler imports its plugins lazily.
        "--collect-submodules", "symverify",
        "--collect-submodules", "apscheduler",
        "--collect-submodules", "watchdog",
        "--collect-submodules", "openai",
        # SQLite is in stdlib but PyInstaller still needs the binding
        # collected. Also rich + click for the in-process narrate path.
        "--collect-submodules", "sqlite3",
        # Place outputs predictably.
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(HERE),
        # Add `src/` to PYTHONPATH so the entry point resolves
        # `from symverify import gui` even though the package wasn't
        # `pip install -e`'d in the build environment.
        "--paths", str(SRC),
        str(ENTRY),
    ]

    print(f"running: {' '.join(cmd)}")
    res = subprocess.run(cmd)
    if res.returncode != 0:
        print(f"[error] PyInstaller exited {res.returncode}", file=sys.stderr)
        return res.returncode

    exe = DIST / "Symmetism.exe"
    if not exe.is_file():
        print(f"[error] expected output missing: {exe}", file=sys.stderr)
        return 1

    size_mb = exe.stat().st_size / (1024 * 1024)
    # Reconfigure stdout so the success line works on cp1252 consoles.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print()
    print(f"  OK  {exe}  ({size_mb:.1f} MB)")
    print(f"      pin to taskbar or copy to Desktop; double-click to run.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
