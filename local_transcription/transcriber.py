from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import openvino_genai as ov_genai

from local_transcription.log import get_logger
from local_transcription.models import resolve_model_dir

if TYPE_CHECKING:
    from numpy.typing import NDArray

log = get_logger("transcriber")


def resolve_language_token(language: str) -> str | None:
    code = language.strip().lower()
    if code in ("auto", "", "none"):
        return None
    return f"<|{code}|>"


class Transcriber:
    """Offline Whisper inference via OpenVINO (Intel CPU/GPU/NPU)."""

    def __init__(
        self,
        model_dir: Path | str,
        device: str = "GPU",
        language: str = "auto",
    ) -> None:
        self._lock = threading.Lock()
        self._language_token = resolve_language_token(language)
        model_path = resolve_model_dir(Path(model_dir))

        ov_config: dict[str, str] = {}
        if device == "NPU" or "GPU" in device.upper():
            cache_dir = model_path.parent / "openvino-cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            ov_config["CACHE_DIR"] = str(cache_dir)
            log.info("OpenVINO compile cache: %s", cache_dir)

        log.info("Loading WhisperPipeline from %s on device=%s", model_path, device)
        log.info("First GPU load can take 30-60s while the model compiles for Intel Arc")

        started = time.perf_counter()
        try:
            self._pipe = ov_genai.WhisperPipeline(str(model_path), device, **ov_config)
            log.info("Pipeline loaded on %s in %.1fs", device, time.perf_counter() - started)
        except Exception as exc:
            if device.upper() != "CPU":
                log.warning("GPU init failed (%s), falling back to CPU", exc)
                started = time.perf_counter()
                self._pipe = ov_genai.WhisperPipeline(str(model_path), "CPU")
                log.info("Pipeline loaded on CPU in %.1fs", time.perf_counter() - started)
            else:
                log.exception("Failed to load WhisperPipeline")
                raise

        self._fast_config = self._build_config(fast=True)
        self._final_config = self._build_config(fast=False)
        lang_label = self._language_token or "auto (detection)"
        log.info("Transcriber ready (language=%s)", lang_label)

    def _build_config(self, *, fast: bool) -> ov_genai.WhisperGenerationConfig:
        config = self._pipe.get_generation_config()
        if self._language_token is not None:
            config.language = self._language_token
        config.task = "transcribe"
        config.return_timestamps = False

        if hasattr(config, "num_beams"):
            config.num_beams = 1 if fast else 5
        if hasattr(config, "max_new_tokens") and fast:
            config.max_new_tokens = 128

        log.debug("Built %s config (beams=%s)", "fast" if fast else "final", getattr(config, "num_beams", "?"))
        return config

    def transcribe(
        self,
        audio: NDArray[np.float32],
        *,
        fast: bool = False,
    ) -> str:
        if audio.size == 0:
            log.debug("Skipping empty audio buffer")
            return ""

        mode = "partial" if fast else "final"
        duration_s = len(audio) / 16_000
        log.info("Transcribing %.2fs audio (%s pass)", duration_s, mode)

        speech = audio.astype(np.float32, copy=False).flatten().tolist()
        config = self._fast_config if fast else self._final_config

        started = time.perf_counter()
        with self._lock:
            result = self._pipe.generate(speech, config)

        text = str(result).strip()
        log.info(
            "%s transcript in %.2fs: %r",
            mode.capitalize(),
            time.perf_counter() - started,
            text[:120] + ("..." if len(text) > 120 else ""),
        )
        return text
