from __future__ import annotations

import json
import subprocess

from local_transcription.log import get_logger

log = get_logger("focus")

DEFAULT_TERMINAL_CLASSES = (
    "Alacritty,kitty,foot,wezterm,ghostty,org.gnome.Terminal,konsole,Terminator"
)


def parse_terminal_classes(raw: str | None) -> frozenset[str]:
    """Parse LT_TERMINAL_CLASSES (comma-separated). Empty string disables detection."""
    if raw is None:
        raw = DEFAULT_TERMINAL_CLASSES
    raw = raw.strip()
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def active_window_class() -> str | None:
    """Return Hyprland active window class, or None if hyprctl fails."""
    try:
        result = subprocess.run(
            ["hyprctl", "activewindow", "-j"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("hyprctl activewindow failed: %s", exc)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        log.debug("hyprctl activewindow failed: rc=%s", result.returncode)
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.debug("hyprctl activewindow returned invalid JSON")
        return None
    window_class = data.get("class")
    if not window_class:
        log.debug("hyprctl activewindow missing class field")
        return None
    return str(window_class)


def uses_terminal_paste(
    class_name: str | None,
    terminal_classes: frozenset[str],
) -> bool:
    """True when the focused window should receive Ctrl+Shift+V paste."""
    if not terminal_classes or not class_name:
        return False
    lowered = class_name.casefold()
    return any(lowered == tc.casefold() for tc in terminal_classes)
