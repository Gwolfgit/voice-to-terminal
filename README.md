# Voice → Terminal

Hands-free dictation that streams your speech **straight into a chosen terminal
tab** — not just the focused window. Hit Start once and just talk: voice-activity
detection picks out your speech and transcribes it live as you go (no key to
hold, no per-phrase button). Pin a Terminator tab once and keep working
anywhere else while transcribed text flows into that pinned shell (handy for
talking to a coding-agent CLI). A PyQt5 tray app drives two interchangeable
transcription engines:

- **Offline (Vosk)** — fully local models, no network, no API key.
- **AssemblyAI** — Universal-Streaming API for higher accuracy (needs a key).

Linux + X11 only.

---

## Why it's different

Most dictation tools type into whatever window currently has focus. This one can
inject text **directly into a specific pseudo-terminal** via a small setuid
`TIOCSTI` helper, so the text lands in the *pinned* shell no matter which window
you're actually looking at. Unpinned, it falls back to typing into the focused
window with `xdotool`.

It's also cost-aware for AssemblyAI: that service bills by how long the streaming
socket stays **open** (idle time included), so the engine stays dormant until it
hears speech, opens one connection for the talking burst, and closes it after a
spell of silence. The GUI shows a live running total of billable connection time.

## Tray quick-switcher

Right-click the tray icon for a quick-switch menu:

- **Show / Hide** the control window.
- **Start / Stop Listening** — toggle transcription without opening the window.
- **Pin a terminal** — a live, checkable list of every open Terminator terminal.
  Click one to pin it (it gets a check mark); pick **Focused window (no pin)** to
  go back to typing into whatever's focused. The list rebuilds each time you open
  the menu, so newly-opened tabs show up immediately.
- **Quit**.

The pin you choose here stays in sync with the **Dictate** tab in the main window.

## Components

| File | Role |
|------|------|
| `voice_gui.py`     | PyQt5 control panel + tray app (the main entry point) |
| `aai_print.py`     | AssemblyAI Universal-Streaming engine (`--stream` to stdout) |
| `talk.py`          | Vosk dictation engine (type or `--print`) |
| `dictate.py`       | Standalone continuous Vosk dictation CLI |
| `recmeter.py`      | Mic-test recorder with live level metering |
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
- [Terminator](https://gnome-terminator.org/) for the pin-a-tab feature
  (`remotinator` provides tab titles). Without it, dictation still types into the
  focused window.
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
matters to you. If you'd rather not install it, leave the target **unpinned** and
the app types into the focused window instead.

## License

MIT — see [LICENSE](LICENSE).
