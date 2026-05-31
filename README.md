# local-transcription

Offline push-to-talk dictation with OpenVINO Whisper (Intel CPU/GPU/NPU). Types into the focused application via `wtype`, `dotool`, or `ydotool`.

## Quick start

```bash
# One-time: download the OpenVINO Whisper turbo model (~1.6 GB)
uv run local-transcription download-model

# Or explicitly:
uv run local-transcription download-model --model openai/whisper-turbo

# Run the daemon (loads the model once)
uv run local-transcription daemon

# In another terminal or Hyprland keybind
uv run local-transcription toggle   # start/stop recording
```

Hyprland example:

```
bind = SUPER, V, exec, uv run local-transcription toggle
```

## How typing works

Each recording session types at the **focused cursor**. Text from earlier sessions is **not** removed; new dictation is inserted at the cursor. If you finished a previous dictation and start another with the cursor at the end of that text, a space is inserted before the new text when `LT_APPEND_SPACE=1` (default).

Partial updates during recording replace only what the **current** session typed, not older text in the field.

## Language (German / English)

Default is automatic detection per recording:

```bash
LT_LANGUAGE=auto uv run local-transcription daemon
# or
uv run local-transcription daemon --language auto
```

Force a language:

```bash
LT_LANGUAGE=de uv run local-transcription daemon
uv run local-transcription daemon --language en
```

`auto` picks German or English (and other Whisper languages) per utterance. Mixed DE+EN in a **single** sentence is a known Whisper limitation; switch languages between recordings instead.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LT_LANGUAGE` | `auto` | `auto`, `de`, or `en` |
| `LT_DEVICE` | `GPU` | OpenVINO device (`GPU`, `CPU`, `NPU`) |
| `LT_HF_MODEL` | `openai/whisper-turbo` | Model id for `download-model` (also `openai/whisper-small`, `openai/whisper-medium`, …) |
| `LT_MODEL_DIR` | `~/.local/share/.../whisper-turbo` | Path to converted OpenVINO model |
| `LT_STREAM_PARTIALS` | `1` | Live partial text while recording (`0` = final only) |
| `LT_PARTIAL_INTERVAL` | `0.45` | Seconds between partial transcriptions |
| `LT_PARTIAL_WINDOW` | `0` | `0` = transcribe full buffer per partial (coherent, recommended on GPU). Set to e.g. `4.0` for a sliding window only on slow CPU setups |
| `LT_MIN_PARTIAL_AUDIO` | `0.8` | Minimum recorded seconds before first partial |
| `LT_PARTIAL_JOIN_TIMEOUT` | `10` | Seconds to wait for partial thread on stop |
| `LT_SKIP_FINAL_IF_PARTIAL` | `1` | Skip final transcription when partial text is already up to date |
| `LT_NUM_BEAMS` | `1` | Beam search width for partial and final passes |
| `LT_FINAL_NUM_BEAMS` | same as `LT_NUM_BEAMS` | Beam width for final pass only (opt-in quality mode with `>1`) |
| `LT_FINAL_DEVICE` | _(unset)_ | Optional separate device for quality final pass (e.g. `CPU` with `LT_FINAL_NUM_BEAMS=5`) |
| `LT_STOPPING_WAIT` | `15` | Seconds to wait when toggling during an in-progress stop |
| `LT_APPEND_SPACE` | `1` | Leading space before next session after a successful dictation |
| `LT_TYPING_BACKEND` | `auto` | `wtype`, `dotool`, `ydotool`, or `clipboard` |
| `LT_LOG_LEVEL` | `INFO` | `DEBUG` for verbose logs |
| `LT_NOTIFY` | `1` | Desktop notifications via `notify-send` (`0` to disable) |

## Troubleshooting

- **Old text appears before new dictation** — Expected when appending at the cursor: place the cursor where you want new text. Previous dictation stays to the left.
- **Wrong or garbled text** — Check cursor focus and window; try `LT_STREAM_PARTIALS=0` for final-only typing.
- **Empty recording left partial text** — Partials are discarded automatically on stop with no speech.
- **Poor DE/EN quality** — Default is [Whisper turbo](https://github.com/openai/whisper) (`large-v3-turbo`, ~809M params). For even higher accuracy try `openai/whisper-medium` or force `LT_LANGUAGE=de` / `en`. For higher final quality at the cost of latency: `LT_FINAL_DEVICE=CPU LT_FINAL_NUM_BEAMS=5`.
- **GPU crash on stop / "Not Implemented"** — Intel Arc does not support `num_beams>1` on GPU. Default is `LT_NUM_BEAMS=1`. The transcriber retries automatically with beams=1 if needed.
- **Partials derail / repeat ("city of the city ...") or switch language** — Caused by transcribing short isolated windows. Keep `LT_PARTIAL_WINDOW=0` (default) so each partial uses the full buffer with one coherent language decision.
- **Daemon already running** — `uv run local-transcription shutdown` or remove stale PID under `$XDG_RUNTIME_DIR/local-transcription.pid`.

## Commands

| Command | Description |
|---------|-------------|
| `daemon` | Start the dictation server |
| `toggle` / `start` / `stop` | Control recording |
| `status` | `IDLE` or `RECORDING` |
| `shutdown` | Stop the daemon |
| `download-model` | Fetch OpenVINO Whisper weights (default: turbo) |

### Supported models

Pre-converted OpenVINO models from [OpenVINO on Hugging Face](https://huggingface.co/OpenVINO):

| CLI `--model` | OpenVINO repo | Notes |
|---------------|---------------|-------|
| `openai/whisper-turbo` (default) | `whisper-large-v3-turbo-fp16-ov` | Best speed/quality tradeoff (~8× faster than large) |
| `openai/whisper-small` | `whisper-small-fp16-ov` | Lighter, lower VRAM |
| `openai/whisper-medium` | `whisper-medium-fp16-ov` | Higher accuracy, slower |
| `openai/whisper-tiny` / `base` | … | Fastest, least accurate |
