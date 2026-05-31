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
    "whisper-tiny": "OpenVINO/whisper-tiny-fp16-ov",
    "whisper-base": "OpenVINO/whisper-base-fp16-ov",
    "whisper-small": "OpenVINO/whisper-small-fp16-ov",
    "whisper-medium": "OpenVINO/whisper-medium-fp16-ov",
}


def default_model_dir(model_id: str = "whisper-small") -> Path:
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


def resolve_model_dir(model_dir: Path | None = None, model_id: str = "whisper-small") -> Path:
    path = (model_dir or default_model_dir(model_id)).expanduser()
    log.info("Checking model at %s", path)
    if not model_is_ready(path):
        raise RuntimeError(
            f"OpenVINO model not found in {path}.\n"
            "Download once (needs internet):\n"
            "  uv run local-transcription download-model --model openai/whisper-small\n"
            "Or set LT_MODEL_DIR to an existing converted model directory."
        )
    log.info("Model OK: %s", path)
    return path


def download_model(
    model: str = "openai/whisper-small",
    output_dir: Path | None = None,
) -> Path:
    log.info("download-model started for %r", model)
    repo_id = resolve_openvino_repo(model)
    short_name = repo_id.split("/")[-1].removesuffix("-ov").removesuffix("-fp16")
    if short_name.startswith("whisper-"):
        short_name = short_name.removeprefix("whisper-")
    target = (output_dir or default_model_dir(f"whisper-{short_name}")).expanduser()

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
    log.info("Connecting to Hugging Face Hub for %s ...", repo_id)
    log.info("This can take a few minutes on first download (~500 MB for whisper-small)")

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
