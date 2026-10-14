"""Puzzle Pieces — by jwdfir. Launcher for the GUI (also the PyInstaller entry point)."""

import multiprocessing
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui import main  # noqa: E402

if __name__ == "__main__":
    multiprocessing.freeze_support()  # required for background worker processes in the packaged .exe
    raise SystemExit(main(sys.argv[1:]))
