# local-transcription

Offline push-to-talk dictation with OpenVINO Whisper (Intel CPU/GPU/NPU). Inserts text into the focused application via clipboard paste (`Ctrl+V`, or `Ctrl+Shift+V` in terminals such as Alacritty) by default, with `wtype`, `dotool`, or `ydotool` as keystroke-injection fallbacks. Terminal detection uses Hyprland (`hyprctl activewindow`).

## Quick start

```bash
# One-time: download the OpenVINO Whisper turbo model (~1.6 GB)
uv run local-transcription download-model

# Or explicitly:
uv run local-transcription download-model --model openai/whisper-turbo

# Run the daemon (loads the model once)
uv sync --extra overlay   # once: recording indicator (PyGObject in uv venv)
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

While recording, a small floating indicator appears at the bottom center of the screen (Wayland layer-shell overlay): **Recording ●** (red, pulsing). When you stop, it switches to **Transcribing ●** (orange) while Whisper runs, then disappears.

When you stop, the full audio is transcribed once and the complete text is inserted in a single step. With the default `clipboard` backend, the daemon copies to the clipboard and sends a paste shortcut: `Ctrl+Shift+V` when the focused window class matches `LT_TERMINAL_CLASSES` (e.g. Alacritty), otherwise `Ctrl+V`.

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
| `LT_NUM_BEAMS` | `1` | Beam search width for transcription |
| `LT_STOPPING_WAIT` | `15` | Seconds to wait when toggling during an in-progress stop |
| `LT_APPEND_SPACE` | `1` | Leading space before next session after a successful dictation |
| `LT_TYPING_BACKEND` | `auto` | `clipboard` (preferred), `wtype`, `dotool`, or `ydotool`. `auto` tries `clipboard` first |
| `LT_PASTE_DELAY_MS` | `120` | Delay before sending the paste shortcut so focus settles (clipboard backend) |
| `LT_CLIPBOARD_RESTORE` | `1` | Restore previous clipboard content after pasting (`0` to disable) |
| `LT_TERMINAL_CLASSES` | `Alacritty,kitty,foot,…` | Comma-separated Hyprland window classes that get `Ctrl+Shift+V` paste. Empty string disables terminal detection (always `Ctrl+V`). Check class with `hyprctl activewindow -j` |
| `LT_OVERLAY` | `1` | Floating recording indicator (bottom center, gtk-layer-shell) |
| `LT_OVERLAY_MARGIN` | `32` | Distance from bottom screen edge in pixels |
| `LT_LOG_LEVEL` | `INFO` | `DEBUG` for verbose logs |
| `LT_NOTIFY` | `0` | Desktop notifications via `notify-send` (`1` to enable) |

## Troubleshooting

- **Old text appears before new dictation** — Expected when appending at the cursor: place the cursor where you want new text. Previous dictation stays to the left.
- **Wrong or garbled text** — Check cursor focus and window; try forcing `LT_LANGUAGE=de` or `en`.
- **Missing spaces / lost focus / text in the wrong place (browsers, Cursor, Electron apps)** — Caused by per-character key injection (`wtype`) being throttled by Chromium/Electron. The default `clipboard` backend (atomic Ctrl+V) fixes this. If it still misbehaves, increase `LT_PASTE_DELAY_MS` (e.g. `200`).
- **Nothing pastes in a terminal** — The `clipboard` backend sends `Ctrl+Shift+V` for classes in `LT_TERMINAL_CLASSES` (Alacritty is included by default). Confirm the window class with `hyprctl activewindow -j` and add it to the list if needed. Set `LT_LOG_LEVEL=DEBUG` to see which paste chord was used. If `hyprctl` is unavailable, paste falls back to `Ctrl+V`. Set `LT_TERMINAL_CLASSES=""` to disable terminal detection.
- **No recording indicator** — The uv venv is isolated from system Python; install overlay deps once:

  ```bash
  # Manjaro system libraries
  sudo pacman -S gtk3 gtk-layer-shell gobject-introspection libgirepository cairo pkgconf

  # PyGObject into the uv venv (matches Python 3.12)
  uv sync --extra overlay
  ```

  Set `LT_OVERLAY=0` to disable. Without PyGObject the daemon still runs, just without the dot.
- **Poor DE/EN quality** — Default is [Whisper turbo](https://github.com/openai/whisper) (`large-v3-turbo`, ~809M params). For even higher accuracy try `openai/whisper-medium` or force `LT_LANGUAGE=de` / `en`.
- **GPU crash / "Not Implemented"** — Intel Arc does not support `num_beams>1` on GPU. Default is `LT_NUM_BEAMS=1`. The transcriber retries automatically with beams=1 if needed.
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
