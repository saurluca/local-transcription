from __future__ import annotations

import threading
from typing import Literal, Protocol

from local_transcription.log import get_logger

log = get_logger("overlay")

OverlayState = Literal["hidden", "recording", "stopping"]


class OverlayBackend(Protocol):
    def start(self) -> bool: ...

    def stop(self) -> None: ...

    def set_state(self, state: OverlayState) -> None: ...


class NullOverlay:
    def start(self) -> bool:
        return True

    def stop(self) -> None:
        return

    def set_state(self, state: OverlayState) -> None:
        return


def _layer_shell_available() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("GtkLayerShell", "0.1")
    except (ImportError, ValueError):
        return False
    return True


class DictationOverlay:
    """Floating Wayland indicator anchored bottom-center via gtk-layer-shell."""

    def __init__(self, *, margin_bottom: int = 32) -> None:
        self._margin_bottom = margin_bottom
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._shutdown = threading.Event()
        self._state: OverlayState = "hidden"
        self._state_lock = threading.Lock()

    def start(self) -> bool:
        if not _layer_shell_available():
            log.warning(
                "Recording overlay unavailable "
                "(system: gtk3 gtk-layer-shell; venv: uv sync --extra overlay)"
            )
            return False

        self._thread = threading.Thread(
            target=self._run_gtk,
            name="dictation-overlay",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            log.warning("Recording overlay did not start in time")
            return False
        log.info("Recording overlay ready (bottom center)")
        return True

    def stop(self) -> None:
        self._shutdown.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def set_state(self, state: OverlayState) -> None:
        with self._state_lock:
            self._state = state
        if self._ready.is_set():
            self._dispatch(self._apply_state)

    def _dispatch(self, callback) -> None:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib

        GLib.idle_add(callback)

    def _current_state(self) -> OverlayState:
        with self._state_lock:
            return self._state

    def _run_gtk(self) -> None:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("GtkLayerShell", "0.1")
        from gi.repository import GLib, Gtk, GtkLayerShell

        css = b"""
        window {
            background-color: transparent;
        }
        #dictation-indicator-box {
            background-color: alpha(#1a1a1a, 0.72);
            border-radius: 16px;
            padding: 8px 14px;
        }
        #dictation-indicator-box.recording #dictation-indicator-dot {
            color: #ff4444;
        }
        #dictation-indicator-box.stopping #dictation-indicator-dot {
            color: #ffaa44;
        }
        #dictation-indicator-dot {
            font-size: 20px;
            font-weight: bold;
        }
        """

        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gtk.Window().get_screen(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        window.set_decorated(False)
        window.set_resizable(False)
        window.set_app_paintable(True)
        screen = window.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            window.set_visual(visual)

        GtkLayerShell.init_for_window(window)
        GtkLayerShell.set_layer(window, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(window, GtkLayerShell.Edge.BOTTOM, True)
        GtkLayerShell.set_margin(window, GtkLayerShell.Edge.BOTTOM, self._margin_bottom)
        GtkLayerShell.set_keyboard_mode(window, GtkLayerShell.KeyboardMode.NONE)
        GtkLayerShell.set_exclusive_zone(window, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_name("dictation-indicator-box")
        dot = Gtk.Label(label="●")
        dot.set_name("dictation-indicator-dot")
        box.pack_start(dot, False, False, 0)
        window.add(box)

        pulse_source_id: int | None = None
        pulse_phase = 0

        def stop_pulse() -> None:
            nonlocal pulse_source_id
            if pulse_source_id is not None:
                GLib.source_remove(pulse_source_id)
                pulse_source_id = None
            dot.set_opacity(1.0)

        def on_pulse() -> bool:
            nonlocal pulse_phase
            pulse_phase = 1 - pulse_phase
            dot.set_opacity(0.45 if pulse_phase else 1.0)
            return True

        def start_pulse() -> None:
            nonlocal pulse_source_id
            stop_pulse()
            pulse_source_id = GLib.timeout_add(450, on_pulse)

        def apply_state() -> bool:
            state = self._current_state()
            style = box.get_style_context()
            for css_class in ("recording", "stopping"):
                style.remove_class(css_class)

            if state == "hidden":
                stop_pulse()
                window.hide()
                return False

            window.show_all()
            if state == "recording":
                style.add_class("recording")
                start_pulse()
            else:
                style.add_class("stopping")
                stop_pulse()
                dot.set_opacity(0.85)
            return False

        def on_shutdown_check() -> bool:
            if self._shutdown.is_set():
                stop_pulse()
                Gtk.main_quit()
                return False
            return True

        self._apply_state = apply_state
        self._dispatch = lambda cb: GLib.idle_add(cb)

        window.connect("destroy", Gtk.main_quit)
        GLib.timeout_add(200, on_shutdown_check)
        self._ready.set()
        apply_state()
        Gtk.main()


def create_overlay(*, enabled: bool = True, margin_bottom: int = 32) -> OverlayBackend:
    if not enabled:
        return NullOverlay()
    overlay = DictationOverlay(margin_bottom=margin_bottom)
    if overlay.start():
        return overlay
    return NullOverlay()
