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

    @abstractmethod
    def delete_chars(self, count: int) -> bool:
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

    def delete_chars(self, count: int) -> bool:
        if count <= 0:
            return True
        # Batch all backspaces into a single wtype invocation. Spawning one
        # process per char is what made deletion feel sluggish.
        args = ["wtype"]
        for _ in range(count):
            args += ["-k", "BackSpace"]
        return subprocess.run(args, check=False).returncode == 0


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

    def delete_chars(self, count: int) -> bool:
        if count <= 0:
            return True
        return self._run(*("key BackSpace" for _ in range(count)))


class YdotoolOutput(TextOutput):
    name = "ydotool"

    def type_text(self, text: str) -> bool:
        if not text:
            return True
        return subprocess.run(["ydotool", "type", "--", text], check=False).returncode == 0

    def delete_chars(self, count: int) -> bool:
        if count <= 0:
            return True
        # KEY_BACKSPACE = 14. Send all press/release pairs in one call instead
        # of spawning a process per backspace.
        args = ["ydotool", "key"]
        for _ in range(count):
            args += ["14:1", "14:0"]
        return subprocess.run(args, check=False).returncode == 0


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

    def delete_chars(self, count: int) -> bool:
        return count == 0


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


class StreamingTyper:
    """Replace partial dictation text for the current session only."""

    def __init__(self, output: TextOutput, *, append_space: bool = True) -> None:
        self._output = output
        self._append_space = append_space
        self._lock = threading.Lock()
        self._session_text = ""
        self._prepend_space_on_next = False

    @property
    def backend(self) -> str:
        return self._output.name

    @property
    def session_text(self) -> str:
        with self._lock:
            return self._session_text

    def begin_session(self) -> None:
        with self._lock:
            self._session_text = ""

    def reset(self) -> None:
        self.begin_session()

    def _apply_prepend_space(self, text: str) -> str:
        if not self._prepend_space_on_next or not text or text.startswith(" "):
            return text
        self._prepend_space_on_next = False
        return f" {text}"

    @staticmethod
    def _common_prefix_len(a: str, b: str) -> int:
        limit = min(len(a), len(b))
        i = 0
        while i < limit and a[i] == b[i]:
            i += 1
        return i

    def _replace_session_text(self, text: str) -> None:
        text = self._apply_prepend_space(text.strip())
        if text == self._session_text:
            return

        # Only rewrite the part that actually changed: keep the shared prefix
        # in place, delete the diverging tail, then type the new tail. This
        # avoids re-deleting and re-typing the whole sentence on every update.
        shared = self._common_prefix_len(self._session_text, text)
        to_delete = len(self._session_text) - shared
        to_type = text[shared:]

        if to_delete > 0:
            log.debug("Deleting %d trailing chars via %s", to_delete, self._output.name)
            if not self._output.delete_chars(to_delete):
                raise RuntimeError(f"{self._output.name} failed to delete partial text")

        if to_type:
            log.debug("Typing %d chars via %s", len(to_type), self._output.name)
            if not self._output.type_text(to_type):
                raise RuntimeError(f"{self._output.name} failed to type text")

        self._session_text = text

    def update(self, text: str) -> None:
        with self._lock:
            self._replace_session_text(text)

    def finalize(self, text: str) -> None:
        with self._lock:
            self._replace_session_text(text)
            if self._append_space and self._session_text:
                self._prepend_space_on_next = True

    def discard_session(self) -> None:
        with self._lock:
            if not self._session_text:
                self._prepend_space_on_next = False
                return
            log.debug("Discarding %d session chars via %s", len(self._session_text), self._output.name)
            if not self._output.delete_chars(len(self._session_text)):
                raise RuntimeError(f"{self._output.name} failed to discard session text")
            self._session_text = ""
            self._prepend_space_on_next = False
