"""Entry point for the bundled Symmetism.exe.

`python -m symverify.__main_gui__` and the PyInstaller bundle both call
this. Kept as its own module (not gui.main) so PyInstaller has a clean
top-level entry point that doesn't go through click — click + PyInstaller
sometimes interact badly when sys.argv is empty.
"""

from __future__ import annotations


def main() -> None:
    # Import inside main so any import-time error is caught and shown
    # in a message box instead of vanishing into the void on a Windows
    # GUI subsystem exe with no console.
    try:
        from symverify import gui
        gui.main()
    except Exception as e:
        import traceback
        msg = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
        try:
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror("Symmetism — startup failed", msg)
        except Exception:
            # Fall back to printing — only useful if launched from a console.
            print(msg)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
