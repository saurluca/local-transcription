from __future__ import annotations

import copy
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
        *,
        num_beams: int = 1,
        final_num_beams: int = 1,
        final_device: str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._language_token = resolve_language_token(language)
        self._device = device
        self._num_beams = num_beams
        self._final_num_beams = final_num_beams
        self._final_device = final_device
        model_path = resolve_model_dir(Path(model_dir))
        self._model_path = model_path

        self._pipe = self._load_pipeline(device)
        self._final_pipe: ov_genai.WhisperPipeline | None = None
        self._final_pipe_config: ov_genai.WhisperGenerationConfig | None = None

        self._fast_config = self._build_config(self._pipe, fast=True, num_beams=num_beams)
        self._final_config = self._build_config(
            self._pipe, fast=False, num_beams=final_num_beams
        )
        lang_label = self._language_token or "auto (detection)"
        if self._use_quality_final:
            log.info(
                "Quality final mode enabled (device=%s, beams=%d)",
                final_device,
                final_num_beams,
            )
        log.info("Transcriber ready (language=%s)", lang_label)

    @property
    def _use_quality_final(self) -> bool:
        if not self._final_device:
            return False
        if self._final_num_beams <= 1:
            return False
        return self._final_device.upper() != self._device.upper()

    def _load_pipeline(self, device: str) -> ov_genai.WhisperPipeline:
        ov_config: dict[str, str] = {}
        if device == "NPU" or "GPU" in device.upper():
            cache_dir = self._model_path.parent / "openvino-cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            ov_config["CACHE_DIR"] = str(cache_dir)
            log.info("OpenVINO compile cache: %s", cache_dir)

        log.info("Loading WhisperPipeline from %s on device=%s", self._model_path, device)
        if "GPU" in device.upper():
            log.info("First GPU load can take 30-60s while the model compiles for Intel Arc")

        started = time.perf_counter()
        try:
            pipe = ov_genai.WhisperPipeline(str(self._model_path), device, **ov_config)
            log.info("Pipeline loaded on %s in %.1fs", device, time.perf_counter() - started)
            return pipe
        except Exception as exc:
            if device.upper() != "CPU":
                log.warning("%s init failed (%s), falling back to CPU", device, exc)
                started = time.perf_counter()
                pipe = ov_genai.WhisperPipeline(str(self._model_path), "CPU")
                log.info("Pipeline loaded on CPU in %.1fs", time.perf_counter() - started)
                return pipe
            log.exception("Failed to load WhisperPipeline")
            raise

    def _build_config(
        self,
        pipe: ov_genai.WhisperPipeline,
        *,
        fast: bool,
        num_beams: int,
    ) -> ov_genai.WhisperGenerationConfig:
        config = pipe.get_generation_config()
        if self._language_token is not None:
            config.language = self._language_token
        config.task = "transcribe"
        config.return_timestamps = False

        if hasattr(config, "num_beams"):
            config.num_beams = num_beams
        if hasattr(config, "max_new_tokens") and fast:
            config.max_new_tokens = 128
        # Curb Whisper's repetition loops ("city of the city of the city ...")
        # that show up on silence/ambiguous audio.
        if hasattr(config, "no_repeat_ngram_size"):
            config.no_repeat_ngram_size = 3

        log.debug(
            "Built %s config (beams=%s)",
            "fast" if fast else "final",
            getattr(config, "num_beams", "?"),
        )
        return config

    def _ensure_quality_final_pipe(self) -> tuple[ov_genai.WhisperPipeline, ov_genai.WhisperGenerationConfig]:
        if self._final_pipe is not None and self._final_pipe_config is not None:
            return self._final_pipe, self._final_pipe_config

        assert self._final_device is not None
        try:
            self._final_pipe = self._load_pipeline(self._final_device)
            self._final_pipe_config = self._build_config(
                self._final_pipe,
                fast=False,
                num_beams=self._final_num_beams,
            )
        except Exception as exc:
            log.warning(
                "Quality final pipeline on %s failed (%s), using primary device",
                self._final_device,
                exc,
            )
            self._final_pipe = self._pipe
            self._final_pipe_config = self._build_config(
                self._pipe,
                fast=False,
                num_beams=1,
            )
        return self._final_pipe, self._final_pipe_config

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
            log.warning("num_beams=%d not supported (%s), retrying with num_beams=1", beams, exc)
            retry_config = copy.copy(config)
            if hasattr(retry_config, "num_beams"):
                retry_config.num_beams = 1
            return str(pipe.generate(speech, retry_config))

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
        if fast:
            pipe, config = self._pipe, self._fast_config
        elif self._use_quality_final:
            pipe, config = self._ensure_quality_final_pipe()
        else:
            pipe, config = self._pipe, self._final_config

        started = time.perf_counter()
        with self._lock:
            result = self._generate(pipe, speech, config)

        text = result.strip()
        log.info(
            "%s transcript in %.2fs: %r",
            mode.capitalize(),
            time.perf_counter() - started,
            text[:120] + ("..." if len(text) > 120 else ""),
        )
        return text
