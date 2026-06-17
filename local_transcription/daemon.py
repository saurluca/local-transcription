from __future__ import annotations

import os
import queue
import signal
import socket
import subprocess
import threading
import time

import numpy as np
from numpy.typing import NDArray

from local_transcription.config import SETTINGS, Settings, normalize_language
from local_transcription.log import get_logger
from local_transcription.overlay import OverlayBackend, OverlayState, create_overlay
from local_transcription.recorder import AudioRecorder
from local_transcription.transcriber import Transcriber
from local_transcription.typer import DictationTyper, create_output

log = get_logger("daemon")

_SHUTDOWN_DRAIN_TIMEOUT_S = 120.0


class DictationSession:
    def __init__(self, settings: Settings, overlay: OverlayBackend) -> None:
        self._settings = settings
        self._overlay = overlay
        self._lock = threading.Lock()
        self._state = "idle"
        self._pending = 0
        self._jobs: queue.Queue[NDArray[np.float32] | None] = queue.Queue()
        log.info("Initializing dictation session")
        log.info("Model dir: %s", settings.model_dir)
        language = normalize_language(settings.language)
        log.info("Device: %s, language: %s", settings.device, language)

        self._recorder = AudioRecorder(settings.sample_rate)
        self._transcriber = Transcriber(
            model_dir=settings.model_dir,
            device=settings.device,
            language=language,
            num_beams=settings.num_beams,
        )
        self._typer = DictationTyper(
            create_output(
                settings.typing_backend,
                paste_delay_ms=settings.paste_delay_ms,
                clipboard_restore=settings.clipboard_restore,
                terminal_classes=settings.terminal_classes,
            ),
            append_space=settings.append_space,
        )
        log.info("Typing backend: %s", self._typer.backend)
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="dictation-worker",
            daemon=True,
        )
        self._worker.start()
        log.info("Session ready")

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _status_response(self) -> str:
        with self._lock:
            if self._state == "recording":
                return "RECORDING"
            if self._pending > 0:
                return "TRANSCRIBING"
            return "IDLE"

    def handle(self, command: str) -> str:
        command = command.strip().lower()
        log.info("Received command: %s (state=%s)", command, self._status_response())

        if command == "status":
            return self._status_response()
        if command == "toggle":
            if self.state == "recording":
                self.stop()
                return "TRANSCRIBING"
            self.start()
            return "RECORDING"
        if command == "start":
            if self.state == "recording":
                return "RECORDING"
            self.start()
            return "RECORDING"
        if command == "stop":
            if self.state != "recording":
                return "IDLE"
            self.stop()
            return "TRANSCRIBING"
        if command == "shutdown":
            if self.state == "recording":
                self.stop()
            self.shutdown()
            return "SHUTDOWN"

        log.warning("Unknown command: %s", command)
        return f"ERROR unknown command: {command}"

    def start(self) -> None:
        with self._lock:
            if self._state == "recording":
                log.debug("Already recording")
                return
            self._recorder.start()
            self._state = "recording"

        self._refresh_overlay()
        self._notify("Dictation started")
        log.info("Dictation recording active — speak now")

    def stop(self) -> None:
        with self._lock:
            if self._state != "recording":
                log.debug("Stop ignored, state=%s", self._state)
                return
            log.info("Stopping dictation ...")
            self._recorder.stop()
            audio = self._recorder.get_audio()
            self._state = "idle"
            self._pending += 1

        self._jobs.put(audio)
        self._refresh_overlay()
        self._notify("Dictation stopped")
        log.info("Dictation idle (transcription queued)")

    def shutdown(self, timeout: float = _SHUTDOWN_DRAIN_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                pending = self._pending
            if pending == 0:
                break
            time.sleep(0.05)

        with self._lock:
            pending = self._pending
        if pending > 0:
            log.warning(
                "Shutting down with %d transcription job(s) still pending after %.0fs",
                pending,
                timeout,
            )

        self._jobs.put(None)
        self._worker.join(timeout=max(0.0, deadline - time.monotonic()))

    def _worker_loop(self) -> None:
        while True:
            audio = self._jobs.get()
            if audio is None:
                self._jobs.task_done()
                break

            try:
                final_text = ""
                log.info("Running transcription ...")
                try:
                    final_text = self._transcriber.transcribe(audio)
                except Exception as exc:
                    log.exception("Transcription failed: %s", exc)

                try:
                    if final_text:
                        log.info("Typing transcript (%d chars)", len(final_text))
                        self._typer.type_transcript(final_text)
                    else:
                        log.warning("No speech detected in recording")
                except RuntimeError as exc:
                    log.error("Failed to type transcript: %s", exc)
            finally:
                with self._lock:
                    self._pending -= 1
                self._refresh_overlay()
                self._jobs.task_done()

    def _refresh_overlay(self) -> None:
        with self._lock:
            recording = self._state == "recording"
            pending = self._pending

        if recording:
            overlay_state: OverlayState = "recording"
        elif pending > 0:
            overlay_state = "stopping"
        else:
            overlay_state = "hidden"

        self._set_overlay(overlay_state)

    def _set_overlay(self, state: OverlayState) -> None:
        self._overlay.set_state(state)

    def _notify(self, message: str) -> None:
        if not self._settings.notify:
            return
        try:
            subprocess.run(
                ["notify-send", "-a", "local-transcription", "Dictation", message],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            log.debug("notify-send not available")


class DictationDaemon:
    def __init__(self, settings: Settings = SETTINGS) -> None:
        self._settings = settings
        self._session: DictationSession | None = None
        self._overlay: OverlayBackend | None = None
        self._server: socket.socket | None = None
        self._running = False

    def run(self) -> int:
        log.info("Starting dictation daemon")
        log.info("Socket: %s", self._settings.socket_path)
        log.info("PID file: %s", self._settings.pid_path)

        if self._pid_running():
            log.error("Daemon already running")
            return 1

        self._write_pid()
        try:
            normalize_language(self._settings.language)
        except ValueError as exc:
            log.error("%s", exc)
            if self._settings.pid_path.exists():
                self._settings.pid_path.unlink(missing_ok=True)
            return 1

        log.info("Loading speech model (this may take a while on first run) ...")
        self._overlay = create_overlay(
            enabled=self._settings.overlay,
            margin_bottom=self._settings.overlay_margin_bottom,
        )
        started = time.perf_counter()
        self._session = DictationSession(self._settings, self._overlay)
        log.info("Startup completed in %.1fs", time.perf_counter() - started)

        if self._settings.socket_path.exists():
            log.warning("Removing stale socket file")
            self._settings.socket_path.unlink()

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self._settings.socket_path))
        self._server.listen(5)
        self._running = True

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        log.info("Daemon ready on %s", self._settings.device)
        log.info("Hyprland bind: bind = SUPER, V, exec, uv run local-transcription toggle")
        log.info("Waiting for commands ...")

        try:
            while self._running:
                conn, _addr = self._server.accept()
                threading.Thread(
                    target=self._handle_client,
                    args=(conn,),
                    daemon=True,
                ).start()
        finally:
            self._cleanup()

        return 0

    def _handle_client(self, conn: socket.socket) -> None:
        with conn:
            data = conn.recv(1024).decode("utf-8", errors="replace").strip()
            log.debug("Client connected, payload=%r", data)
            if not self._session:
                conn.sendall(b"ERROR daemon not ready\n")
                return
            response = self._session.handle(data)
            if response == "SHUTDOWN":
                log.info("Shutdown requested")
                conn.sendall(b"SHUTDOWN\n")
                self._running = False
                if self._server:
                    self._server.close()
                return
            conn.sendall(f"{response}\n".encode("utf-8"))

    def _handle_signal(self, signum: int, _frame: object) -> None:
        log.info("Received signal %s, shutting down", signum)
        self._running = False
        if self._server:
            self._server.close()

    def _cleanup(self) -> None:
        log.info("Cleaning up daemon")
        if self._session:
            if self._session.state == "recording":
                self._session.stop()
            self._session.shutdown()
        if self._overlay:
            self._overlay.stop()
            self._overlay = None
        if self._settings.socket_path.exists():
            self._settings.socket_path.unlink()
        if self._settings.pid_path.exists():
            self._settings.pid_path.unlink()
        log.info("Daemon stopped")

    def _write_pid(self) -> None:
        self._settings.pid_path.write_text(str(os.getpid()))
        log.debug("Wrote PID %s", os.getpid())

    def _pid_running(self) -> bool:
        if not self._settings.pid_path.exists():
            return False
        try:
            pid = int(self._settings.pid_path.read_text().strip())
        except ValueError:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            self._settings.pid_path.unlink(missing_ok=True)
            return False
        log.warning("Existing daemon PID %s is still running", pid)
        return True


def send_command(command: str, settings: Settings = SETTINGS) -> str:
    if not settings.socket_path.exists():
        raise RuntimeError(
            "Daemon not running. Start with:\n  uv run local-transcription daemon"
        )

    log.debug("Connecting to socket %s", settings.socket_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(120)
        sock.connect(str(settings.socket_path))
        sock.sendall(command.encode("utf-8"))
        return sock.recv(4096).decode("utf-8", errors="replace").strip()
