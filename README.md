# Voice → Terminal

Hands-free dictation that streams your speech into one of two **delivery modes**.
Hit Start once and just talk: voice-activity detection picks out your speech and
transcribes it live as you go (no key to hold, no per-phrase button). A PyQt5
tray app drives two interchangeable transcription engines:

- **Offline (Vosk)** — fully local models, no network, no API key.
- **AssemblyAI** — Universal-Streaming API for higher accuracy (needs a key).

The two delivery modes, switchable in the Dictate tab or from the tray:

- **Terminal** — type into a pinned Terminator terminal (tab/split), injected
  straight into that shell via a setuid `TIOCSTI` helper, no matter which window
  is focused.
- **Active window** — type wherever your cursor is: the focused window, right at
  the current cursor position (via `xdotool`).

While listening you can **Pause** instead of Stop: pausing simply discards
incoming audio and keeps the model loaded (no long model-reload wait on resume),
and a **Noise** tab lets you set a noise-floor gate for noisy environments.

Linux + X11 only.

---

## Why it's different

Most dictation tools type into whatever window currently has focus. This one has
two explicit modes:

- **Terminal mode** injects text **directly into a specific pseudo-terminal** via
  a small setuid `TIOCSTI` helper, so the text lands in the *pinned* shell no
  matter which window you're actually looking at — handy for talking to a
  coding-agent CLI while you work elsewhere.
- **Active-window mode** types with `xdotool` into the focused window, exactly
  where the cursor is — the usual dictation behavior.

It's also cost-aware for AssemblyAI: that service bills by how long the streaming
socket stays **open** (idle time included), so the engine stays dormant until it
hears speech, opens one connection for the talking burst, and closes it after a
spell of silence. The GUI shows a live running total of billable connection time.

## Tray quick-switcher

Right-click the tray icon for a quick-switch menu:

- **Show / Hide** the control window.
- **Start / Stop Listening** — toggle transcription without opening the window.
- **Pause / Resume** (while listening) — pause audio processing without
  unloading the model.
- **Delivery mode** — switch between **Terminal (pinned)** and
  **Active window (cursor)**. Same switch as the Dictate tab.
- **Pin a terminal** — a live, checkable list of every open Terminator terminal.
  Click one to pin it and switch to Terminal mode (it gets a check mark);
  choose **Unpin terminal** to drop the pin (which switches to Active-window
  mode). The list rebuilds each time you open the menu, so newly-opened tabs
  show up immediately.
- **Quit**.

The mode and pin you choose here stay in sync with the **Dictate** tab in the
main window.

## Noise floor (noisy environments)

The **Noise** tab has a vertical threshold slider and a live input-level meter.
Open the tab and talk: the meter shows your current mic level in real time, the
orange line is the noise-floor threshold, and the status flips between
**RECEIVED** (green — above the floor, will be transcribed) and **GATED**
(red — below the floor, treated as noise). Move the slider until your
background noise stays GATED and your voice is RECEIVED.

The gate works for both engines:

- **Vosk** — frames quieter than the threshold are replaced with silence before
  recognition, so noise doesn't get transcribed.
- **AssemblyAI** — the manual threshold replaces its automatic noise
  calibration.

Uncheck **Enable noise gate** to go back to no gating (Vosk) or automatic
calibration (AssemblyAI). The meter pauses while you're actually listening
(the engine needs the mic to itself); use the tab to set the floor, then start
listening on the Dictate tab.

## Filler-word filter (transient sounds)

A chair creak, a keyboard clack, or a dropped mug is sometimes loud enough to
pass the noise gate — and the transcriber renders it as a lone **"the"** (or
"a", "um", …). Over a long working session those stray words accumulate.

The **Dictate** tab has a **Filter isolated filler words** checkbox (on by
default). When a finalized segment consists only of filler words, it is held
for the **grace** period (default 1.5 s) instead of being typed immediately:

- if real speech follows within the grace period, the held word is typed first
  and then the real speech — order is preserved;
- if silence follows (or the session ends), the held word is discarded;
- a chain of filler segments is never typed — each one replaces the previous
  held word until real speech arrives.

Uncheck the box to disable the filter entirely. The word list defaults to
`the, a, an, um, uh, hmm` and can be overridden by setting `filler_words` in
`~/.config/voice-gui/config.json` (a JSON list of lowercase words). The grace
period is adjustable in the UI (0.5–5 s) and stored as `filler_grace`.

## Components

| File | Role |
|------|------|
| `voice_gui.py`     | PyQt5 control panel + tray app (the main entry point) |
| `aai_print.py`     | AssemblyAI Universal-Streaming engine (`--stream` to stdout) |
| `talk.py`          | Vosk dictation engine (type or `--print`) |
| `dictate.py`       | Standalone continuous Vosk dictation CLI |
| `recmeter.py`      | Mic-test recorder with live level metering (also the Noise-tab `--meter` source) |
| `tiocsti-inject.c` | setuid-root helper that injects bytes into your own `/dev/pts/*` |
| `bin/`             | `voicegui`, `talk`, `dictate` launchers |

## Install

```bash
git clone https://github.com/Gwolfgit/voice-to-terminal.git
cd voice-to-terminal
./install.sh
```

`install.sh` installs the system packages, builds the venv, compiles + installs
the setuid injector (asks for sudo), and drops the launchers, icon, and desktop
entry into place. Then do the two manual steps it prints:

1. **Vosk models** (offline engine): download from
   <https://alphacephei.com/vosk/models> and unpack into
   `~/.local/share/vosk-talk/models/<lang>/` (e.g. `models/en/`).
2. **AssemblyAI key** (streaming engine): export `ASSEMBLYAI_API_KEY`, or write it
   to `~/.config/agent-dictate/api_key` (chmod 600), or paste it in the GUI's
   **API** tab. **No key is bundled in this repo** — supply your own.

Launch with `voicegui`, or pick **Voice → Terminal** from your app menu.

## Requirements

- Linux on **X11** (keystroke injection uses `xdotool`; Wayland won't work).
- [Terminator](https://gnome-terminator.org/) for the Terminal delivery mode
  (`remotinator` provides tab titles). Without it, use **Active window** mode and
  dictation types wherever your cursor is.
- PulseAudio/PipePulse (`parec`/`pactl`) for mic capture.
- System `python3` + `python3-pyqt5` for the GUI; a venv for the engines.

## Optional: auto-arm a Bluetooth headset mic

The `talk` launcher can connect a Bluetooth headset and switch it to its
mic-capable HSP/HFP profile before listening. It's **off by default**; enable it
by exporting your headset's MAC:

```bash
export TALK_BT_MAC="AA:BB:CC:DD:EE:FF"
```

Set `TALK_NO_BT=1` to force-skip it.

## Security note on `tiocsti-inject`

`TIOCSTI` lets a process push characters into a terminal's input as if typed. The
helper is installed **setuid root** because the kernel requires elevated
privileges to write into a terminal that isn't the caller's controlling tty. To
contain that, the helper refuses any target that isn't a `/dev/pts/*` device
**owned by the real (invoking) user** — so you can only ever inject into your own
terminals. Read `tiocsti-inject.c` (it's ~60 lines) before installing if that
matters to you. If you'd rather not install it, use **Active window** mode and
the app types wherever your cursor is instead.

## License

MIT — see [LICENSE](LICENSE).
