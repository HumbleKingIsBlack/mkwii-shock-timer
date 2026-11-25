#!/usr/bin/env python3
"""Native Windows frontend for the Mario Kart Wii Shock Timer."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import wave
from collections import deque

try:
    import numpy as np
except ImportError as error:
    raise SystemExit("NumPy is required. Run setup-windows.bat first.") from error


APP_DIR = Path(__file__).resolve().parent
CUE_DIR = APP_DIR / "assets" / "cues"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ShockTimer"
CONFIG_PATH = CONFIG_DIR / "config.json"
RATE = 32000
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class SpectralMatcher:
    FRAME = 1024
    HOP = 512
    BANDS = 96
    THRESHOLDS = {"start": 0.525, "shock": 0.957, "end": 0.50}

    def __init__(self, callback=None):
        self.callback = callback or (lambda *_: None)
        self.lock = threading.RLock()
        self.window = np.hanning(self.FRAME).astype(np.float64)
        minimum, maximum = math.log(80), math.log(12000)
        spread = maximum - minimum
        bins = []
        for index in range(self.FRAME // 2 + 1):
            hz = index * RATE / self.FRAME
            bins.append(-1 if hz < 80 or hz > 12000 else min(self.BANDS - 1, int((math.log(hz) - minimum) / spread * self.BANDS)))
        self.bin_bands = np.asarray(bins, dtype=np.int16)
        self.valid_bins = self.bin_bands >= 0
        self.templates = {
            "start": self._load_template(CUE_DIR / "race-start.wav"),
            "shock": self._load_template(CUE_DIR / "shock-used.wav"),
            "end": self._load_template(CUE_DIR / "race-end.wav"),
        }
        self.reset()

    def reset(self):
        with self.lock:
            self.buffer = np.empty(0, dtype=np.float32)
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
        frames = [self.feature(samples[index:index + self.FRAME]) for index in range(0, len(samples) - self.FRAME + 1, self.HOP)]
        peak = max((energy for _, energy in frames), default=0.0001)
        active = [index for index, (_, energy) in enumerate(frames) if energy > peak * 0.12]
        if active:
            frames = frames[max(0, active[0] - 2):min(len(frames), active[-1] + 3)]
        return np.stack([bands for bands, _ in frames])

    def feature(self, samples):
        values = np.asarray(samples, dtype=np.float64) * self.window
        energy = float(np.mean(values * values))
        spectrum = np.fft.rfft(values)
        power = spectrum.real * spectrum.real + spectrum.imag * spectrum.imag
        bands = np.bincount(
            self.bin_bands[self.valid_bins],
            weights=power[self.valid_bins],
            minlength=self.BANDS,
        ).astype(np.float64)
        bands = np.log1p(bands * 10000)
        bands -= bands.mean()
        norm = np.linalg.norm(bands)
        if norm:
            bands /= norm
        return bands, energy

    @staticmethod
    def similarity(live, reference):
        a = np.stack(live).reshape(-1)
        b = reference.reshape(-1)
        count = a.size
        covariance = float(np.dot(a, b) - a.sum() * b.sum() / count)
        variance_a = float(np.dot(a, a) - a.sum() ** 2 / count)
        variance_b = float(np.dot(b, b) - b.sum() ** 2 / count)
        return covariance / (math.sqrt(max(0.0, variance_a * variance_b)) + 1e-9)

    def consume(self, samples):
        with self.lock:
            self.buffer = np.concatenate((self.buffer, np.asarray(samples, dtype=np.float32)))
            offset = 0
            while self.buffer.size - offset >= self.FRAME:
                feature = self.feature(self.buffer[offset:offset + self.FRAME])
                self._consume_feature(feature[0])
                self.frame_index += 1
                offset += self.HOP
            if offset:
                self.buffer = self.buffer[offset:].copy()

    def _consume_feature(self, bands):
        active = ("start",) if self.state == "idle" else ("shock", "end")
        for name in active:
            template = self.templates[name]
            history = self.histories[name]
            history.append(bands)
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


def read_wav_mono(path):
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        if source.getsampwidth() != 2 or source.getframerate() != RATE:
            raise ValueError(f"Cue must be PCM16 at {RATE} Hz: {path}")
        values = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32)
        if channels > 1:
            values = values.reshape(-1, channels).mean(axis=1)
        return values / 32768.0


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


def list_dshow_audio_devices():
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    devices = []
    for line in result.stderr.splitlines():
        match = re.search(r'"([^"]+)"\s*\(audio\)', line)
        if match and match.group(1) not in devices:
            devices.append(match.group(1))
    return devices


def list_windows_output_devices():
    import sounddevice as sd

    devices = []
    host_apis = sd.query_hostapis()
    default_output = sd.default.device[1]
    for index, device in enumerate(sd.query_devices()):
        if device["max_output_channels"] <= 0:
            continue
        host_name = host_apis[device["hostapi"]]["name"]
        prefix = "Default — " if index == default_output else ""
        display = f"{prefix}{device['name']} — {host_name} [{index}]"
        devices.append((display, index))
    devices.sort(key=lambda item: item[1] != default_output)
    return devices


class CaptureWorker:
    def __init__(self, device, matcher, error_callback):
        self.device, self.matcher, self.error_callback = device, matcher, error_callback
        self.process = None
        self.stopping = threading.Event()

    def start(self):
        if not self.device:
            raise RuntimeError("Select a capture-card audio device first.")
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "dshow", "-i", f"audio={self.device}",
            "-ac", "1", "-ar", str(RATE), "-f", "f32le", "pipe:1",
        ]
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._run, name="capture-audio", daemon=True).start()

    def _run(self):
        try:
            while not self.stopping.is_set():
                data = self.process.stdout.read(16384)
                if not data:
                    error = self.process.stderr.read().decode("utf-8", "replace").strip()
                    if not self.stopping.is_set():
                        self.error_callback(error or "The audio input stopped.")
                    break
                self.matcher.consume(np.frombuffer(data, dtype="<f4"))
        except Exception as error:
            if not self.stopping.is_set():
                self.error_callback(str(error))

    def stop(self):
        self.stopping.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()


class ShockTimerWindows:
    BG = "#10131a"
    CARD = "#171c26"
    BORDER = "#30394d"
    TEXT = "#eef2ff"
    MUTED = "#8f9bb3"
    BLUE = "#6b8fff"
    GREEN = "#42df91"

    def __init__(self):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk, self.filedialog, self.messagebox, self.ttk = tk, filedialog, messagebox, ttk
        self.root = tk.Tk()
        self.root.title("Shock Timer")
        self.root.geometry("1040x720")
        self.root.minsize(860, 620)
        self.root.configure(bg=self.BG)
        self.config = load_config()
        self.session = RaceSession()
        self.matcher = SpectralMatcher(lambda *event: self.root.after(0, self.on_match, *event))
        self.capture = None
        self.enabled = False
        self.devices = []
        self.output_devices = {}
        self.icon = tk.PhotoImage(file=str(APP_DIR / "assets" / "shock.png"))
        self.small_icon = self.icon.subsample(3, 3)
        self.root.iconphoto(True, self.icon)
        self._build()
        self.refresh_devices()
        self.refresh_outputs()
        self.tick()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build(self):
        tk, ttk = self.tk, self.ttk
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Dark.TCombobox", fieldbackground=self.CARD, background=self.CARD, foreground=self.TEXT, arrowcolor=self.TEXT)

        root = tk.Frame(self.root, bg=self.BG, padx=24, pady=22)
        root.pack(fill="both", expand=True)
        header = tk.Frame(root, bg=self.BG)
        header.pack(fill="x", pady=(0, 18))
        tk.Label(header, text="⚡ Shock Timer", bg=self.BG, fg=self.TEXT, font=("Segoe UI", 22, "bold")).pack(side="left")
        tk.Label(header, text="Mario Kart Wii audio assistant", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 10)).pack(side="left", padx=16, pady=(10, 0))
        self.toggle = tk.Button(header, text="●  OFFLINE", command=self.toggle_monitoring, bg="#252b39", fg="#a9b4c9",
                                activebackground="#2d3546", activeforeground=self.TEXT, bd=0, width=15, padx=16, pady=9, cursor="hand2")
        self.toggle.pack(side="right")

        body = tk.Frame(root, bg=self.BG)
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=self.CARD, highlightbackground="#283043", highlightthickness=1)
        left.pack(side="left", fill="both", expand=True, padx=(0, 18))
        self.canvas = tk.Canvas(left, width=360, height=390, bg=self.CARD, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=20, pady=(18, 0))
        self.state_title = tk.Label(left, text="Ready when you are", bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 18, "bold"))
        self.state_title.pack(pady=(0, 5))
        self.state_detail = tk.Label(left, text="Enable listening to detect the next race.", bg=self.CARD, fg=self.MUTED,
                                     font=("Segoe UI", 10), wraplength=420)
        self.state_detail.pack(pady=(0, 22))

        right = tk.Frame(body, bg=self.BG, width=420)
        right.pack(side="right", fill="both")
        right.pack_propagate(False)
        self.section(right, "AUDIO INPUT")
        device_row = tk.Frame(right, bg=self.BG)
        device_row.pack(fill="x", pady=(6, 16))
        self.device_var = tk.StringVar(value=self.config.get("audio_device", ""))
        self.device_combo = ttk.Combobox(device_row, textvariable=self.device_var, state="readonly", style="Dark.TCombobox")
        self.device_combo.pack(side="left", fill="x", expand=True, ipady=6)
        tk.Button(device_row, text="↻", command=self.refresh_devices, bg="#242b3a", fg=self.TEXT, bd=0, padx=13, pady=9).pack(side="right", padx=(8, 0))

        self.section(right, "ALERT OUTPUT")
        output_row = tk.Frame(right, bg=self.BG)
        output_row.pack(fill="x", pady=(6, 8))
        self.output_var = tk.StringVar(value=self.config.get("output_device", ""))
        self.output_combo = ttk.Combobox(output_row, textvariable=self.output_var, state="readonly", style="Dark.TCombobox")
        self.output_combo.pack(side="left", fill="x", expand=True, ipady=6)
        self.output_combo.bind("<<ComboboxSelected>>", self.output_changed)
        tk.Button(output_row, text="↻", command=self.refresh_outputs, bg="#242b3a", fg=self.TEXT, bd=0, padx=13, pady=9).pack(side="right", padx=(8, 0))
        self.section(right, "ALERT SOUND")
        alert_row = tk.Frame(right, bg=self.BG)
        alert_row.pack(fill="x", pady=(0, 8))
        self.sound_label = tk.Label(alert_row, text=self.sound_name(), anchor="w", bg=self.CARD, fg="#cfd7e8", padx=11, pady=10)
        self.sound_label.pack(side="left", fill="x", expand=True)
        tk.Button(alert_row, text="Choose…", command=self.choose_sound, bg="#242b3a", fg=self.TEXT, bd=0, padx=13, pady=10).pack(side="right", padx=(8, 0))
        tk.Button(right, text="▶  Test alert sound", command=self.play_alert, bg="#242b3a", fg=self.TEXT, bd=0, pady=10).pack(fill="x", pady=(0, 16))

        self.log = tk.Text(right, bg="#151923", fg=self.TEXT, insertbackground=self.TEXT, relief="flat", state="disabled",
                           font=("Segoe UI", 10), padx=10, pady=8, wrap="word")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("title", font=("Segoe UI", 10, "bold"), foreground=self.TEXT)
        self.log.tag_configure("detail", foreground=self.MUTED, spacing3=10)

    def section(self, parent, text):
        self.tk.Label(parent, text=text, anchor="w", bg=self.BG, fg="#75829d", font=("Segoe UI", 9, "bold")).pack(fill="x", pady=(4, 0))

    def refresh_devices(self):
        self.devices = list_dshow_audio_devices()
        self.device_combo["values"] = self.devices
        if self.device_var.get() not in self.devices:
            self.device_var.set(self.devices[0] if self.devices else "")
        if not self.devices:
            self.add_log("No DirectShow audio inputs found", "Check FFmpeg and reconnect the capture card")

    def refresh_outputs(self):
        previous = self.config.get("output_device", "")
        try:
            outputs = list_windows_output_devices()
        except Exception as error:
            self.output_devices = {}
            self.output_combo["values"] = []
            self.output_var.set("")
            self.add_log("No audio outputs found", str(error))
            return
        self.output_devices = dict(outputs)
        self.output_combo["values"] = list(self.output_devices)
        if previous in self.output_devices:
            selected = previous
        else:
            selected = outputs[0][0] if outputs else ""
        self.output_var.set(selected)
        if selected:
            self.config["output_device"] = selected
            save_config(self.config)

    def output_changed(self, _event=None):
        selected = self.output_var.get()
        if selected in self.output_devices:
            self.config["output_device"] = selected
            save_config(self.config)

    def toggle_monitoring(self):
        if self.enabled:
            self.stop_monitoring()
        else:
            self.start_monitoring()

    def start_monitoring(self):
        device = self.device_var.get()
        try:
            self.matcher.reset()
            self.matcher.set_state(self.session.state)
            self.capture = CaptureWorker(device, self.matcher, lambda message: self.root.after(0, self.capture_error, message))
            self.capture.start()
        except Exception as error:
            self.messagebox.showerror("Could not open audio input", str(error))
            return
        self.enabled = True
        self.device_combo.configure(state="disabled")
        self.toggle.configure(text="●  ONLINE", bg="#153d31", fg="#62e6a9", activebackground="#1d513f", activeforeground="#62e6a9")
        self.config["audio_device"] = device
        save_config(self.config)
        self.add_log("Monitoring enabled", device)

    def stop_monitoring(self):
        if self.capture:
            self.capture.stop()
            self.capture = None
        self.enabled = False
        self.device_combo.configure(state="readonly")
        self.toggle.configure(text="●  OFFLINE", bg="#252b39", fg="#a9b4c9", activebackground="#2d3546", activeforeground=self.TEXT)

    def capture_error(self, message):
        self.stop_monitoring()
        self.messagebox.showerror("Audio input stopped", message)

    def on_match(self, cue, score, _onset, latency_ms):
        occurred = time.monotonic() - latency_ms / 1000
        if cue == "start":
            changed, message = self.session.start(occurred), "Race started"
        elif cue == "shock":
            changed, message = self.session.shock(occurred), "Shock used — cooldown restarted"
        else:
            changed, message = self.session.end(), "FINITO — timer cancelled"
        if changed:
            self.matcher.set_state(self.session.state)
            self.add_log(message, f"audio · {score * 100:.1f}%")

    def tick(self):
        if self.session.tick():
            self.matcher.set_state(self.session.state)
            self.add_log("Shock available", "30-second timer")
            self.play_alert()
        self.draw_timer()
        self.root.after(100, self.tick)

    def draw_timer(self):
        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 360)
        height = max(canvas.winfo_height(), 390)
        cx, cy, radius = width / 2, height / 2 - 5, min(width, height) / 2 - 45
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        canvas.create_oval(*box, outline="#2b3344", width=16)
        progress = 1 if self.session.state == "available" else (1 - self.session.remaining() / 30 if self.session.state == "cooldown" else 0)
        color = self.GREEN if self.session.state == "available" else (self.BLUE if self.session.state == "cooldown" else "#596174")
        if progress:
            canvas.create_arc(*box, start=90, extent=-359.9 * progress, style="arc", outline=color, width=16)
        canvas.create_image(cx, cy - 12, image=self.small_icon)
        label = "SHOCK AVAILABLE" if self.session.state == "available" else ("SHOCK RECHARGING" if self.session.state == "cooldown" else "WAITING FOR RACE")
        canvas.create_text(cx, cy + radius * 0.72, text=label, fill=self.MUTED, font=("Segoe UI", 10, "bold"))
        if self.session.state == "idle":
            self.state_title.configure(text="Listening for race start" if self.enabled else "Ready when you are")
            self.state_detail.configure(text="The timer begins on the GO cue." if self.enabled else "Enable listening to detect the next race.")
        elif self.session.state == "cooldown":
            self.state_title.configure(text="Shock recharging")
            self.state_detail.configure(text="A shock restarts the timer. FINITO cancels it.")
        else:
            self.state_title.configure(text="Shock is available")
            self.state_detail.configure(text="Listening for the next shock or race finish.")

    def sound_name(self):
        sound = self.config.get("alert_sound", "")
        return Path(sound).name if sound else "Default two-tone alert"

    def choose_sound(self):
        filename = self.filedialog.askopenfilename(
            title="Choose the available-alert sound",
            filetypes=[("Audio files", "*.mp3 *.wav *.ogg *.flac *.m4a *.aac *.opus"), ("All files", "*.*")],
        )
        if filename:
            self.config["alert_sound"] = filename
            save_config(self.config)
            self.sound_label.configure(text=self.sound_name())

    def play_alert(self):
        threading.Thread(target=self._play_alert_worker, name="alert-playback", daemon=True).start()

    def _play_alert_worker(self):
        try:
            import sounddevice as sd

            display = self.output_var.get()
            if display not in self.output_devices:
                raise RuntimeError("Select an alert output device first.")
            device_index = self.output_devices[display]
            device = sd.query_devices(device_index)
            sample_rate = int(device["default_samplerate"] or 48000)
            channels = 2 if device["max_output_channels"] >= 2 else 1
            sd.stop()
            custom = self.config.get("alert_sound", "")
            if custom and Path(custom).is_file():
                result = subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", custom, "-ac", str(channels), "-ar", str(sample_rate), "-f", "f32le", "pipe:1"],
                    capture_output=True,
                    check=True,
                    creationflags=CREATE_NO_WINDOW,
                )
                samples = np.frombuffer(result.stdout, dtype="<f4")
                if channels > 1:
                    samples = samples.reshape(-1, channels)
            else:
                first = np.sin(2 * np.pi * 784 * np.arange(int(sample_rate * 0.14)) / sample_rate)
                second = np.sin(2 * np.pi * 1046 * np.arange(int(sample_rate * 0.18)) / sample_rate)
                mono = np.concatenate((first, np.zeros(int(sample_rate * 0.03)), second)).astype(np.float32) * 0.22
                samples = np.repeat(mono[:, None], channels, axis=1) if channels > 1 else mono
            sd.play(samples, sample_rate, device=device_index, blocking=False)
        except Exception as error:
            self.root.after(0, self.add_log, "Could not play alert", str(error))

    def add_log(self, message, detail):
        self.log.configure(state="normal")
        self.log.insert("1.0", f"{message}\n", "title")
        self.log.insert("2.0", f"{time.strftime('%H:%M:%S')} · {detail}\n\n", "detail")
        self.log.configure(state="disabled")

    def close(self):
        self.stop_monitoring()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def analyze_video(video, stream):
    events = []
    matcher = SpectralMatcher(lambda *event: events.append(event))
    process = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", video, "-map", f"0:{stream}", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "pipe:1"],
        stdout=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )
    while True:
        data = process.stdout.read(65536)
        if not data:
            break
        matcher.consume(np.frombuffer(data, dtype="<f4"))
        while events:
            cue, score, onset, latency = events.pop(0)
            print(f"{cue:5}  {onset:8.3f}s  score={score:.4f}  latency={latency / 1000:.3f}s", flush=True)
            matcher.set_state("race" if cue in ("start", "shock") else "idle")
    return process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", metavar="VIDEO", help="offline-test a recording instead of opening the GUI")
    parser.add_argument("--stream", type=int, default=4, help="global FFmpeg audio stream index for --analyze")
    arguments = parser.parse_args()
    if arguments.analyze:
        return analyze_video(arguments.analyze, arguments.stream)
    if os.name != "nt":
        raise SystemExit("This frontend is for Windows. On Linux, run launch-gui.sh.")
    ShockTimerWindows().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
