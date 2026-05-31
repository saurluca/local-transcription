import os
from dataclasses import dataclass
from pathlib import Path

from local_transcription.models import default_model_dir

ALLOWED_LANGUAGES = frozenset({"auto", "de", "en"})


def normalize_language(language: str) -> str:
    code = language.strip().lower()
    if code not in ALLOWED_LANGUAGES:
        allowed = ", ".join(sorted(ALLOWED_LANGUAGES))
        raise ValueError(
            f"Unsupported language {language!r}; use one of: {allowed}."
        )
    return code


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base)
    return Path("/tmp")


@dataclass(frozen=True)
class Settings:
    hf_model: str = os.environ.get("LT_HF_MODEL", "openai/whisper-turbo")
    model_dir: Path = Path(
        os.environ.get("LT_MODEL_DIR", str(default_model_dir("whisper-turbo")))
    ).expanduser()
    language: str = os.environ.get("LT_LANGUAGE", "auto")
    device: str = os.environ.get("LT_DEVICE", "GPU")
    sample_rate: int = 16_000
    partial_interval_s: float = float(os.environ.get("LT_PARTIAL_INTERVAL", "0.45"))
    min_partial_audio_s: float = float(os.environ.get("LT_MIN_PARTIAL_AUDIO", "0.8"))
    partial_join_timeout_s: float = float(
        os.environ.get("LT_PARTIAL_JOIN_TIMEOUT", "10")
    )
    stream_partials: bool = os.environ.get("LT_STREAM_PARTIALS", "1") != "0"
    partial_window_s: float = float(os.environ.get("LT_PARTIAL_WINDOW", "0"))
    skip_final_if_partial: bool = os.environ.get("LT_SKIP_FINAL_IF_PARTIAL", "1") != "0"
    num_beams: int = int(os.environ.get("LT_NUM_BEAMS", "1"))
    final_num_beams: int = int(
        os.environ.get("LT_FINAL_NUM_BEAMS", os.environ.get("LT_NUM_BEAMS", "1"))
    )
    final_device: str | None = os.environ.get("LT_FINAL_DEVICE") or None
    append_space: bool = os.environ.get("LT_APPEND_SPACE", "1") != "0"
    typing_backend: str = os.environ.get("LT_TYPING_BACKEND", "auto")
    stopping_wait_timeout_s: float = float(os.environ.get("LT_STOPPING_WAIT", "15"))
    socket_path: Path = _runtime_dir() / "local-transcription.sock"
    pid_path: Path = _runtime_dir() / "local-transcription.pid"


SETTINGS = Settings()
