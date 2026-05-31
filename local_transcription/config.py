import os
from dataclasses import dataclass
from pathlib import Path

from local_transcription.models import default_model_dir


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base)
    return Path("/tmp")


@dataclass(frozen=True)
class Settings:
    hf_model: str = os.environ.get("LT_HF_MODEL", "openai/whisper-small")
    model_dir: Path = Path(
        os.environ.get("LT_MODEL_DIR", str(default_model_dir("whisper-small")))
    ).expanduser()
    language: str = os.environ.get("LT_LANGUAGE", "auto")
    device: str = os.environ.get("LT_DEVICE", "GPU")
    sample_rate: int = 16_000
    partial_interval_s: float = float(os.environ.get("LT_PARTIAL_INTERVAL", "0.45"))
    min_partial_audio_s: float = float(os.environ.get("LT_MIN_PARTIAL_AUDIO", "0.8"))
    partial_join_timeout_s: float = float(os.environ.get("LT_PARTIAL_JOIN_TIMEOUT", "10"))
    stream_partials: bool = os.environ.get("LT_STREAM_PARTIALS", "1") != "0"
    append_space: bool = os.environ.get("LT_APPEND_SPACE", "1") != "0"
    typing_backend: str = os.environ.get("LT_TYPING_BACKEND", "auto")
    socket_path: Path = _runtime_dir() / "local-transcription.sock"
    pid_path: Path = _runtime_dir() / "local-transcription.pid"


SETTINGS = Settings()
