#!/usr/bin/env bash
# Installer for Voice → Terminal.
#
# Lays the project out the way the code expects:
#   ~/.local/share/vosk-talk/        engines, venv, models
#   ~/.local/bin/                    launchers (voicegui, talk, dictate)
#   /usr/local/bin/tiocsti-inject    setuid-root pts injector (needs sudo)
#   ~/.local/share/applications/     desktop entry
#
# Re-running is safe (idempotent). Vosk models are NOT downloaded here — see the
# bottom of this script / the README for that one manual step.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/vosk-talk"
BIN_DIR="$HOME/.local/bin"
VENV="$APP_DIR/venv"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
DESKTOP_DIR="$HOME/.local/share/applications"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

say "Installing system packages (sudo)…"
sudo apt update
sudo apt install -y python3-venv python3-pyqt5 build-essential \
    xdotool pulseaudio-utils bluez-tools remotinator || \
    say "Some apt packages may be unavailable on this distro — continuing."

say "Copying engines to $APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR" "$ICON_DIR" "$DESKTOP_DIR"
cp "$HERE"/voice_gui.py "$HERE"/aai_print.py "$HERE"/talk.py \
   "$HERE"/dictate.py "$HERE"/recmeter.py "$APP_DIR/"

say "Creating engine virtualenv at $VENV"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$HERE/requirements.txt"

say "Installing launchers to $BIN_DIR"
install -m 755 "$HERE"/bin/voicegui "$HERE"/bin/talk "$HERE"/bin/dictate "$BIN_DIR/"

say "Building + installing the setuid TIOCSTI injector (sudo)"
gcc -O2 -Wall -o "$HERE/tiocsti-inject" "$HERE/tiocsti-inject.c"
sudo install -o root -g root -m 4755 "$HERE/tiocsti-inject" /usr/local/bin/tiocsti-inject
# TIOCSTI is gated on modern kernels; allow it (the helper still restricts to your own pts).
echo 'dev.tty.legacy_tiocsti=1' | sudo tee /etc/sysctl.d/99-tiocsti.conf >/dev/null
sudo sysctl -q -w dev.tty.legacy_tiocsti=1 || true

say "Installing icon + desktop entry"
cp "$HERE/icons/voicegui.svg" "$ICON_DIR/voicegui.svg"
cp "$HERE/voicegui.desktop" "$DESKTOP_DIR/voicegui.desktop"
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

cat <<EOF

Done. Two remaining manual steps:

  1. Vosk models (for the offline engine). Download from
     https://alphacephei.com/vosk/models and unpack so you have, e.g.:
        $APP_DIR/models/en/   (rename the unpacked folder to the lang code)
     Repeat for ru/, uk/, etc.

  2. AssemblyAI key (for the streaming engine). Either:
        export ASSEMBLYAI_API_KEY=...     (in your shell rc), or
        mkdir -p ~/.config/agent-dictate && \\
          printf '%s\\n' YOUR_KEY > ~/.config/agent-dictate/api_key && \\
          chmod 600 ~/.config/agent-dictate/api_key
     (You can also paste + save it in the GUI's "API" tab.)

Launch with:  voicegui   (or pick "Voice → Terminal" from your app menu)
EOF
