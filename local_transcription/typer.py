from __future__ import annotations

import shutil
import subprocess
import threading
from abc import ABC, abstractmethod

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


class ClipboardOutput(TextOutput):
    name = "clipboard"

    def type_text(self, text: str) -> bool:
        if not text:
            return True
        if shutil.which("wl-copy"):
            copy = subprocess.run(["wl-copy", "--", text], check=False)
            paste = subprocess.run(["wtype", "-M", "ctrl", "-k", "v"], check=False)
            return copy.returncode == 0 and paste.returncode == 0
        return False


def create_output(backend: str = "auto") -> TextOutput:
    order: list[str]
    if backend == "auto":
        order = ["wtype", "dotool", "ydotool", "clipboard"]
    else:
        order = [backend]

    factories: dict[str, type[TextOutput]] = {
        "wtype": WtypeOutput,
        "dotool": DotoolOutput,
        "ydotool": YdotoolOutput,
        "clipboard": ClipboardOutput,
    }

    for name in order:
        if name not in factories:
            continue
        if name != "clipboard" and not shutil.which(name):
            log.debug("Typing backend %s not found in PATH", name)
            continue
        log.info("Using typing backend: %s", name)
        return factories[name]()

    raise RuntimeError(
        "No typing backend found. Install one of: wtype, dotool, ydotool "
        "(Manjaro: sudo pacman -S wtype dotool ydotool)."
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
