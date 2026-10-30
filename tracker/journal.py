"""Journal: start a Toggl journaling timer and bring the notes app forward on the configured desktop."""
from __future__ import annotations

import logging
import subprocess
import webbrowser

from . import config

log = logging.getLogger("journal")
# Mission Control "Switch to Desktop N" (ctrl+N) key codes for the number row
KEYCODES = {1: 18, 2: 19, 3: 20, 4: 21, 5: 23, 6: 22, 7: 26, 8: 28, 9: 25}


def open_notes_app() -> str | None:
    cfg = config.load()["journal"]
    app = cfg.get("app") or "Logseq"
    desktop = int(cfg.get("desktop") or 0)
    errors = []
    if desktop in KEYCODES:
        try:   # needs Accessibility permission for the terminal/app, and the shortcut enabled in Keyboard settings
            subprocess.run(["osascript", "-e",
                            f'tell application "System Events" to key code {KEYCODES[desktop]} using control down'],
                           check=True, capture_output=True, timeout=5)
        except Exception as e:
            errors.append(f"desktop switch: {e}")
    try:
        if app.startswith("http"):
            webbrowser.open(app)
        else:
            subprocess.run(["open", "-a", app], check=True, capture_output=True, timeout=10)
    except Exception as e:
        errors.append(f"open {app}: {e}")
    return "; ".join(errors) or None
