#!/usr/bin/env python3
"""Native GTK race/shock timer using the validated Mario Kart Wii cues."""

from __future__ import annotations

import argparse
import array
import cairo
import ctypes
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
import wave
from collections import deque

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


APP_DIR = Path(__file__).resolve().parent
CUE_DIR = APP_DIR / "assets" / "cues"
CONFIG_PATH = Path.home() / ".config" / "shock-timer" / "gui.json"
RATE = 32000


class FastFFT:
    """Small ctypes binding to the system FFTW library; no Python package needed."""

    def __init__(self, size):
        self.size = size
        self.library = ctypes.CDLL("libfftw3.so.3")
        self.input = (ctypes.c_double * size)()
        self.output = (ctypes.c_double * (2 * (size // 2 + 1)))()
        self.library.fftw_plan_dft_r2c_1d.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.c_uint]
        self.library.fftw_plan_dft_r2c_1d.restype = ctypes.c_void_p
        self.library.fftw_execute.argtypes = [ctypes.c_void_p]
        self.library.fftw_destroy_plan.argtypes = [ctypes.c_void_p]
        self.plan = self.library.fftw_plan_dft_r2c_1d(size, self.input, self.output, 64)  # FFTW_ESTIMATE
        if not self.plan:
            raise RuntimeError("FFTW could not create an audio-analysis plan")

    def execute(self, samples, window):
        energy = 0.0
        for index in range(self.size):
            value = samples[index] * window[index]
            self.input[index] = value
            energy += value * value
        self.library.fftw_execute(self.plan)
        return energy / self.size

    def complex_at(self, index):
        return self.output[index * 2], self.output[index * 2 + 1]

    def __del__(self):
        if getattr(self, "plan", None):
            self.library.fftw_destroy_plan(self.plan)
            self.plan = None


class SpectralMatcher:
    FRAME = 1024
    HOP = 512
    BANDS = 96
    # Calibrated with the native FFT backend against audio track 4 of the
    # reference race recording. Each accepted cue has a clean margin over the
    # strongest non-cue window in that recording.
    THRESHOLDS = {"start": 0.525, "shock": 0.957, "end": 0.50}

    def __init__(self, callback=None):
        self.callback = callback or (lambda *_: None)
        self.lock = threading.RLock()
        self.fft = FastFFT(self.FRAME)
        self.window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (self.FRAME - 1)) for i in range(self.FRAME)]
        minimum, maximum = math.log(80), math.log(12000)
        spread = maximum - minimum
        self.bin_bands = []
        for k in range(self.FRAME // 2):
            hz = k * RATE / self.FRAME
            if hz < 80 or hz > 12000:
                self.bin_bands.append(-1)
            else:
                self.bin_bands.append(min(self.BANDS - 1, int((math.log(hz) - minimum) / spread * self.BANDS)))
        self.templates = {
            "start": self._load_template(CUE_DIR / "race-start.wav"),
            "shock": self._load_template(CUE_DIR / "shock-used.wav"),
            "end": self._load_template(CUE_DIR / "race-end.wav"),
        }
        self.reset()

    def reset(self):
        with self.lock:
            self.buffer = []
            self.frame_index = 0
            self.state = "idle"
            self.histories = {name: deque(maxlen=len(template)) for name, template in self.templates.items()}
            self.last_match_frame = {name: -100000 for name in self.templates}

    def set_state(self, state):
        with self.lock:
            self.state = state
            for history in self.histories.values():
                history.clear()

    def _load_template(self, path):
        samples = read_wav_mono(path)
        frames = [self.feature(samples[i:i + self.FRAME]) for i in range(0, len(samples) - self.FRAME + 1, self.HOP)]
        energies = [frame[1] for frame in frames]
        peak = max(energies, default=0.0001)
        active = [i for i, value in enumerate(energies) if value > peak * 0.12]
        if not active:
            return frames
        first, last = max(0, active[0] - 2), min(len(frames) - 1, active[-1] + 2)
        return frames[first:last + 1]

    def feature(self, samples):
        energy = self.fft.execute(samples, self.window)
        bands = [0.0] * self.BANDS
        for k in range(1, self.FRAME // 2):
            band = self.bin_bands[k]
            if band >= 0:
                real, imag = self.fft.complex_at(k)
                bands[band] += real * real + imag * imag
        bands = [math.log1p(value * 10000) for value in bands]
        mean = sum(bands) / len(bands)
        bands = [value - mean for value in bands]
        norm = math.sqrt(sum(value * value for value in bands)) or 1.0
        return ([value / norm for value in bands], energy)

    @staticmethod
    def similarity(live, reference):
        sum_a = sum_b = sum_aa = sum_bb = sum_ab = 0.0
        count = 0
        for live_frame, ref_frame in zip(live, reference):
            for a, b in zip(live_frame[0], ref_frame[0]):
                sum_a += a
                sum_b += b
                sum_aa += a * a
                sum_bb += b * b
                sum_ab += a * b
                count += 1
        covariance = sum_ab - sum_a * sum_b / count
        variance_a = sum_aa - sum_a * sum_a / count
        variance_b = sum_bb - sum_b * sum_b / count
        return covariance / (math.sqrt(max(0.0, variance_a * variance_b)) + 1e-9)

    def consume(self, samples):
        with self.lock:
            self.buffer.extend(samples)
            while len(self.buffer) >= self.FRAME:
                feature = self.feature(self.buffer[:self.FRAME])
                del self.buffer[:self.HOP]
                self._consume_feature(feature)
                self.frame_index += 1

    def _consume_feature(self, feature):
        active = ("start",) if self.state == "idle" else ("shock", "end")
        for name in active:
            template = self.templates[name]
            history = self.histories[name]
            history.append(feature)
            if len(history) < len(template):
                continue
            score = self.similarity(history, template)
            refractory = int(2.5 * RATE / self.HOP)
            if score < self.THRESHOLDS[name] or self.frame_index - self.last_match_frame[name] < refractory:
                continue
            self.last_match_frame[name] = self.frame_index
            onset_frame = self.frame_index - len(template) + 1
            onset_seconds = onset_frame * self.HOP / RATE
            latency_ms = len(template) * self.HOP / RATE * 1000
            self.state = "idle" if name == "end" else "race"
            self.callback(name, score, onset_seconds, latency_ms)
            for item in self.histories.values():
                item.clear()
            break


def fft(real, imag):
    n = len(real)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            real[i], real[j] = real[j], real[i]
            imag[i], imag[j] = imag[j], imag[i]
    length = 2
    while length <= n:
        angle = -2 * math.pi / length
        base_r, base_i = math.cos(angle), math.sin(angle)
        for start in range(0, n, length):
            wr, wi = 1.0, 0.0
            for offset in range(length // 2):
                left, right = start + offset, start + offset + length // 2
                vr = real[right] * wr - imag[right] * wi
                vi = real[right] * wi + imag[right] * wr
                ur, ui = real[left], imag[left]
                real[left], imag[left] = ur + vr, ui + vi
                real[right], imag[right] = ur - vr, ui - vi
                wr, wi = wr * base_r - wi * base_i, wr * base_i + wi * base_r
        length <<= 1


def read_wav_mono(path):
    with wave.open(str(path), "rb") as source:
        channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
        if width != 2 or rate != RATE:
            raise ValueError(f"Cue must be PCM16 at {RATE} Hz: {path}")
        values = array.array("h")
        values.frombytes(source.readframes(source.getnframes()))
        if sys.byteorder != "little":
            values.byteswap()
        if channels == 1:
            return [value / 32768.0 for value in values]
        return [sum(values[i:i + channels]) / (channels * 32768.0) for i in range(0, len(values), channels)]


class RaceSession:
    COOLDOWN_SECONDS = 30.0

    def __init__(self):
        self.cooldown = self.COOLDOWN_SECONDS
        self.state = "idle"
        self.deadline = None

    def start(self, occurred_at=None):
        if self.state != "idle":
            return False
        self.state = "cooldown"
        self.deadline = (occurred_at or time.monotonic()) + self.cooldown
        return True

    def shock(self, occurred_at=None):
        if self.state == "idle":
            return False
        self.state = "cooldown"
        self.deadline = (occurred_at or time.monotonic()) + self.cooldown
        return True

    def end(self):
        if self.state == "idle":
            return False
        self.state, self.deadline = "idle", None
        return True

    def tick(self):
        if self.state == "cooldown" and time.monotonic() >= self.deadline:
            self.state, self.deadline = "available", None
            return True
        return False

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic()) if self.state == "cooldown" else 0.0


class CaptureWorker:
    def __init__(self, source, matcher, error_callback):
        self.source, self.matcher, self.error_callback = source, matcher, error_callback
        self.process = None
        self.stopping = threading.Event()
        self.thread = None

    def start(self):
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "pulse", "-i",
                   self.source or "default", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "pipe:1"]
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.thread = threading.Thread(target=self._run, name="capture-audio", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while not self.stopping.is_set():
                data = self.process.stdout.read(16384)
                if not data:
                    error = self.process.stderr.read().decode("utf-8", "replace").strip()
                    if not self.stopping.is_set():
                        GLib.idle_add(self.error_callback, error or "The audio input stopped.")
                    break
                values = array.array("f")
                values.frombytes(data)
                if sys.byteorder != "little":
                    values.byteswap()
                self.matcher.consume(values)
        except Exception as error:
            if not self.stopping.is_set():
                GLib.idle_add(self.error_callback, str(error))

    def stop(self):
        self.stopping.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()


class TimerDial(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_size_request(320, 320)
        self.state, self.remaining, self.cooldown = "idle", 0.0, 30.0
        self.icon = cairo.ImageSurface.create_from_png(str(APP_DIR / "assets" / "shock.png"))
        self.connect("draw", self._draw)

    def update(self, state, remaining, cooldown):
        self.state, self.remaining, self.cooldown = state, remaining, cooldown
        self.queue_draw()

    def _draw(self, _, context):
        allocation = self.get_allocation()
        cx, cy = allocation.width / 2, allocation.height / 2
        radius = min(allocation.width, allocation.height) / 2 - 28
        context.set_line_width(16)
        context.set_source_rgba(0.17, 0.20, 0.27, 1)
        context.arc(cx, cy, radius, 0, 2 * math.pi)
        context.stroke()
        colors = {"idle": (0.35, 0.39, 0.48), "cooldown": (0.42, 0.56, 1.0), "available": (0.25, 0.92, 0.59)}
        context.set_source_rgb(*colors[self.state])
        progress = 1.0 if self.state == "available" else (1 - self.remaining / self.cooldown if self.state == "cooldown" else 0)
        context.arc(cx, cy, radius, -math.pi / 2, -math.pi / 2 + progress * 2 * math.pi)
        context.stroke()
        icon_size = radius * 1.08
        icon_scale = icon_size / max(self.icon.get_width(), self.icon.get_height())
        context.save()
        context.translate(cx - self.icon.get_width() * icon_scale / 2, cy - self.icon.get_height() * icon_scale / 2 - 10)
        context.scale(icon_scale, icon_scale)
        context.set_source_surface(self.icon, 0, 0)
        context.paint_with_alpha(0.55 if self.state == "idle" else 1.0)
        context.restore()
        context.select_font_face("Sans", 0, 1)
        context.set_font_size(13)
        label = "SHOCK AVAILABLE" if self.state == "available" else ("SHOCK RECHARGING" if self.state == "cooldown" else "WAITING FOR RACE")
        extents = context.text_extents(label)
        context.move_to(cx - extents.width / 2, cy + radius * 0.68)
        context.set_source_rgb(0.55, 0.61, 0.73)
        context.show_text(label)


class ShockTimerWindow(Gtk.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="Shock Timer")
        self.set_default_size(1040, 720)
        self.set_size_request(860, 620)
        self.set_icon_from_file(str(APP_DIR / "assets" / "shock.png"))
        self.config = load_config()
        self.session = RaceSession()
        self.matcher = SpectralMatcher(lambda *event: GLib.idle_add(self.on_match, *event))
        self.capture = None
        self.alert_process = None
        self._build()
        self.refresh_sources()
        GLib.timeout_add(100, self.on_tick)
        self.connect("delete-event", self.on_close)

    def _build(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        root.set_border_width(24)
        self.add(root)

        header = Gtk.Box(spacing=14)
        title = Gtk.Label()
        title.set_markup("<span size='22000' weight='bold'>⚡ Shock Timer</span>\n<span foreground='#8f9bb3'>Mario Kart Wii audio assistant</span>")
        title.set_xalign(0)
        header.pack_start(title, True, True, 0)
        self.status_label = Gtk.Label(label="OFFLINE")
        self.status_label.get_style_context().add_class("pill")
        header.pack_end(self.status_label, False, False, 0)
        self.enable_switch = Gtk.Switch()
        self.enable_switch.set_valign(Gtk.Align.CENTER)
        self.enable_switch.connect("notify::active", self.toggle_monitoring)
        header.pack_end(self.enable_switch, False, False, 0)
        root.pack_start(header, False, False, 0)

        body = Gtk.Box(spacing=18)
        root.pack_start(body, True, True, 0)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left.get_style_context().add_class("card")
        left.set_border_width(18)
        self.dial = TimerDial()
        left.pack_start(self.dial, True, True, 0)
        self.state_title = Gtk.Label(label="Ready when you are")
        self.state_title.get_style_context().add_class("state-title")
        left.pack_start(self.state_title, False, False, 0)
        self.state_detail = Gtk.Label(label="Enable listening to detect the next race.")
        self.state_detail.set_line_wrap(True)
        self.state_detail.set_justify(Gtk.Justification.CENTER)
        self.state_detail.get_style_context().add_class("muted")
        left.pack_start(self.state_detail, False, False, 0)
        body.pack_start(left, True, True, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        right.set_size_request(430, -1)
        body.pack_start(right, False, True, 0)
        right.pack_start(self.section_label("AUDIO INPUT"), False, False, 0)
        input_row = Gtk.Box(spacing=8)
        self.source_combo = Gtk.ComboBoxText()
        self.source_combo.set_hexpand(True)
        input_row.pack_start(self.source_combo, True, True, 0)
        refresh = Gtk.Button(label="↻")
        refresh.connect("clicked", lambda *_: self.refresh_sources())
        input_row.pack_end(refresh, False, False, 0)
        right.pack_start(input_row, False, False, 0)

        right.pack_start(self.section_label("AVAILABLE ALERT"), False, False, 0)
        alert_row = Gtk.Box(spacing=8)
        self.sound_label = Gtk.Label(label=self.sound_name(), xalign=0)
        self.sound_label.set_ellipsize(3)
        self.sound_label.get_style_context().add_class("file-chip")
        alert_row.pack_start(self.sound_label, True, True, 0)
        choose = Gtk.Button(label="Choose…")
        choose.connect("clicked", self.choose_sound)
        alert_row.pack_end(choose, False, False, 0)
        right.pack_start(alert_row, False, False, 0)
        test = Gtk.Button(label="▶  Test alert sound")
        test.connect("clicked", lambda *_: self.play_alert())
        right.pack_start(test, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.log_box = Gtk.ListBox()
        self.log_box.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.add(self.log_box)
        right.pack_start(scroll, True, True, 0)

    @staticmethod
    def section_label(text):
        label = Gtk.Label(label=text, xalign=0)
        label.get_style_context().add_class("section")
        return label

    def sound_name(self):
        path = self.config.get("alert_sound")
        return Path(path).name if path else "Default two-tone alert"

    def refresh_sources(self):
        previous = self.config.get("source", "default")
        self.source_combo.remove_all()
        self.source_combo.append("default", "Default system input")
        try:
            result = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=3)
            for line in result.stdout.splitlines():
                fields = line.split("\t")
                if len(fields) >= 2:
                    name = fields[1]
                    self.source_combo.append(name, name)
        except (OSError, subprocess.SubprocessError):
            pass
        self.source_combo.set_active_id(previous)
        if self.source_combo.get_active() < 0:
            self.source_combo.set_active_id("default")

    def toggle_monitoring(self, switch, _):
        if switch.get_active():
            self.start_monitoring()
        else:
            self.stop_monitoring()

    def start_monitoring(self):
        source = self.source_combo.get_active_id() or "default"
        self.config["source"] = source
        save_config(self.config)
        self.matcher.reset()
        self.matcher.set_state(self.session.state)
        try:
            self.capture = CaptureWorker(source, self.matcher, self.capture_error)
            self.capture.start()
        except Exception as error:
            self.capture_error(str(error))
            return
        self.source_combo.set_sensitive(False)
        self.status_label.set_text("LISTENING")
        self.status_label.get_style_context().add_class("live")
        self.add_log("Monitoring enabled", source)
        self.render_state()

    def stop_monitoring(self):
        if self.capture:
            self.capture.stop()
            self.capture = None
        self.source_combo.set_sensitive(True)
        self.status_label.set_text("OFFLINE")
        self.status_label.get_style_context().remove_class("live")
        self.render_state()

    def capture_error(self, message):
        self.stop_monitoring()
        if self.enable_switch.get_active():
            self.enable_switch.set_active(False)
        dialog = Gtk.MessageDialog(self, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE, "Could not open audio input")
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
        return False

    def on_match(self, cue, score, _onset_seconds, latency_ms):
        self.handle_cue(cue, score, latency_ms)
        return False

    def handle_cue(self, cue, score, latency_ms=0):
        occurred = time.monotonic() - latency_ms / 1000
        changed = False
        if cue == "start":
            changed = self.session.start(occurred)
            message = "Race started"
        elif cue == "shock":
            changed = self.session.shock(occurred)
            message = "Shock used — cooldown restarted"
        else:
            changed = self.session.end()
            message = "FINITO — timer cancelled"
        if changed:
            self.matcher.set_state(self.session.state)
            source = "manual" if score >= 0.999 else f"audio · {score * 100:.1f}%"
            self.add_log(message, source)
            self.render_state()

    def on_tick(self):
        if self.session.tick():
            self.matcher.set_state(self.session.state)
            self.add_log("Shock available", "30-second timer")
            self.play_alert()
        self.render_state()
        return True

    def render_state(self):
        self.dial.update(self.session.state, self.session.remaining(), self.session.cooldown)
        if self.session.state == "idle":
            self.state_title.set_text("Listening for race start" if self.enable_switch.get_active() else "Ready when you are")
            self.state_detail.set_text("The timer begins on the GO cue." if self.enable_switch.get_active() else "Enable listening to detect the next race.")
        elif self.session.state == "cooldown":
            self.state_title.set_text("Shock recharging")
            self.state_detail.set_text("A shock restarts the timer. FINITO cancels it.")
        else:
            self.state_title.set_text("Shock is available")
            self.state_detail.set_text("Listening for the next shock or race finish.")

    def choose_sound(self, _):
        dialog = Gtk.FileChooserDialog("Choose the available-alert sound", self, Gtk.FileChooserAction.OPEN,
                                       ("Cancel", Gtk.ResponseType.CANCEL, "Choose", Gtk.ResponseType.OK))
        audio_filter = Gtk.FileFilter()
        audio_filter.set_name("Audio files")
        for pattern in ("*.mp3", "*.wav", "*.ogg", "*.flac", "*.m4a", "*.aac", "*.opus"):
            audio_filter.add_pattern(pattern)
            audio_filter.add_pattern(pattern.upper())
        dialog.add_filter(audio_filter)
        if dialog.run() == Gtk.ResponseType.OK:
            self.config["alert_sound"] = dialog.get_filename()
            save_config(self.config)
            self.sound_label.set_text(self.sound_name())
        dialog.destroy()

    def play_alert(self):
        if self.alert_process and self.alert_process.poll() is None:
            self.alert_process.terminate()
        custom = self.config.get("alert_sound")
        if custom and Path(custom).is_file():
            command = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", custom]
        else:
            source = "sine=frequency=1046:duration=.30"
            command = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-f", "lavfi", "-i", source]
        try:
            self.alert_process = subprocess.Popen(command)
        except OSError as error:
            self.add_log("Could not play alert", str(error))

    def add_log(self, message, detail):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(9)
        title = Gtk.Label(label=message, xalign=0)
        title.get_style_context().add_class("log-title")
        subtitle = Gtk.Label(label=f"{time.strftime('%H:%M:%S')}  ·  {detail}", xalign=0)
        subtitle.get_style_context().add_class("muted")
        box.pack_start(title, False, False, 0)
        box.pack_start(subtitle, False, False, 0)
        row.add(box)
        self.log_box.insert(row, 0)
        row.show_all()
        while len(self.log_box.get_children()) > 20:
            self.log_box.remove(self.log_box.get_children()[-1])

    def on_close(self, *_):
        self.stop_monitoring()
        if self.alert_process and self.alert_process.poll() is None:
            self.alert_process.terminate()
        return False


class ShockTimerApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="local.HumbleKingIsBlack.ShockTimer")

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        window = self.props.active_window or ShockTimerWindow(self)
        window.show_all()
        window.present()


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_config(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def analyze_video(video, stream):
    events = []
    matcher = SpectralMatcher(lambda *event: events.append(event))
    process = subprocess.Popen(["ffmpeg", "-v", "error", "-i", video, "-map", f"0:{stream}", "-ac", "1",
                                "-ar", str(RATE), "-f", "f32le", "pipe:1"], stdout=subprocess.PIPE)
    while True:
        data = process.stdout.read(65536)
        if not data:
            break
        samples = array.array("f")
        samples.frombytes(data)
        if sys.byteorder != "little":
            samples.byteswap()
        matcher.consume(samples)
        while len(events):
            cue, score, onset, latency = events.pop(0)
            print(f"{cue:5}  {onset:8.3f}s  score={score:.4f}  latency={latency / 1000:.3f}s")
            matcher.set_state("race" if cue in ("start", "shock") else "idle")
    return process.wait()


CSS = """
window { background: #10131a; color: #eef2ff; }
label { color: #eef2ff; }
.card { background: #171c26; border: 1px solid #283043; border-radius: 18px; }
.state-title { font-size: 22px; font-weight: 700; }
.muted { color: #8f9bb3; }
.section { color: #75829d; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; margin-top: 7px; }
.pill { background: #282e3d; color: #a9b4c9; border-radius: 999px; padding: 7px 12px; font-size: 11px; font-weight: 700; }
.pill.live { background: #153d31; color: #62e6a9; }
.file-chip { background: #171c26; border: 1px solid #30394d; border-radius: 8px; padding: 9px 11px; color: #cfd7e8; }
.log-title { font-weight: 700; }
button { background: #242b3a; color: #eef2ff; border: 1px solid #343e53; border-radius: 8px; padding: 9px 13px; }
button:hover { background: #30394c; }
combobox button, spinbutton { background: #171c26; color: #eef2ff; border: 1px solid #30394d; border-radius: 8px; padding: 7px; }
switch slider { background: #d8deeb; }
switch:checked { background: #42df91; }
list, list row { background: #151923; color: #eef2ff; }
list row { border-bottom: 1px solid #252c3a; }
scrollbar slider { background: #3b455b; border-radius: 8px; }
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", metavar="VIDEO", help="offline-test a recording instead of opening the GUI")
    parser.add_argument("--stream", type=int, default=4, help="global FFmpeg audio stream index for --analyze")
    arguments = parser.parse_args()
    if arguments.analyze:
        raise SystemExit(analyze_video(arguments.analyze, arguments.stream))
    raise SystemExit(ShockTimerApplication().run(sys.argv[:1]))
