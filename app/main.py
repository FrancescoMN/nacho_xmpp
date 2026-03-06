from __future__ import annotations

from pathlib import Path
import tkinter as tk

from .gui import MainWindow


def main() -> int:
    root = tk.Tk()
    project_root = Path(__file__).resolve().parents[1]
    MainWindow(root, project_root=project_root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
