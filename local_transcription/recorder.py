from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd

from local_transcription.log import get_logger

if TYPE_CHECKING:
    from numpy.typing import NDArray

log = get_logger("recorder")


class AudioRecorder:
    def __init__(self, sample_rate: int = 16_000) -> None:
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._chunks: list[NDArray[np.float32]] = []
        self._stream: sd.InputStream | None = None
        self._recording = False
        log.debug("AudioRecorder initialized (sample_rate=%d)", sample_rate)

    @property
    def is_recording(self) -> bool:
        return self._recording

    def _callback(
        self,
        indata: NDArray[np.float32],
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            log.warning("Audio callback status: %s", status)
        with self._lock:
            if self._recording:
                self._chunks.append(indata.copy())

    def start(self) -> None:
        if self._recording:
            log.debug("Recorder already running")
            return
        with self._lock:
            self._chunks.clear()
        self._recording = True
        default_input = sd.query_devices(kind="input")
        log.info(
            "Starting microphone: %s (%.0f Hz, %d channel(s))",
            default_input["name"],
            default_input["default_samplerate"],
            default_input["max_input_channels"],
        )
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()
        log.info("Recording started")

    def stop(self) -> None:
        if not self._recording:
            log.debug("Recorder already stopped")
            return
        self._recording = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        log.info("Recording stopped (captured %.2fs)", self.duration_s())

    def get_audio(self) -> NDArray[np.float32]:
        with self._lock:
            if not self._chunks:
                return np.array([], dtype=np.float32)
            return np.concatenate(self._chunks, axis=0).flatten()

    def get_audio_tail(self, seconds: float) -> NDArray[np.float32]:
        audio = self.get_audio()
        if audio.size == 0:
            return audio
        max_samples = max(1, int(seconds * self.sample_rate))
        if len(audio) <= max_samples:
            return audio
        return audio[-max_samples:]

    def duration_s(self) -> float:
        return len(self.get_audio()) / self.sample_rate
