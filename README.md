# Shock Timer

A native Windows and Linux race assistant for Mario Kart Wii. It listens to capture-card audio and:

- starts a fixed 30-second shock cooldown at race start;
- plays a user-selected alert when shock becomes available;
- restarts the cooldown when a shock is used;
- cancels the timer when the `FINITO!` race-finish cue plays.

Audio stays on the computer. The detector does not use a network service.

## Windows setup

1. Install [Python 3.11 or newer](https://www.python.org/downloads/windows/) and enable **Add Python to PATH** during installation.
2. Double-click `setup-windows.bat`. It creates an isolated `.venv`, installs NumPy, and installs FFmpeg through `winget` if needed.
3. Double-click `launch-windows.bat`.
4. Select the capture card under **Audio input**, choose an optional alert sound, and click **Enable listening**.

Windows settings are saved to `%APPDATA%\ShockTimer\config.json`; `windows-config.example.json` documents its fields. Audio capture uses FFmpeg's DirectShow input, so the capture card must appear as a Windows recording device.

## Linux requirements

- Python 3
- GTK 3 Python bindings (`python3-gi`)
- FFmpeg and FFplay
- PulseAudio/PipeWire compatibility tools (`pactl`)
- FFTW 3

On Debian/Ubuntu-based systems, the corresponding packages are commonly installed with:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 ffmpeg pulseaudio-utils libfftw3-3
```

No `pip` packages are required.

## Run on Linux

```bash
git clone https://github.com/HumbleKingIsBlack/mkwii-shock-timer.git
cd mkwii-shock-timer
./launch-gui.sh
```

Choose the capture-card source, optionally select an alert sound, and enable listening with the switch in the top-right corner.

## Offline detector test

The detector can replay a recording without opening the GUI. The stream number is the global FFmpeg stream index:

```bash
./shock_timer_gui.py --analyze race-recording.mp4 --stream 4
```

The calibration recording produced exactly one accepted start, shock, and finish event, with no extra triggers.

## Included cues

- `assets/cues/race-start.wav` — `SE_RC_GO`
- `assets/cues/shock-used.wav` — `SE_ITM_THNDR_USE`
- `assets/cues/race-end.wav` — the universal effect synchronized with `FINITO!`

Archive mappings and checksums are recorded in `assets/cues/cue-map.json`.

Mario Kart and related game assets are property of their respective owners. This is an unofficial fan-made utility.
