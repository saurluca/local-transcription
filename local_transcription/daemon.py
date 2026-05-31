from __future__ import annotations

import os
import signal
import socket
import threading
import time

import numpy as np

from local_transcription.config import SETTINGS, Settings
from local_transcription.log import get_logger
from local_transcription.recorder import AudioRecorder
from local_transcription.transcriber import Transcriber
from local_transcription.typer import StreamingTyper, create_output

log = get_logger("daemon")


class DictationSession:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._state = "idle"
        log.info("Initializing dictation session")
        log.info("Model dir: %s", settings.model_dir)
        log.info("Device: %s, language: %s", settings.device, settings.language)

        self._recorder = AudioRecorder(settings.sample_rate)
        self._transcriber = Transcriber(
            model_dir=settings.model_dir,
            device=settings.device,
            language=settings.language,
            num_beams=settings.num_beams,
            final_num_beams=settings.final_num_beams,
            final_device=settings.final_device,
        )
        self._typer = StreamingTyper(
            create_output(settings.typing_backend),
            append_space=settings.append_space,
        )
        self._stop_partial = threading.Event()
        self._partial_busy = threading.Event()
        self._partial_thread: threading.Thread | None = None
        self._last_partial_text = ""
        self._last_partial_sample_count = 0
        self._partial_prefix_text = ""
        self._partial_prefix_samples = 0
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
            self._last_partial_text = ""
            self._last_partial_sample_count = 0
            self._partial_prefix_text = ""
            self._partial_prefix_samples = 0
            self._typer.begin_session()
            self._recorder.start()
            self._state = "recording"
            self._notify("Dictation started")
            if self._settings.stream_partials:
                self._stop_partial.clear()
                self._partial_thread = threading.Thread(
                    target=self._partial_loop,
                    daemon=True,
                    name="partial-transcription",
                )
                self._partial_thread.start()
                window = self._settings.partial_window_s
                window_label = "full audio" if window <= 0 else f"{window:.2f}s window"
                log.info(
                    "Partial streaming enabled (every %.2fs, %s)",
                    self._settings.partial_interval_s,
                    window_label,
                )
            else:
                log.info("Partial streaming disabled")

        log.info("Dictation recording active — speak now")

    @staticmethod
    def _audio_is_silent(audio: np.ndarray, *, rms_threshold: float = 0.008) -> bool:
        if audio.size == 0:
            return True
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
        return rms < rms_threshold

    @staticmethod
    def _merge_partial_text(prefix: str, tail: str) -> str:
        prefix = prefix.strip()
        tail = tail.strip()
        if not prefix:
            return tail
        if not tail:
            return prefix
        if tail in prefix:
            return prefix
        if prefix in tail:
            return tail

        prefix_words = prefix.split()
        tail_words = tail.split()
        max_overlap = min(len(prefix_words), len(tail_words))
        for overlap in range(max_overlap, 0, -1):
            if prefix_words[-overlap:] == tail_words[:overlap]:
                return " ".join(prefix_words + tail_words[overlap:])
        return f"{prefix} {tail}"

    def _transcribe_partial(self, full_audio: np.ndarray) -> str:
        window_samples = int(self._settings.partial_window_s * self._settings.sample_rate)
        # Window disabled (<= 0): always transcribe the full buffer. On a fast GPU
        # this is just as quick and keeps language/context coherent. The windowed
        # path is only worthwhile on slow CPU setups with long recordings.
        if window_samples <= 0 or len(full_audio) <= window_samples:
            return self._transcriber.transcribe(full_audio, fast=True)

        prefix_end = len(full_audio) - window_samples
        refresh_threshold = window_samples // 2
        if prefix_end > self._partial_prefix_samples + refresh_threshold:
            prefix_audio = full_audio[:prefix_end]
            self._partial_prefix_text = self._transcriber.transcribe(prefix_audio, fast=True)
            self._partial_prefix_samples = prefix_end
            log.debug(
                "Refreshed partial prefix (%.2fs audio): %r",
                prefix_end / self._settings.sample_rate,
                self._partial_prefix_text[:80],
            )

        tail_audio = full_audio[prefix_end:]
        tail_text = self._transcriber.transcribe(tail_audio, fast=True)
        return self._merge_partial_text(self._partial_prefix_text, tail_text)

    def _should_skip_final(self, audio: np.ndarray) -> bool:
        if not self._settings.stream_partials:
            return False
        if not self._settings.skip_final_if_partial:
            return False
        if not self._last_partial_text:
            return False
        if self._last_partial_text != self._typer.session_text:
            return False

        current_samples = len(audio)
        if current_samples <= self._last_partial_sample_count:
            return True

        trailing = audio[self._last_partial_sample_count :]
        return self._audio_is_silent(trailing)

    def stop(self) -> None:
        with self._lock:
            if self._state != "recording":
                log.debug("Stop ignored, state=%s", self._state)
                return
            self._state = "stopping"

        try:
            log.info("Stopping dictation ...")
            self._stop_partial.set()
            if self._partial_thread and self._partial_thread.is_alive():
                self._partial_thread.join(timeout=self._settings.partial_join_timeout_s)
            if self._partial_busy.is_set():
                log.debug("Waiting for in-flight partial transcription")
                self._partial_busy.wait(timeout=self._settings.partial_join_timeout_s)

            self._recorder.stop()
            audio = self._recorder.get_audio()

            final_text = ""
            if self._should_skip_final(audio):
                log.info("Skipping final pass — partial already up to date")
                final_text = self._last_partial_text
            else:
                log.info("Running final transcription ...")
                try:
                    final_text = self._transcriber.transcribe(audio, fast=False)
                except Exception as exc:
                    log.exception("Final transcription failed: %s", exc)
                    if self._last_partial_text:
                        log.info("Keeping last partial transcript after failure")
                        final_text = self._last_partial_text

            try:
                if final_text:
                    log.info("Typing final transcript (%d chars)", len(final_text))
                    self._typer.finalize(final_text)
                else:
                    log.warning("No speech detected in recording")
                    self._typer.discard_session()
            except RuntimeError as exc:
                log.error("Failed to type transcript: %s", exc)
        finally:
            with self._lock:
                self._state = "idle"
            self._notify("Dictation stopped")
            log.info("Dictation idle")

    def _partial_loop(self) -> None:
        log.debug("Partial transcription thread started")
        last_sent = ""
        cycle = 0
        while not self._stop_partial.is_set():
            time.sleep(self._settings.partial_interval_s)
            cycle += 1
            duration = self._recorder.duration_s()
            if duration < self._settings.min_partial_audio_s:
                log.debug(
                    "Partial cycle %d: waiting for audio (%.2fs / %.2fs needed)",
                    cycle,
                    duration,
                    self._settings.min_partial_audio_s,
                )
                continue

            window_s = min(duration, self._settings.partial_window_s)
            log.debug("Partial cycle %d: transcribing (window up to %.2fs)", cycle, window_s)

            self._partial_busy.set()
            try:
                full_audio = self._recorder.get_audio()
                partial = self._transcribe_partial(full_audio)
                self._last_partial_sample_count = len(full_audio)
                if self._stop_partial.is_set():
                    log.debug("Partial cycle %d: stop requested after transcribe", cycle)
                    break
                if not partial or partial == last_sent:
                    log.debug("Partial cycle %d: no new text", cycle)
                    continue

                try:
                    log.info("Partial update: %r", partial)
                    self._typer.update(partial)
                    last_sent = partial
                    self._last_partial_text = partial
                except RuntimeError as exc:
                    log.error("Partial typing failed: %s", exc)
            finally:
                self._partial_busy.clear()

        log.debug("Partial transcription thread stopped")

    @staticmethod
    def _notify(message: str) -> None:
        if os.environ.get("LT_NOTIFY", "1") == "0":
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
        log.info("Loading speech model (this may take a while on first run) ...")
        started = time.perf_counter()
        self._session = DictationSession(self._settings)
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
