from __future__ import annotations

import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod

from local_transcription.focus import (
    active_window_class,
    uses_terminal_paste,
)
from local_transcription.log import get_logger

log = get_logger("typer")


class TextOutput(ABC):
    @abstractmethod
    def type_text(self, text: str) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError


def _paste_command(*, shift: bool = False) -> list[str] | None:
    """Return a command that sends Ctrl+V or Ctrl+Shift+V."""
    if shutil.which("wtype"):
        if shift:
            return [
                "wtype",
                "-M",
                "ctrl",
                "-M",
                "shift",
                "-k",
                "v",
                "-m",
                "shift",
                "-m",
                "ctrl",
            ]
        return ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"]
    if shutil.which("ydotool"):
        # ydotool key codes: 29 = leftctrl, 42 = leftshift, 47 = v
        if shift:
            return [
                "ydotool",
                "key",
                "29:1",
                "42:1",
                "47:1",
                "47:0",
                "42:0",
                "29:0",
            ]
        return ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"]
    return None


class ClipboardOutput(TextOutput):
    """Insert text via clipboard paste (Ctrl+V or Ctrl+Shift+V in terminals).

    This is far more reliable than per-character key injection in
    Chromium/Electron apps (browsers, Cursor, ...), where rapid synthetic
    keystrokes get throttled or dropped — causing missing spaces, lost
    focus, or keystrokes leaking into the wrong widget.
    """

    name = "clipboard"

    def __init__(
        self,
        *,
        paste_delay_ms: int = 120,
        restore: bool = True,
        terminal_classes: frozenset[str] | None = None,
    ) -> None:
        self._paste_delay_s = max(paste_delay_ms, 0) / 1000.0
        self._restore = restore
        self._terminal_classes = terminal_classes or frozenset()

    @staticmethod
    def available() -> bool:
        return bool(shutil.which("wl-copy") and _paste_command(shift=False))

    def _read_clipboard(self) -> str | None:
        if not shutil.which("wl-paste"):
            return None
        try:
            result = subprocess.run(
                ["wl-paste", "--no-newline", "--type", "text"],
                capture_output=True,
                check=False,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            log.debug("clipboard snapshot is not UTF-8 text; skipping restore")
            return None

    def type_text(self, text: str) -> bool:
        if not text:
            return True

        window_class = active_window_class()
        use_shift = uses_terminal_paste(window_class, self._terminal_classes)
        paste = _paste_command(shift=use_shift)
        if not shutil.which("wl-copy") or paste is None:
            log.error("clipboard backend requires wl-copy and wtype/ydotool")
            return False

        chord = "ctrl+shift+v" if use_shift else "ctrl+v"
        log.debug(
            "Paste chord %s (class=%s, terminal_classes=%s)",
            chord,
            window_class,
            bool(self._terminal_classes),
        )

        previous = self._read_clipboard() if self._restore else None

        if subprocess.run(["wl-copy", "--", text], check=False).returncode != 0:
            log.error("wl-copy failed to set clipboard")
            return False

        if self._paste_delay_s:
            time.sleep(self._paste_delay_s)

        ok = subprocess.run(paste, check=False).returncode == 0
        if not ok:
            log.error("paste keystroke (%s) failed", paste[0])

        if self._restore and previous is not None:
            # Give the target app a moment to consume the paste before
            # we put the old clipboard content back.
            time.sleep(max(self._paste_delay_s, 0.1))
            subprocess.run(["wl-copy", "--", previous], check=False)

        return ok


class WtypeOutput(TextOutput):
    name = "wtype"

    def type_text(self, text: str) -> bool:
        if not text:
            return True
        return subprocess.run(["wtype", "--", text], check=False).returncode == 0


class DotoolOutput(TextOutput):
    name = "dotool"

    def _run(self, *commands: str) -> bool:
        payload = "\n".join(commands) + "\n"
        result = subprocess.run(
            ["dotool"],
            input=payload,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def type_text(self, text: str) -> bool:
        if not text:
            return True
        escaped = text.replace("\\", "\\\\").replace("\n", "\\n")
        return self._run(f"type {escaped}")


class YdotoolOutput(TextOutput):
    name = "ydotool"

    def type_text(self, text: str) -> bool:
        if not text:
            return True
        return subprocess.run(["ydotool", "type", "--", text], check=False).returncode == 0


def create_output(
    backend: str = "auto",
    *,
    paste_delay_ms: int = 120,
    clipboard_restore: bool = True,
    terminal_classes: frozenset[str] | None = None,
) -> TextOutput:
    order: list[str]
    if backend == "auto":
        order = ["clipboard", "wtype", "dotool", "ydotool"]
    else:
        order = [backend]

    def make_clipboard() -> ClipboardOutput:
        return ClipboardOutput(
            paste_delay_ms=paste_delay_ms,
            restore=clipboard_restore,
            terminal_classes=terminal_classes,
        )

    factories: dict[str, type[TextOutput] | object] = {
        "clipboard": make_clipboard,
        "wtype": WtypeOutput,
        "dotool": DotoolOutput,
        "ydotool": YdotoolOutput,
    }

    for name in order:
        factory = factories.get(name)
        if factory is None:
            continue
        if name == "clipboard":
            if not ClipboardOutput.available():
                log.debug("clipboard backend unavailable (need wl-copy + wtype/ydotool)")
                continue
        elif not shutil.which(name):
            log.debug("Typing backend %s not found in PATH", name)
            continue
        log.info("Using typing backend: %s", name)
        return factory()  # type: ignore[operator]

    raise RuntimeError(
        "No typing backend found. Install one of: wl-clipboard (+ wtype/ydotool), "
        "wtype, dotool, ydotool "
        "(Manjaro: sudo pacman -S wl-clipboard wtype dotool ydotool)."
    )


class DictationTyper:
    """Insert the full transcript once when a recording finishes."""

    def __init__(self, output: TextOutput, *, append_space: bool = True) -> None:
        self._output = output
        self._append_space = append_space
        self._lock = threading.Lock()
        self._prepend_space_on_next = False

    @property
    def backend(self) -> str:
        return self._output.name

    def type_transcript(self, text: str) -> None:
        text = text.strip()
        if not text:
            return

        with self._lock:
            if self._prepend_space_on_next and not text.startswith(" "):
                text = f" {text}"
                self._prepend_space_on_next = False

            log.debug("Typing %d chars via %s", len(text), self._output.name)
            if not self._output.type_text(text):
                raise RuntimeError(f"{self._output.name} failed to type text")

            if self._append_space:
                self._prepend_space_on_next = True
