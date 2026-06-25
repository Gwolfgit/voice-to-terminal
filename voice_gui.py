#!/usr/bin/env python3
"""Voice → Terminal control GUI.

A PyQt5 control panel for hands-free dictation with two interchangeable
transcription engines:

  * Vosk        — fully offline (local models, no network, no key).
  * AssemblyAI  — Universal-Streaming API (needs an API key).

The final transcript is delivered to a target you choose:

  * A *pinned* Terminator terminal (tab/split), identified by its pseudo-
    terminal. Text is injected straight into that pts via a setuid TIOCSTI
    helper, so it lands in that same shell no matter which tab/window is
    focused — keep working elsewhere while dictation flows into the pinned tab.
  * Otherwise the currently *focused* X11 window, typed with xdotool.

Tabs:
  Dictate — engine, target/pin, listen, live transcript.
  API     — AssemblyAI key (save) + validate-key test (no audio).
  Voice   — input-device selection + a mic level test.
"""
import array
import json
import math
import os
import re
import signal
import subprocess
import time
import wave

from PyQt5 import QtGui, QtWidgets
from PyQt5.QtCore import Qt, QEvent, QProcess, QProcessEnvironment, QTimer

DEBUG_LOG = "/tmp/voicegui-debug.log"


def dbg(msg):
    """Append a timestamped debug line (also to stderr/journal)."""
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print("DBG", line, flush=True)

HOME = os.path.expanduser("~")
TALK = os.path.join(HOME, ".local/bin/talk")
VENVPY = os.path.join(HOME, ".local/share/vosk-talk/venv/bin/python")
AAI = os.path.join(HOME, ".local/share/vosk-talk/aai_print.py")
RECMETER = os.path.join(HOME, ".local/share/vosk-talk/recmeter.py")
MODELS = os.path.join(HOME, ".local/share/vosk-talk/models")
KEY_FILE = os.path.join(HOME, ".config/agent-dictate/api_key")
CONFIG = os.path.join(HOME, ".config/voice-gui/config.json")
REC_FILE = "/tmp/voicegui-rec.wav"
ICON_PATH = os.path.join(HOME, ".local/share/icons/hicolor/scalable/apps/voicegui.svg")
INJECTOR = "/usr/local/bin/tiocsti-inject"
AAI_CHECK_URL = "https://api.assemblyai.com/v2/transcript?limit=1"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG)


USAGE_FILE = os.path.join(HOME, ".config/voice-gui/usage.json")


def load_usage_seconds():
    try:
        with open(USAGE_FILE) as f:
            return float(json.load(f).get("assemblyai_seconds", 0.0))
    except Exception:
        return 0.0


def save_usage_seconds(seconds):
    os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
    tmp = USAGE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"assemblyai_seconds": seconds}, f)
    os.replace(tmp, USAGE_FILE)


def fmt_hms(seconds):
    s = int(seconds)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------- #
# Terminal discovery (each Terminator terminal is a child shell on its own pts)
# --------------------------------------------------------------------------- #
def enumerate_terminals():
    try:
        out = subprocess.check_output(
            ["ps", "-e", "-o", "pid=,ppid=,tpgid=,tty=,args="],
            text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []

    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, tpgid, tty, args = parts
        try:
            procs[int(pid)] = {"pid": int(pid), "ppid": int(ppid),
                               "tpgid": int(tpgid), "tty": tty, "args": args}
        except ValueError:
            continue

    term_pids = {p["pid"] for p in procs.values()
                 if re.search(r"(^|/)terminator(\s|$)", p["args"])}
    if not term_pids:
        return []

    terminals = []
    for p in procs.values():
        if p["ppid"] in term_pids and p["tty"].startswith("pts/"):
            fg = procs.get(p["tpgid"], p)
            cmd = _short_cmd(fg["args"])
            title = _tab_title(_terminator_uuid(p["pid"]))
            if title:
                label = f"{title}   ·   {p['tty']}"
            else:
                cwd = _proc_cwd(fg["pid"]) or _proc_cwd(p["pid"]) or "?"
                label = f"{p['tty']}  —  {cmd}  ({os.path.basename(cwd) or cwd})"
            terminals.append({
                "pts": "/dev/" + p["tty"], "tty": p["tty"],
                "title": title, "cmd": cmd, "label": label,
            })
    terminals.sort(key=lambda t: t["tty"])
    return terminals


def _terminator_uuid(pid):
    """The TERMINATOR_UUID env var the terminator process sets in each tab's shell."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            for kv in f.read().split(b"\0"):
                if kv.startswith(b"TERMINATOR_UUID="):
                    return kv[len(b"TERMINATOR_UUID="):].decode()
    except OSError:
        pass
    return None


def _tab_title(uuid):
    """Terminator tab title for a terminal UUID, via remotinator (DBus)."""
    if not uuid:
        return None
    try:
        t = subprocess.check_output(["remotinator", "-u", uuid, "get_tab_title"],
                                    text=True, stderr=subprocess.DEVNULL, timeout=3)
        # strip a leading braille/spinner glyph some prompts prepend
        t = re.sub(r"^[⠀-⣿\s]+", "", t.strip())
        return t.strip() or None
    except Exception:
        return None


def _short_cmd(args, n=44):
    args = re.sub(r"^\S*/(python3?|node|uv)\s+", "", args.strip())
    return (args[: n - 1] + "…") if len(args) > n else args


def _proc_cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def injector_available():
    return os.access(INJECTOR, os.X_OK)


def inject_tiocsti(pts_path, text, press_enter):
    data = text.encode("utf-8") + (b"\n" if press_enter else b"")
    r = subprocess.run([INJECTOR, pts_path], input=data, stderr=subprocess.PIPE)
    dbg(f"inject_tiocsti pts={pts_path} bytes={len(data)} rc={r.returncode} "
        f"err={r.stderr.decode('utf-8', 'replace').strip()!r}")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()
                           or f"injector exited {r.returncode}")


def type_xdotool(text, press_enter):
    subprocess.run(["xdotool", "type", "--clearmodifiers", "--", text], check=True)
    if press_enter:
        subprocess.run(["xdotool", "key", "Return"], check=True)


def list_pulse_sources():
    """Return [{name, desc, spec, state}] for real (non-monitor) input sources."""
    try:
        out = subprocess.check_output(["pactl", "list", "sources"], text=True,
                                      stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        return []
    blocks, cur = [], {}
    for line in out.splitlines():
        if re.match(r"^Source #\d+", line):
            if cur:
                blocks.append(cur)
            cur = {}
        else:
            m = re.match(r"\s*(Name|Description|Sample Specification|State):\s*(.*)", line)
            if m:
                cur[m.group(1)] = m.group(2)
    if cur:
        blocks.append(cur)
    res = []
    for b in blocks:
        name = b.get("Name", "")
        if not name or name.endswith(".monitor"):
            continue
        res.append({"name": name, "desc": b.get("Description", name),
                    "spec": b.get("Sample Specification", ""),
                    "state": b.get("State", "")})
    return res


def default_source():
    try:
        return subprocess.check_output(["pactl", "get-default-source"],
                                       text=True, timeout=5).strip()
    except Exception:
        return ""


def announce(text):
    """Speak a short confirmation via speech-dispatcher (fire and forget)."""
    try:
        subprocess.Popen(["spd-say", "-t", "female1", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def wav_duration(path):
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


def wav_peak_db(path):
    try:
        with wave.open(path, "rb") as w:
            frames = w.readframes(w.getnframes())
        if not frames:
            return -120.0
        a = array.array("h")
        a.frombytes(frames)
        peak = max((abs(x) for x in a), default=0)
        return 20 * math.log10(peak / 32768.0) if peak > 0 else -120.0
    except Exception:
        return -120.0


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
class VoiceGui(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.proc = None          # listening QProcess
        self.rec_proc = None      # mic-test recorder QProcess
        self._procs = []          # keep refs to one-shot aux processes alive
        self.out_buf = ""
        self.pinned = None
        self._announced = False
        self.sources = []
        self._usage_total = load_usage_seconds()   # billable AssemblyAI seconds
        self._conn_start = None                     # wall-clock of current connection
        self._usage_timer = QTimer(self)
        self._usage_timer.setInterval(1000)
        self._usage_timer.timeout.connect(self._refresh_usage_label)
        self._loading = True      # suppress config writes during initial setup
        self._tray_notified = False
        self.setWindowIcon(QtGui.QIcon(ICON_PATH))
        self._build_ui()
        self._build_tray()
        self._load_into_ui()
        self.refresh_terminals()
        self.refresh_devices()
        self._loading = False
        if not injector_available():
            self.set_status("⚠ tiocsti-inject helper missing — pinned injection "
                            "won't work; focused typing still will.")

    # ---- UI -------------------------------------------------------------- #
    def _build_ui(self):
        self.setWindowTitle("Voice → Terminal")
        self.setMinimumWidth(470)
        outer = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        outer.addWidget(self.tabs)
        self.tabs.addTab(self._tab_dictate(), "Dictate")
        self.tabs.addTab(self._tab_api(), "API")
        self.tabs.addTab(self._tab_voice(), "Voice")

        self.status = QtWidgets.QLabel("Ready.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #888;")
        outer.addWidget(self.status)

        self.usage_label = QtWidgets.QLabel()
        self.usage_label.setStyleSheet("color: #555; font-size: 11px;")
        self.usage_label.setToolTip("Total AssemblyAI streaming connection time "
                                    "(what they bill by). Ticks live while listening.")
        outer.addWidget(self.usage_label)
        self._refresh_usage_label()

        # Critical: if dictation types into the focused GUI (unpinned), a streamed
        # space/Enter must NOT activate a button (e.g. press Stop on itself). Deny
        # keyboard focus to all controls so typed text can never trigger them.
        for cls in (QtWidgets.QPushButton, QtWidgets.QCheckBox, QtWidgets.QComboBox):
            for wdg in self.findChildren(cls):
                wdg.setFocusPolicy(Qt.NoFocus)

    def _tab_dictate(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Engine:"))
        self.engine = QtWidgets.QComboBox()
        self.engine.addItem("Offline (Vosk)", "vosk")
        self.engine.addItem("AssemblyAI (API)", "assemblyai")
        self.engine.currentIndexChanged.connect(self._engine_changed)
        row.addWidget(self.engine)
        row.addSpacing(12)
        self.lang_label = QtWidgets.QLabel("Language:")
        row.addWidget(self.lang_label)
        self.lang = QtWidgets.QComboBox()
        self.lang.addItems(sorted(os.listdir(MODELS)) if os.path.isdir(MODELS) else ["en"])
        self.lang.currentIndexChanged.connect(self._save_settings)
        row.addWidget(self.lang)
        row.addStretch(1)
        v.addLayout(row)

        self.auto_enter = QtWidgets.QCheckBox("Press Enter after sending")
        self.auto_enter.stateChanged.connect(self._save_settings)
        v.addWidget(self.auto_enter)

        box = QtWidgets.QGroupBox("Target")
        bv = QtWidgets.QVBoxLayout(box)
        trow = QtWidgets.QHBoxLayout()
        self.targets = QtWidgets.QComboBox()
        self.targets.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                   QtWidgets.QSizePolicy.Preferred)
        trow.addWidget(self.targets, 1)
        rb = QtWidgets.QPushButton("⟳")
        rb.setFixedWidth(34)
        rb.setToolTip("Refresh terminal list")
        rb.clicked.connect(self.refresh_terminals)
        trow.addWidget(rb)
        bv.addLayout(trow)
        prow = QtWidgets.QHBoxLayout()
        self.pin_btn = QtWidgets.QPushButton("📌 Pin selected")
        self.pin_btn.clicked.connect(self.pin_selected)
        unpin = QtWidgets.QPushButton("Unpin")
        unpin.clicked.connect(self.unpin)
        prow.addWidget(self.pin_btn)
        prow.addWidget(unpin)
        bv.addLayout(prow)
        self.pin_label = QtWidgets.QLabel()
        self.pin_label.setWordWrap(True)
        bv.addWidget(self.pin_label)
        v.addWidget(box)

        self.listen_btn = QtWidgets.QPushButton("🎤  Start Listening")
        self.listen_btn.setMinimumHeight(48)
        f = self.listen_btn.font(); f.setPointSize(f.pointSize() + 2)
        self.listen_btn.setFont(f)
        self.listen_btn.clicked.connect(self.toggle_listen)
        v.addWidget(self.listen_btn)

        self.transcript = QtWidgets.QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("Live transcript appears here…")
        self.transcript.setMaximumHeight(90)
        v.addWidget(self.transcript)
        return w

    def _tab_api(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.addWidget(QtWidgets.QLabel(
            "AssemblyAI Universal-Streaming API key.\nStored at "
            "~/.config/agent-dictate/api_key (shared with agent-dictate)."))
        krow = QtWidgets.QHBoxLayout()
        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key_edit.setPlaceholderText("paste AssemblyAI API key")
        krow.addWidget(self.key_edit, 1)
        self.show_key = QtWidgets.QCheckBox("Show")
        self.show_key.stateChanged.connect(
            lambda s: self.key_edit.setEchoMode(
                QtWidgets.QLineEdit.Normal if s else QtWidgets.QLineEdit.Password))
        krow.addWidget(self.show_key)
        v.addLayout(krow)

        brow = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save key")
        save.clicked.connect(self.save_key)
        self.validate_btn = QtWidgets.QPushButton("Validate key (no audio)")
        self.validate_btn.clicked.connect(self.validate_key)
        brow.addWidget(save)
        brow.addWidget(self.validate_btn)
        v.addLayout(brow)

        self.api_result = QtWidgets.QLabel()
        self.api_result.setWordWrap(True)
        v.addWidget(self.api_result)
        v.addStretch(1)
        return w

    def _tab_voice(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        drow = QtWidgets.QHBoxLayout()
        drow.addWidget(QtWidgets.QLabel("Input device:"))
        self.device = QtWidgets.QComboBox()
        self.device.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                  QtWidgets.QSizePolicy.Preferred)
        self.device.currentIndexChanged.connect(self._on_device_changed)
        drow.addWidget(self.device, 1)
        rb = QtWidgets.QPushButton("⟳")
        rb.setFixedWidth(34)
        rb.setToolTip("Refresh device list")
        rb.clicked.connect(self.refresh_devices)
        drow.addWidget(rb)
        v.addLayout(drow)

        self.dev_info = QtWidgets.QLabel()
        self.dev_info.setWordWrap(True)
        self.dev_info.setStyleSheet("color: #555;")
        v.addWidget(self.dev_info)

        box = QtWidgets.QGroupBox("Mic test (record, then play it back)")
        bv = QtWidgets.QVBoxLayout(box)
        trow = QtWidgets.QHBoxLayout()
        self.rec_btn = QtWidgets.QPushButton("●  Record")
        self.rec_btn.clicked.connect(self.toggle_record)
        self.play_btn = QtWidgets.QPushButton("▶  Play back")
        self.play_btn.setEnabled(os.path.exists(REC_FILE))
        self.play_btn.clicked.connect(self.play_recording)
        trow.addWidget(self.rec_btn)
        trow.addWidget(self.play_btn)
        bv.addLayout(trow)
        self.mic_bar = QtWidgets.QProgressBar()
        self.mic_bar.setRange(0, 100)
        self.mic_bar.setTextVisible(False)
        bv.addWidget(self.mic_bar)
        self.mic_result = QtWidgets.QLabel("Press Record, speak, then Stop — "
                                           "then Play back to hear yourself.")
        self.mic_result.setWordWrap(True)
        bv.addWidget(self.mic_result)
        v.addWidget(box)
        v.addStretch(1)
        return w

    def _on_device_changed(self, *a):
        self._save_settings()
        self._update_device_info()

    def _update_device_info(self):
        name = self.device.currentData()
        if not name:
            self.dev_info.setText("Capture resamples to 16 kHz mono via PulseAudio "
                                  "(parec) regardless of the device's native rate.")
            return
        s = next((x for x in self.sources if x["name"] == name), None)
        if s:
            self.dev_info.setText(
                f"<b>{s['spec']}</b> · state: {s['state']}<br>"
                f"<small>{s['name']}</small><br>"
                f"Captured at 16 kHz mono (auto-resampled).")

    # ---- settings load/save --------------------------------------------- #
    def _load_into_ui(self):
        i = self.engine.findData(self.cfg.get("engine", "vosk"))
        self.engine.setCurrentIndex(max(0, i))
        li = self.lang.findText(self.cfg.get("lang", "en"))
        if li >= 0:
            self.lang.setCurrentIndex(li)
        self.auto_enter.setChecked(bool(self.cfg.get("auto_enter", False)))
        try:
            self.key_edit.setText(open(KEY_FILE).read().strip())
        except OSError:
            pass
        self._engine_changed()

    def _save_settings(self, *a):
        if getattr(self, "_loading", False):
            return
        self.cfg.update({
            "engine": self.engine.currentData(),
            "lang": self.lang.currentText(),
            "auto_enter": self.auto_enter.isChecked(),
            "source": self.device.currentData(),
        })
        save_config(self.cfg)

    def _engine_changed(self, *a):
        is_vosk = self.engine.currentData() == "vosk"
        self.lang.setEnabled(is_vosk)
        self.lang_label.setEnabled(is_vosk)
        self._save_settings()

    # ---- terminals / devices -------------------------------------------- #
    def refresh_terminals(self):
        self.terminals = enumerate_terminals()
        self.targets.clear()
        if not self.terminals:
            self.targets.addItem("(no Terminator terminals found)")
            self.targets.setEnabled(False)
            self.pin_btn.setEnabled(False)
        else:
            self.targets.setEnabled(True)
            self.pin_btn.setEnabled(True)
            for t in self.terminals:
                self.targets.addItem(t["label"], t)
            if self.pinned:
                for i, t in enumerate(self.terminals):
                    if t["pts"] == self.pinned["pts"]:
                        self.targets.setCurrentIndex(i)
                        break
        self._update_pin_label()

    def refresh_devices(self):
        want = self.cfg.get("source")
        self.sources = list_pulse_sources()
        ds = default_source()
        self.device.blockSignals(True)
        self.device.clear()
        self.device.addItem(f"System default ({ds or 'unknown'})", None)
        for s in self.sources:
            self.device.addItem(s["desc"], s["name"])
        if want:
            j = self.device.findData(want)
            if j >= 0:
                self.device.setCurrentIndex(j)
        self.device.blockSignals(False)
        self._update_device_info()

    def pin_selected(self):
        t = self.targets.currentData()
        if t:
            self.pinned = t
            self._update_pin_label()
            self.set_status(f"Pinned {t['tty']}.")
            dbg(f"pinned {t['pts']} ({t['label']})")

    def unpin(self):
        self.pinned = None
        self._update_pin_label()
        self.set_status("Unpinned — will type into the focused window.")

    def _update_pin_label(self):
        if self.pinned:
            name = self.pinned.get("title") or self.pinned.get("cmd") or ""
            self.pin_label.setText(f"📌 Pinned → <b>{name}</b> ({self.pinned['tty']})")
            self.pin_label.setStyleSheet("color: #2e7d32;")
        else:
            self.pin_label.setText("Not pinned → text goes to the focused window (xdotool).")
            self.pin_label.setStyleSheet("color: #888;")

    # ---- listening ------------------------------------------------------- #
    def _engine_cmd(self):
        src = self.device.currentData()    # PulseAudio source name, or None=default
        if self.engine.currentData() == "assemblyai":
            args = [AAI, "--print"]
            if src:
                args += ["--source", src]
            return VENVPY, args, None
        # Vosk: go through parec via --source (Bluetooth-safe). For the default
        # device, resolve the current default source name; if that fails, fall
        # back to talk.py's built-in sounddevice default.
        args = ["--print", self.lang.currentText()]
        ds = src or default_source()
        if ds:
            args += ["--source", ds]
        return TALK, args, {"TALK_NO_BT": "1"}

    def toggle_listen(self):
        if self.proc is None:
            self.start_listen()
        else:
            self.stop_listen()

    def start_listen(self):
        self._announced = False
        self._stdout_buf = ""        # line-buffer for finalized segments
        self._injected_any = False
        self._seg_count = 0
        self.transcript.clear()
        prog, args, env = self._engine_cmd()
        args = args + ["--stream"]   # emit finalized segments live
        self.proc = QProcess(self)
        self.proc.setProgram(prog)
        self.proc.setArguments(args)
        if env:
            pe = QProcessEnvironment.systemEnvironment()
            for k, v in env.items():
                pe.insert(k, v)
            self.proc.setProcessEnvironment(pe)
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(self._on_finished)
        self.proc.start()
        self.listen_btn.setText("■  Stop")
        if self.pinned:
            self.set_status("Starting…")
        else:
            self.set_status("⚠ No tab pinned — pin one (Target box) so text goes "
                            "to that terminal, not the focused window.")
        dbg(f"start_listen engine={self.engine.currentData()} "
            f"prog={prog} args={args} pinned={self.pinned['pts'] if self.pinned else None} "
            f"auto_enter={self.auto_enter.isChecked()}")

    def stop_listen(self):
        if self.proc is not None:
            self.set_status("Finalizing…")
            self.proc.terminate()      # SIGTERM → engine flushes & exits

    def _on_stdout(self):
        # Each complete line is a finalized segment; inject it as it arrives.
        chunk = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        dbg(f"stdout chunk {chunk!r}")
        self._stdout_buf += chunk
        while "\n" in self._stdout_buf:
            line, self._stdout_buf = self._stdout_buf.split("\n", 1)
            seg = line.strip()
            if seg:
                self._emit_segment(seg)

    def _emit_segment(self, seg):
        piece = seg + " "
        dbg(f"emit_segment {seg!r} pinned={self.pinned['pts'] if self.pinned else None}")
        try:
            if self.pinned:
                inject_tiocsti(self.pinned["pts"], piece, False)
            else:
                type_xdotool(piece, False)
                dbg("xdotool type done (focused window)")
        except Exception as e:
            dbg(f"emit_segment FAILED: {e}")
            self.set_status(f"Inject failed ({e}) — stopping.")
            self.stop_listen()
            return
        self._injected_any = True
        self._seg_count += 1
        self.transcript.setPlainText((self.transcript.toPlainText() + piece))
        tgt = self.pinned["tty"] if self.pinned else "focused window"
        self.set_status(f"🎙 Streaming → {tgt} ({self._seg_count} segment"
                        f"{'s' if self._seg_count != 1 else ''})")

    def _on_stderr(self):
        text = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            dbg(f"engine stderr: {s!r}")
            if s.startswith("…"):
                # live partial preview (not yet finalized / injected)
                tgt = self.pinned["tty"] if self.pinned else "focused"
                self.set_status(f"🎙 [{tgt}] … {s.lstrip('… ').strip()}")
            elif s.startswith("[connected"):
                if not self._announced:
                    announce("Connected")
                    self._announced = True
                self.set_status("Connected to AssemblyAI.")
                if self._conn_start is None:        # start the live usage clock
                    self._conn_start = time.time()
                    self._usage_timer.start()
            elif s.startswith("[usage]"):
                m = re.search(r"seconds=([\d.]+)", s)
                if m:
                    self._usage_total += float(m.group(1))
                    save_usage_seconds(self._usage_total)
                self._conn_start = None
                self._usage_timer.stop()
                self._refresh_usage_label()
            elif s.startswith("[listening"):
                self.set_status("🎙  Listening — speak; text streams in live.")
                if not self._announced:
                    announce("Ready")
                    self._announced = True
            elif s.startswith("[connecting"):
                self.set_status("Connecting to AssemblyAI…")
            elif s.startswith("[loading"):
                self.set_status("Loading model…")
            elif s.startswith("[calibrating"):
                self.set_status("Calibrating mic — stay quiet a moment…")
            elif s.startswith("[noise floor") or s.startswith("[vad threshold"):
                self.set_status("Ready — speak.")
            elif s.startswith("[capture") or s.startswith("[mic dropped"):
                self.set_status("Mic dropped — reconnecting…")
            elif s.startswith("[no AssemblyAI API key"):
                self.set_status("No API key — set it on the API tab.")
            elif s.startswith("[warning"):
                self.set_status(s.strip("[]"))

    def _on_finished(self):
        # drain any buffered output still queued (may hold the last segment + [usage])
        try:
            self._on_stdout()
            self._on_stderr()
        except Exception:
            pass
        dbg(f"on_finished injected_any={self._injected_any} segs={self._seg_count} "
            f"leftover_buf={self._stdout_buf!r}")
        # flush any trailing buffered segment, then press Enter once if asked
        if self._stdout_buf.strip():
            self._emit_segment(self._stdout_buf.strip())
            self._stdout_buf = ""
        self.proc = None
        self.listen_btn.setText("🎤  Start Listening")
        # finalize usage if the engine didn't emit a [usage] line (crash/kill)
        self._usage_timer.stop()
        if self._conn_start is not None:
            self._usage_total += time.time() - self._conn_start
            save_usage_seconds(self._usage_total)
            self._conn_start = None
        self._refresh_usage_label()
        if self._injected_any and self.auto_enter.isChecked():
            try:
                if self.pinned:
                    inject_tiocsti(self.pinned["pts"], "", True)   # newline only
                else:
                    subprocess.run(["xdotool", "key", "Return"], check=False)
            except Exception:
                pass
        if not self._injected_any:
            self.set_status("No speech recognized.")
        else:
            tgt = self.pinned["tty"] if self.pinned else "focused window"
            self.set_status(f"Done — streamed {self._seg_count} segment(s) → {tgt}.")

    # ---- API key --------------------------------------------------------- #
    def save_key(self):
        key = self.key_edit.text().strip()
        if not key:
            self.api_result.setText("Nothing to save (key is empty).")
            return
        os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
        with open(KEY_FILE, "w") as f:
            f.write(key + "\n")
        os.chmod(KEY_FILE, 0o600)
        self.api_result.setText(f"Saved key to {KEY_FILE} (chmod 600).")
        self.api_result.setStyleSheet("color: #2e7d32;")

    def validate_key(self):
        key = self.key_edit.text().strip()
        if not key:
            self.api_result.setText("Enter a key first.")
            self.api_result.setStyleSheet("color: #b00;")
            return
        self.validate_btn.setEnabled(False)
        self.api_result.setText("Validating…")
        self.api_result.setStyleSheet("color: #888;")
        p = self._spawn("curl", ["-sS", "-o", "/dev/null", "-w", "%{http_code}",
                                 "--max-time", "15", "-K", "-"],
                        self._on_validate_done)
        # feed url+header via stdin so the key never appears in argv / ps
        p.write(f'url = "{AAI_CHECK_URL}"\n'
                f'header = "Authorization: {key}"\n'.encode())
        p.closeWriteChannel()

    def _on_validate_done(self, proc):
        code = bytes(proc.readAllStandardOutput()).decode().strip()
        self.validate_btn.setEnabled(True)
        if code == "200":
            self.api_result.setText("✅ Key is valid (HTTP 200).")
            self.api_result.setStyleSheet("color: #2e7d32;")
        elif code in ("401", "403"):
            self.api_result.setText(f"❌ Key rejected (HTTP {code}).")
            self.api_result.setStyleSheet("color: #b00;")
        else:
            self.api_result.setText(f"Could not validate (response: {code or 'no reply'}).")
            self.api_result.setStyleSheet("color: #b00;")

    # ---- mic test (record, then play back) ------------------------------- #
    def toggle_record(self):
        if self.rec_proc is None:
            self.start_record()
        else:
            self.stop_record()

    def start_record(self):
        src = self.device.currentData()
        args = [RECMETER, "--out", REC_FILE]
        if src:
            args += ["--source", src]
        self.rec_proc = QProcess(self)
        self.rec_proc.setProgram(VENVPY)
        self.rec_proc.setArguments(args)
        self.rec_proc.readyReadStandardError.connect(self._on_rec_level)
        self.rec_proc.finished.connect(self._on_rec_finished)
        self.rec_proc.start()
        self.rec_btn.setText("■  Stop")
        self.play_btn.setEnabled(False)
        self.mic_result.setText("🎙 Recording — speak now, then Stop.")
        self.mic_result.setStyleSheet("color: #888;")

    def stop_record(self):
        if self.rec_proc is not None:
            pid = int(self.rec_proc.processId())
            if pid > 0:
                os.kill(pid, signal.SIGINT)     # clean WAV finalize
            else:
                self.rec_proc.terminate()

    def _on_rec_level(self):
        text = bytes(self.rec_proc.readAllStandardError()).decode("utf-8", "replace")
        for line in text.splitlines():
            if line.startswith("LEVEL "):
                try:
                    db = float(line.split()[1])
                except (ValueError, IndexError):
                    continue
                self.mic_bar.setValue(max(0, min(100, int((db + 60) / 60 * 100))))

    def _on_rec_finished(self):
        self.rec_proc = None
        self.rec_btn.setText("●  Record")
        self.mic_bar.setValue(0)
        dur = wav_duration(REC_FILE)
        if dur <= 0:
            self.mic_result.setText("⚠ No audio captured — the source delivered "
                                    "nothing (e.g. a Bluetooth mic not streaming).")
            self.mic_result.setStyleSheet("color: #b00;")
            return
        peak = wav_peak_db(REC_FILE)
        self.play_btn.setEnabled(True)
        if peak > -50:
            self.mic_result.setText(f"✅ Recorded {dur:.1f}s, peak {peak:.0f} dBFS — "
                                    f"press Play back to hear it.")
            self.mic_result.setStyleSheet("color: #2e7d32;")
        else:
            self.mic_result.setText(f"⚠ Recorded {dur:.1f}s but very quiet "
                                    f"(peak {peak:.0f} dBFS). Check the mic/device.")
            self.mic_result.setStyleSheet("color: #b00;")

    def play_recording(self):
        if not os.path.exists(REC_FILE):
            return
        self.play_btn.setEnabled(False)
        self.mic_result.setText("▶ Playing back…")
        self._spawn("paplay", [REC_FILE], lambda p: self._on_play_done())

    def _on_play_done(self):
        self.play_btn.setEnabled(True)
        self.mic_result.setText("Playback done.")

    # ---- misc ------------------------------------------------------------ #
    def _spawn(self, prog, args, on_finished):
        """Start a one-shot aux QProcess; call on_finished(proc) when done."""
        p = QProcess(self)
        p.setProgram(prog)
        p.setArguments(args)
        self._procs.append(p)

        def done(*_):
            try:
                on_finished(p)
            finally:
                if p in self._procs:
                    self._procs.remove(p)
        p.finished.connect(done)
        p.start()
        return p

    def _refresh_usage_label(self):
        live = (time.time() - self._conn_start) if self._conn_start else 0.0
        total = self._usage_total + live
        live_note = "  ● live" if self._conn_start else ""
        self.usage_label.setText(f"AssemblyAI connection time: {fmt_hms(total)}{live_note}")

    def set_status(self, msg):
        self.status.setText(msg)

    # ---- system tray ----------------------------------------------------- #
    def _build_tray(self):
        self.tray = QtWidgets.QSystemTrayIcon(QtGui.QIcon(ICON_PATH), self)
        self.tray.setToolTip("Voice → Terminal")
        self.tray_menu = QtWidgets.QMenu()
        # Rebuild the menu each time it opens so the terminal list, pin state and
        # listen toggle always reflect reality.
        self.tray_menu.aboutToShow.connect(self._populate_tray_menu)
        self._populate_tray_menu()
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _populate_tray_menu(self):
        m = self.tray_menu
        m.clear()

        m.addAction("Show / Hide").triggered.connect(self._toggle_window)
        m.addSeparator()

        listening = self.proc is not None
        toggle = m.addAction("■  Stop Listening" if listening else "🎤  Start Listening")
        toggle.triggered.connect(self.toggle_listen)
        m.addSeparator()

        header = m.addAction("Pin a terminal:")
        header.setEnabled(False)

        # Fresh enumeration so newly-opened tabs show up immediately.
        self.terminals = enumerate_terminals()
        self._tray_pin_group = QtWidgets.QActionGroup(m)
        self._tray_pin_group.setExclusive(True)
        pinned_pts = self.pinned["pts"] if self.pinned else None

        if self.terminals:
            for t in self.terminals:
                a = m.addAction(t.get("title") or t.get("label"))
                a.setCheckable(True)
                self._tray_pin_group.addAction(a)
                if t["pts"] == pinned_pts:
                    a.setChecked(True)
                a.triggered.connect(lambda _checked, term=t: self._pin_to(term))
        else:
            none = m.addAction("(no Terminator terminals found)")
            none.setEnabled(False)

        focused = m.addAction("Focused window (no pin)")
        focused.setCheckable(True)
        self._tray_pin_group.addAction(focused)
        focused.setChecked(pinned_pts is None)
        focused.triggered.connect(lambda _checked: self._pin_to(None))

        m.addSeparator()
        m.addAction("Quit").triggered.connect(self._quit)

    def _pin_to(self, t):
        """Pin (or unpin if t is None) from the tray, syncing the Dictate tab."""
        self.pinned = t
        self.refresh_terminals()      # re-syncs the Target combo + pin label
        if t:
            self.set_status(f"Pinned {t['tty']}.")
            dbg(f"tray pinned {t['pts']} ({t['label']})")
        else:
            self.set_status("Unpinned — will type into the focused window.")

    def _on_tray_activated(self, reason):
        if reason == QtWidgets.QSystemTrayIcon.Trigger:      # left click
            self._toggle_window()

    def _toggle_window(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def changeEvent(self, e):
        # Minimize → hide into the tray instead of the taskbar.
        if e.type() == QEvent.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self.hide)
        super().changeEvent(e)

    def closeEvent(self, e):
        # Closing the window keeps it running in the tray; Quit is in the menu.
        if self.tray.isVisible():
            e.ignore()
            self.hide()
            if not self._tray_notified:
                self.tray.showMessage(
                    "Voice → Terminal",
                    "Still running in the tray — click to reopen, right-click to Quit.",
                    QtWidgets.QSystemTrayIcon.Information, 4000)
                self._tray_notified = True
        else:
            self._quit()

    def _quit(self):
        if self.proc is not None:
            self.proc.kill()
        if self.rec_proc is not None:
            self.rec_proc.kill()
        self.tray.hide()
        QtWidgets.QApplication.quit()


def main():
    try:
        open(DEBUG_LOG, "w").close()      # truncate previous session's log
    except OSError:
        pass
    dbg(f"=== voice_gui start (tiocsti_sysctl={_tiocsti_sysctl()}, "
        f"injector={injector_available()}) ===")
    app = QtWidgets.QApplication([])
    app.setApplicationName("Voice → Terminal")
    app.setWindowIcon(QtGui.QIcon(ICON_PATH))
    app.setQuitOnLastWindowClosed(False)   # keep running in the tray
    w = VoiceGui()
    w.show()
    return app.exec_()


def _tiocsti_sysctl():
    try:
        with open("/proc/sys/dev/tty/legacy_tiocsti") as f:
            return f.read().strip()
    except OSError:
        return "?"


if __name__ == "__main__":
    raise SystemExit(main())
