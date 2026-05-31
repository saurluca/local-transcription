from __future__ import annotations

import os
import signal
import socket
import threading
import time

from local_transcription.config import SETTINGS, Settings, normalize_language
from local_transcription.log import get_logger
from local_transcription.overlay import OverlayBackend, OverlayState, create_overlay
from local_transcription.recorder import AudioRecorder
from local_transcription.transcriber import Transcriber
from local_transcription.typer import DictationTyper, create_output

log = get_logger("daemon")


class DictationSession:
    def __init__(self, settings: Settings, overlay: OverlayBackend) -> None:
        self._settings = settings
        self._overlay = overlay
        self._lock = threading.Lock()
        self._state = "idle"
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
        log.info("Session ready")

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _wait_for_idle(self, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None else self._settings.stopping_wait_timeout_s)
        while time.monotonic() < deadline:
            if self.state == "idle":
                return True
            time.sleep(0.05)
        return self.state == "idle"

    def handle(self, command: str) -> str:
        command = command.strip().lower()
        log.info("Received command: %s (state=%s)", command, self.state)

        if command == "status":
            return self.state.upper()
        if command == "toggle":
            if self.state == "stopping":
                if not self._wait_for_idle():
                    return "STOPPING"
            if self.state == "recording":
                self.stop()
                return "IDLE"
            self.start()
            return "RECORDING"
        if command == "start":
            if self.state == "recording":
                return "RECORDING"
            if self.state == "stopping":
                if not self._wait_for_idle():
                    return "STOPPING"
            self.start()
            return "RECORDING"
        if command == "stop":
            if self.state == "stopping":
                if not self._wait_for_idle():
                    return "STOPPING"
                return "IDLE"
            if self.state != "recording":
                return "IDLE"
            self.stop()
            return "IDLE"
        if command == "shutdown":
            if self.state == "stopping":
                self._wait_for_idle()
            elif self.state == "recording":
                self.stop()
            return "SHUTDOWN"

        log.warning("Unknown command: %s", command)
        return f"ERROR unknown command: {command}"

    def start(self) -> None:
        if self.state == "stopping":
            if not self._wait_for_idle():
                log.warning("Start blocked — previous stop still in progress")
                return

        with self._lock:
            if self._state == "recording":
                log.debug("Already recording")
                return
            self._recorder.start()
            self._state = "recording"
            self._set_overlay("recording")
            self._notify("Dictation started")

        log.info("Dictation recording active — speak now")

    def stop(self) -> None:
        with self._lock:
            if self._state != "recording":
                log.debug("Stop ignored, state=%s", self._state)
                return
            self._state = "stopping"

        self._set_overlay("stopping")

        try:
            log.info("Stopping dictation ...")
            self._recorder.stop()
            audio = self._recorder.get_audio()

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
                self._state = "idle"
            self._set_overlay("hidden")
            self._notify("Dictation stopped")
            log.info("Dictation idle")

    def _set_overlay(self, state: OverlayState) -> None:
        self._overlay.set_state(state)

    def _notify(self, message: str) -> None:
        if not self._settings.notify:
            return
        try:
            import subprocess

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
        if self._session and self._session.state == "recording":
            self._session.stop()
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
