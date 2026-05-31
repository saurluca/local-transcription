from __future__ import annotations

import argparse
import sys
from pathlib import Path

from local_transcription.daemon import DictationDaemon, send_command
from local_transcription.log import get_logger, setup_logging
from local_transcription.models import default_model_dir, download_model

log = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-transcription",
        description="Offline push-to-talk dictation with OpenVINO (Intel Arc GPU).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging (same as LT_LOG_LEVEL=DEBUG).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    daemon = sub.add_parser("daemon", help="Run the dictation daemon (loads the model once on GPU).")
    daemon.add_argument(
        "--language",
        choices=["auto", "de", "en"],
        default=None,
        help="Whisper language (default: LT_LANGUAGE or auto).",
    )
    sub.add_parser("toggle", help="Start/stop recording and type into the active field.")
    sub.add_parser("start", help="Start recording.")
    sub.add_parser("stop", help="Stop recording and insert the final transcript.")
    sub.add_parser("status", help="Show daemon state.")
    sub.add_parser("shutdown", help="Stop the daemon.")

    download = sub.add_parser(
        "download-model",
        help="One-time download: fetch pre-converted OpenVINO Whisper model.",
    )
    download.add_argument(
        "--model",
        default="openai/whisper-small",
        help="Hugging Face model id (default: openai/whisper-small).",
    )
    download.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output directory (default: ~/.local/share/local-transcription/models/whisper-small).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.verbose:
        import os

        os.environ["LT_LOG_LEVEL"] = "DEBUG"
    setup_logging()

    log.info("Command: %s", args.command)

    if args.command == "daemon":
        if getattr(args, "language", None) is not None:
            import os

            os.environ["LT_LANGUAGE"] = args.language
        return DictationDaemon().run()

    if args.command == "download-model":
        try:
            output = args.output or default_model_dir(args.model.split("/")[-1])
            log.info("Output directory: %s", output)
            download_model(args.model, output)
        except RuntimeError as exc:
            log.error("%s", exc)
            return 1
        return 0

    try:
        log.debug("Sending command to daemon: %s", args.command)
        response = send_command(args.command)
        log.info("Daemon response: %s", response)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    print(response)
    if response.startswith("ERROR"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
