"""Entry point."""
import sys, traceback
from pathlib import Path

def main():
    try:
        from PyQt6.QtWidgets import QApplication
        from .app import create_app
        app = create_app()
        sys.exit(app.exec())
    except Exception:
        (Path.home() / "lmgrader_crash.log").write_text(traceback.format_exc())
        raise

if __name__ == "__main__":
    main()
