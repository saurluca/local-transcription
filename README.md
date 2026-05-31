# local-transcription

Offline push-to-talk dictation with OpenVINO Whisper (Intel CPU/GPU/NPU). Types into the focused application via `wtype`, `dotool`, or `ydotool`.

## Quick start

```bash
# One-time: download the OpenVINO Whisper model (~500 MB for whisper-small)
uv run local-transcription download-model --model openai/whisper-small

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
| `LT_MODEL_DIR` | `~/.local/share/.../whisper-small` | Path to converted OpenVINO model |
| `LT_STREAM_PARTIALS` | `1` | Live partial text while recording (`0` = final only) |
| `LT_PARTIAL_INTERVAL` | `0.45` | Seconds between partial transcriptions |
| `LT_MIN_PARTIAL_AUDIO` | `0.8` | Minimum recorded seconds before first partial |
| `LT_PARTIAL_JOIN_TIMEOUT` | `10` | Seconds to wait for partial thread on stop |
| `LT_APPEND_SPACE` | `1` | Leading space before next session after a successful dictation |
| `LT_TYPING_BACKEND` | `auto` | `wtype`, `dotool`, `ydotool`, or `clipboard` |
| `LT_LOG_LEVEL` | `INFO` | `DEBUG` for verbose logs |
| `LT_NOTIFY` | `1` | Desktop notifications via `notify-send` (`0` to disable) |

## Troubleshooting

- **Old text appears before new dictation** — Expected when appending at the cursor: place the cursor where you want new text. Previous dictation stays to the left.
- **Wrong or garbled text** — Check cursor focus and window; try `LT_STREAM_PARTIALS=0` for final-only typing.
- **Empty recording left partial text** — Partials are discarded automatically on stop with no speech.
- **Poor DE/EN quality** — Try `whisper-medium`, `LT_LANGUAGE=de` or `en` if you always use one language, or disable partials under GPU load.
- **Daemon already running** — `uv run local-transcription shutdown` or remove stale PID under `$XDG_RUNTIME_DIR/local-transcription.pid`.

## Commands

| Command | Description |
|---------|-------------|
| `daemon` | Start the dictation server |
| `toggle` / `start` / `stop` | Control recording |
| `status` | `IDLE` or `RECORDING` |
| `shutdown` | Stop the daemon |
| `download-model` | Fetch OpenVINO Whisper weights |
