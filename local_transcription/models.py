from __future__ import annotations

import os
import time
from pathlib import Path

from local_transcription.log import get_logger

log = get_logger("models")

# Pre-converted OpenVINO IR models from Hugging Face (no torch/CUDA needed).
PRECONVERTED_MODELS: dict[str, str] = {
    "openai/whisper-tiny": "OpenVINO/whisper-tiny-fp16-ov",
    "openai/whisper-base": "OpenVINO/whisper-base-fp16-ov",
    "openai/whisper-small": "OpenVINO/whisper-small-fp16-ov",
    "openai/whisper-medium": "OpenVINO/whisper-medium-fp16-ov",
    "openai/whisper-large-v3-turbo": "OpenVINO/whisper-large-v3-turbo-fp16-ov",
    "openai/whisper-turbo": "OpenVINO/whisper-large-v3-turbo-fp16-ov",
    "whisper-tiny": "OpenVINO/whisper-tiny-fp16-ov",
    "whisper-base": "OpenVINO/whisper-base-fp16-ov",
    "whisper-small": "OpenVINO/whisper-small-fp16-ov",
    "whisper-medium": "OpenVINO/whisper-medium-fp16-ov",
    "whisper-turbo": "OpenVINO/whisper-large-v3-turbo-fp16-ov",
    "whisper-large-v3-turbo": "OpenVINO/whisper-large-v3-turbo-fp16-ov",
}

DEFAULT_MODEL = "openai/whisper-turbo"
DEFAULT_MODEL_DIR_NAME = "whisper-turbo"

MODEL_STORAGE_NAMES: dict[str, str] = {
    "openai/whisper-turbo": DEFAULT_MODEL_DIR_NAME,
    "openai/whisper-large-v3-turbo": DEFAULT_MODEL_DIR_NAME,
    "whisper-turbo": DEFAULT_MODEL_DIR_NAME,
    "whisper-large-v3-turbo": DEFAULT_MODEL_DIR_NAME,
}

MODEL_DOWNLOAD_HINTS: dict[str, str] = {
    "whisper-tiny": "~150 MB",
    "whisper-base": "~300 MB",
    "whisper-small": "~500 MB",
    "whisper-medium": "~1.5 GB",
    "whisper-turbo": "~1.6 GB",
    "whisper-large-v3-turbo": "~1.6 GB",
}


def model_storage_dir_name(model: str, repo_id: str | None = None) -> str:
    if model in MODEL_STORAGE_NAMES:
        return MODEL_STORAGE_NAMES[model]
    repo = repo_id or resolve_openvino_repo(model)
    short_name = repo.split("/")[-1].removesuffix("-ov").removesuffix("-fp16")
    if short_name.startswith("whisper-"):
        short_name = short_name.removeprefix("whisper-")
    return f"whisper-{short_name}"


def default_model_dir(model_id: str = DEFAULT_MODEL_DIR_NAME) -> Path:
    if os.environ.get("LT_MODEL_DIR"):
        return Path(os.environ["LT_MODEL_DIR"]).expanduser()
    return (Path.home() / ".local/share/local-transcription/models" / model_id).expanduser()


def model_is_ready(model_dir: Path) -> bool:
    if not model_dir.is_dir():
        log.debug("Model dir missing: %s", model_dir)
        return False
    xml_files = list(model_dir.glob("*.xml"))
    log.debug("Found %d OpenVINO xml file(s) in %s", len(xml_files), model_dir)
    return bool(xml_files)


def resolve_openvino_repo(model: str) -> str:
    if model in PRECONVERTED_MODELS:
        repo = PRECONVERTED_MODELS[model]
        log.info("Resolved model %r -> %s", model, repo)
        return repo
    if model.startswith("OpenVINO/"):
        log.info("Using OpenVINO repo directly: %s", model)
        return model
    raise RuntimeError(
        f"Unknown model '{model}'. Supported:\n"
        + "\n".join(f"  {name}" for name in sorted(PRECONVERTED_MODELS))
    )


def resolve_model_dir(model_dir: Path | None = None, model_id: str = DEFAULT_MODEL_DIR_NAME) -> Path:
    path = (model_dir or default_model_dir(model_id)).expanduser()
    log.info("Checking model at %s", path)
    if not model_is_ready(path):
        raise RuntimeError(
            f"OpenVINO model not found in {path}.\n"
            "Download once (needs internet):\n"
            f"  uv run local-transcription download-model --model {DEFAULT_MODEL}\n"
            "Or set LT_MODEL_DIR to an existing converted model directory."
        )
    log.info("Model OK: %s", path)
    return path


def download_model(
    model: str = DEFAULT_MODEL,
    output_dir: Path | None = None,
) -> Path:
    log.info("download-model started for %r", model)
    repo_id = resolve_openvino_repo(model)
    storage_name = model_storage_dir_name(model, repo_id)
    target = (output_dir or default_model_dir(storage_name)).expanduser()

    log.info("Target directory: %s", target)
    target.mkdir(parents=True, exist_ok=True)

    if model_is_ready(target):
        log.info("Model already present, skipping download")
        return target

    try:
        from huggingface_hub import snapshot_download
        from huggingface_hub.utils import enable_progress_bars
    except ImportError as exc:
        raise RuntimeError("Missing huggingface_hub. Run:\n  uv sync") from exc

    enable_progress_bars()
    size_hint = MODEL_DOWNLOAD_HINTS.get(storage_name, "several hundred MB")
    log.info("Connecting to Hugging Face Hub for %s ...", repo_id)
    log.info("This can take a few minutes on first download (%s)", size_hint)

    started = time.perf_counter()
    try:
        snapshot_download(
            repo_id=repo_id,
            local_dir=str(target),
            local_dir_use_symlinks=False,
        )
    except Exception as exc:
        log.exception("Download failed")
        raise RuntimeError(f"Download failed for {repo_id}: {exc}") from exc

    elapsed = time.perf_counter() - started
    log.info("Download finished in %.1fs", elapsed)

    xml_files = list(target.glob("*.xml"))
    log.info("Downloaded files in %s:", target)
    for path in sorted(target.rglob("*")):
        if path.is_file():
            log.info("  %s (%d bytes)", path.relative_to(target), path.stat().st_size)

    if not model_is_ready(target):
        raise RuntimeError(f"Download finished but no OpenVINO .xml model found in {target}")

    log.info("Model ready: %s (%d xml file(s))", target, len(xml_files))
    return target
