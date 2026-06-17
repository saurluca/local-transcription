from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import openvino_genai as ov_genai

from local_transcription.config import normalize_language
from local_transcription.log import get_logger
from local_transcription.models import resolve_model_dir

if TYPE_CHECKING:
    from numpy.typing import NDArray

log = get_logger("transcriber")


def resolve_language_token(language: str) -> str | None:
    code = normalize_language(language)
    if code == "auto":
        return None
    return f"<|{code}|>"


class Transcriber:
    """Offline Whisper inference via OpenVINO (Intel CPU/GPU/NPU)."""

    def __init__(
        self,
        model_dir: Path | str,
        device: str = "GPU",
        language: str = "auto",
        *,
        num_beams: int = 1,
    ) -> None:
        self._lock = threading.Lock()
        self._language_token = resolve_language_token(language)
        self._device = device
        self._num_beams = num_beams
        model_path = resolve_model_dir(Path(model_dir))
        self._model_path = model_path

        self._pipe = self._load_pipeline(device)
        self._config = self._build_config(self._pipe, num_beams=num_beams)
        lang_label = self._language_token or "auto (detection)"
        log.info("Transcriber ready (language=%s)", lang_label)

    def _load_pipeline(self, device: str) -> ov_genai.WhisperPipeline:
        ov_config: dict[str, str] = {}
        if device == "NPU" or "GPU" in device.upper():
            cache_dir = self._model_path.parent / "openvino-cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            ov_config["CACHE_DIR"] = str(cache_dir)
            log.info("OpenVINO compile cache: %s", cache_dir)

        log.info(
            "Loading WhisperPipeline from %s on device=%s", self._model_path, device
        )
        if "GPU" in device.upper():
            log.info(
                "First GPU load can take 30-60s while the model compiles for Intel Arc"
            )

        started = time.perf_counter()
        try:
            pipe = ov_genai.WhisperPipeline(str(self._model_path), device, **ov_config)
            log.info(
                "Pipeline loaded on %s in %.1fs", device, time.perf_counter() - started
            )
            return pipe
        except Exception as exc:
            if device.upper() != "CPU":
                log.warning("%s init failed (%s), falling back to CPU", device, exc)
                started = time.perf_counter()
                pipe = ov_genai.WhisperPipeline(str(self._model_path), "CPU")
                log.info(
                    "Pipeline loaded on CPU in %.1fs", time.perf_counter() - started
                )
                return pipe
            log.exception("Failed to load WhisperPipeline")
            raise

    def _build_config(
        self,
        pipe: ov_genai.WhisperPipeline,
        *,
        num_beams: int,
    ) -> ov_genai.WhisperGenerationConfig:
        config = pipe.get_generation_config()
        if self._language_token is not None:
            config.language = self._language_token
        config.task = "transcribe"
        config.return_timestamps = False

        if hasattr(config, "num_beams"):
            config.num_beams = num_beams
        if hasattr(config, "no_repeat_ngram_size"):
            config.no_repeat_ngram_size = 3

        log.debug("Built generation config (beams=%s)", getattr(config, "num_beams", "?"))
        return config

    def _generate(
        self,
        pipe: ov_genai.WhisperPipeline,
        speech: list[float],
        config: ov_genai.WhisperGenerationConfig,
    ) -> str:
        beams = getattr(config, "num_beams", 1)
        try:
            return str(pipe.generate(speech, config))
        except RuntimeError as exc:
            if beams <= 1 or "not implemented" not in str(exc).lower():
                raise
            log.warning(
                "num_beams=%d not supported (%s), retrying with num_beams=1", beams, exc
            )
            retry_config = copy.copy(config)
            if hasattr(retry_config, "num_beams"):
                retry_config.num_beams = 1
            return str(pipe.generate(speech, retry_config))

    def transcribe(self, audio: NDArray[np.float32]) -> str:
        if audio.size == 0:
            log.debug("Skipping empty audio buffer")
            return ""

        duration_s = len(audio) / 16_000
        log.info("Transcribing %.2fs audio", duration_s)

        speech = audio.astype(np.float32, copy=False).flatten().tolist()

        started = time.perf_counter()
        with self._lock:
            result = self._generate(self._pipe, speech, self._config)

        text = result.strip()
        log.info(
            "Transcript in %.2fs: %r",
            time.perf_counter() - started,
            text,
        )
        return text
