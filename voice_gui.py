#!/usr/bin/env python3
"""Voice → Terminal control GUI.

A PyQt5 control panel for hands-free dictation with two interchangeable
transcription engines:

  * Vosk        — fully offline (local models, no network, no key).
  * AssemblyAI  — Universal-Streaming API (needs an API key).

The final transcript is delivered in one of two explicit delivery modes:

  * Terminal mode — text goes to a *pinned* Terminator terminal (tab/split),
    identified by its pseudo-terminal. It is injected straight into that pts
    via a setuid TIOCSTI helper, so it lands in that same shell no matter
    which tab/window is focused — keep working elsewhere while dictation
    flows into the pinned tab.
  * Active-window mode — text is typed with xdotool into whatever window is
    currently focused, right where the cursor sits.

Tabs:
  Dictate — engine, target/pin, listen/pause, live transcript.
  API     — AssemblyAI key (save) + validate-key test (no audio).
  Voice   — input-device selection + a mic level test.
  Noise   — noise-floor gate with a live input-level meter.
"""
import array
import json
import math
import os
import queue
import re
import signal
import subprocess
import threading
import time
import wave

from PyQt5 import QtGui, QtWidgets
from PyQt5.QtCore import Qt, QEvent, QObject, QProcess, QProcessEnvironment, QTimer, pyqtSignal

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

# Delivery modes
MODE_TERMINAL = "terminal"   # pinned Terminator tab, injected via TIOCSTI
MODE_FOCUSED = "focused"     # active window, typed with xdotool at the cursor

# Filler-word filter: transient sounds (a chair creak, a keyboard clack, a
# dropped mug) are often transcribed as a lone "the" (or similar short word).
# When a finalized segment consists solely of these words, hold it for a short
# grace period and type it only if real speech follows; otherwise drop it.
DEFAULT_FILLER_WORDS = ["the", "a", "an", "um", "uh", "hmm"]


def _is_filler_segment(seg, words):
    """True when `seg` is nothing but filler words (case/punctuation-insensitive)."""
    wset = set(words) if words else set(DEFAULT_FILLER_WORDS)
    tokens = re.findall(r"[a-z']+", seg.lower())
    if not tokens:
        return False
    return all(t in wset for t in tokens)


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


def inject_tiocsti(pts_path, text, press_enter, timeout=8.0):
    data = text.encode("utf-8") + (b"\n" if press_enter else b"")
    try:
        r = subprocess.run([INJECTOR, pts_path], input=data, stderr=subprocess.PIPE,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        dbg(f"inject_tiocsti pts={pts_path} TIMEOUT after {timeout}s")
        raise RuntimeError(
            f"injection timed out — {pts_path} is not consuming input "
            f"(terminal stopped/full?)") from None
    dbg(f"inject_tiocsti pts={pts_path} bytes={len(data)} rc={r.returncode} "
        f"err={r.stderr.decode('utf-8', 'replace').strip()!r}")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()
                           or f"injector exited {r.returncode}")


def type_xdotool(text, press_enter, window_id=None, timeout=20.0):
    # Note: `xdotool type --window` delivers XSendEvent keys, which many apps
    # (terminals especially) silently ignore. Focus the captured window first,
    # then let XTEST type into the focused window — indistinguishable from real
    # keyboard input.
    if window_id:
        try:
            subprocess.run(["xdotool", "windowfocus", "--sync", str(window_id)],
                           check=True, timeout=3)
        except Exception:
            window_id = None   # window gone/unfocusable — type wherever focus is
    try:
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--", text],
                       check=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("xdotool type timed out") from None
    if press_enter:
        subprocess.run(["xdotool", "key", "Return"], check=True, timeout=timeout)


def _active_window_id():
    """Return the X11 window id that currently has focus ('' / None on failure)."""
    try:
        out = subprocess.check_output(["xdotool", "getactivewindow"], text=True,
                                      timeout=2)
        return out.strip() or None
    except Exception:
        return None


class Signals(QObject):
    """Cross-thread signal bridge (emitted from worker threads, handled in GUI)."""
    terminals_ready = pyqtSignal(object)
    devices_ready = pyqtSignal(object)
    delivery_done = pyqtSignal(object)
    delivery_failed = pyqtSignal(str)


class DeliveryThread(threading.Thread):
    """Types/injects finalized segments off the GUI thread, in FIFO order.

    `xdotool type` takes ~12 ms per character and the TIOCSTI injector can block
    indefinitely when a pinned terminal isn't consuming input. Running delivery
    here keeps the GUI responsive so Pause/Stop/Hide/Quit always react at once.
    """

    def __init__(self, signals):
        super().__init__(daemon=True)
        self.signals = signals
        self.q = queue.Queue()

    def submit(self, item):
        self.q.put(item)

    def clear(self):
        """Drop queued-but-not-started items (e.g. when a new session begins)."""
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def stop(self):
        self.q.put(None)          # sentinel: finish the queue, then exit

    def run(self):
        while True:
            item = self.q.get()
            if item is None:
                break
            try:
                self._deliver(item)
            except Exception as e:
                dbg(f"delivery thread FAILED: {e}")
                try:
                    self.signals.delivery_failed.emit(str(e))
                except RuntimeError:
                    pass          # GUI shutting down
            else:
                try:
                    self.signals.delivery_done.emit(item)
                except RuntimeError:
                    pass

    @staticmethod
    def _deliver(item):
        if item["kind"] == "enter":
            if item["mode"] == MODE_TERMINAL:
                inject_tiocsti(item["pts"], "", True)    # newline only
            else:
                if item.get("window"):
                    try:
                        subprocess.run(["xdotool", "windowfocus", "--sync",
                                        str(item["window"])], check=True, timeout=3)
                    except Exception:
                        pass
                subprocess.run(["xdotool", "key", "Return"], check=False, timeout=10)
        elif item["mode"] == MODE_TERMINAL:
            inject_tiocsti(item["pts"], item["piece"], False)
        else:
            type_xdotool(item["piece"], False, item.get("window"))


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
class LevelMeter(QtWidgets.QWidget):
    """Vertical live audio-level meter with the noise-floor threshold drawn on it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = -120.0      # current input level, dBFS
        self._threshold = -45.0   # noise-floor threshold, dBFS
        self.setMinimumSize(40, 180)
        self.setMaximumWidth(56)

    def set_level(self, dbfs):
        self._level = max(-120.0, min(0.0, dbfs))
        self.update()

    def set_threshold(self, dbfs):
        self._threshold = dbfs
        self.update()

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QtGui.QColor("#1e1e1e"))

        lo, hi = -60.0, 0.0

        def y_for(db):
            frac = (db - lo) / (hi - lo)
            return h - int(frac * (h - 2)) - 1

        lvl_y = y_for(self._level)
        thr_y = y_for(self._threshold)

        passed = self._level >= self._threshold
        color = QtGui.QColor("#2e7d32") if passed else QtGui.QColor("#c62828")
        p.fillRect(2, lvl_y, w - 4, h - lvl_y - 2, color)

        pen = QtGui.QPen(QtGui.QColor("#ffb300"), 2)
        p.setPen(pen)
        p.drawLine(0, thr_y, w - 1, thr_y)
        p.end()


class VoiceGui(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.terminals = []
        self._pending_deliveries = 0   # segments/Enter still queued for delivery
        self._inject_failed = False
        self._inject_error = ""
        self._delivered_any = False
        self._last_window = None       # X11 window the last focused-mode segment targeted
        self._term_refresh_running = False
        self._dev_refresh_running = False
        self._default_source = ""
        self.sig = Signals()
        self.sig.terminals_ready.connect(self._on_terminals_ready)
        self.sig.devices_ready.connect(self._on_devices_ready)
        self.sig.delivery_done.connect(self._on_delivery_done)
        self.sig.delivery_failed.connect(self._on_delivery_failed)
        self._delivery = DeliveryThread(self.sig)
        self._delivery.start()
        self.proc = None          # listening QProcess
        self.rec_proc = None      # mic-test recorder QProcess
        self.meter_proc = None    # noise-floor level meter QProcess
        self._procs = []          # keep refs to one-shot aux processes alive
        self.out_buf = ""
        self.pinned = None
        self._paused = False      # dictation paused (engine still loaded)
        self._stdout_buf = ""
        self._injected_any = False
        self._seg_count = 0
        self._noise_tab = None
        self._announced = False
        self.sources = []
        self._usage_total = load_usage_seconds()   # billable AssemblyAI seconds
        self._conn_start = None                     # wall-clock of current connection
        self._usage_timer = QTimer(self)
        self._usage_timer.setInterval(1000)
        self._usage_timer.timeout.connect(self._refresh_usage_label)
        self._held_filler = None          # segment currently held by the filler filter
        self._filler_timer = QTimer(self) # grace period for a held filler segment
        self._filler_timer.setSingleShot(True)
        self._filler_timer.timeout.connect(self._on_filler_timeout)
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
        self.setMinimumWidth(520)
        outer = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        outer.addWidget(self.tabs)
        self.tabs.addTab(self._tab_dictate(), "Dictate")
        self.tabs.addTab(self._tab_api(), "API")
        self.tabs.addTab(self._tab_voice(), "Voice")
        self._noise_tab = self._tab_noise()
        self.tabs.addTab(self._noise_tab, "Noise")
        self.tabs.currentChanged.connect(self._on_tab_changed)

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
        for cls in (QtWidgets.QPushButton, QtWidgets.QCheckBox, QtWidgets.QComboBox,
                    QtWidgets.QRadioButton, QtWidgets.QSlider, QtWidgets.QSpinBox,
                    QtWidgets.QDoubleSpinBox):
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

        frow = QtWidgets.QHBoxLayout()
        self.filter_fillers = QtWidgets.QCheckBox(
            "Filter isolated filler words (transient sounds → “the”)")
        self.filter_fillers.setToolTip(
            "Transient noises are often transcribed as a lone “the” (or “a”, “um”, …).\n"
            "When enabled, a segment made only of those words is held briefly and\n"
            "typed only if real speech follows within the grace period; otherwise\n"
            "it is discarded. Chains of such segments are never typed.")
        self.filter_fillers.stateChanged.connect(self._save_settings)
        frow.addWidget(self.filter_fillers)
        frow.addSpacing(8)
        frow.addWidget(QtWidgets.QLabel("grace:"))
        self.filler_grace = QtWidgets.QDoubleSpinBox()
        self.filler_grace.setRange(0.5, 5.0)
        self.filler_grace.setSingleStep(0.5)
        self.filler_grace.setDecimals(1)
        self.filler_grace.setSuffix(" s")
        self.filler_grace.setToolTip(
            "How long to wait for real speech before discarding a held filler word.")
        self.filler_grace.valueChanged.connect(self._save_settings)
        frow.addWidget(self.filler_grace)
        frow.addStretch(1)
        v.addLayout(frow)

        mbox = QtWidgets.QGroupBox("Delivery mode")
        mv = QtWidgets.QVBoxLayout(mbox)
        self.mode_terminal = QtWidgets.QRadioButton(
            "Terminal — type into the pinned terminal tab")
        self.mode_focused = QtWidgets.QRadioButton(
            "Active window — type at the cursor in the focused window")
        self.mode_terminal.toggled.connect(self._on_mode_changed)
        self.mode_focused.toggled.connect(self._on_mode_changed)
        mv.addWidget(self.mode_terminal)
        mv.addWidget(self.mode_focused)
        v.addWidget(mbox)

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
        self.pause_btn = QtWidgets.QPushButton("⏸  Pause")
        self.pause_btn.setMinimumHeight(48)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setToolTip("Pause/resume processing without unloading the model")
        self.pause_btn.clicked.connect(self.toggle_pause)
        lrow = QtWidgets.QHBoxLayout()
        lrow.addWidget(self.listen_btn, 1)
        lrow.addWidget(self.pause_btn, 1)
        v.addLayout(lrow)

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

    def _tab_noise(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)

        self.nf_gate = QtWidgets.QCheckBox(
            "Enable noise gate (discard audio quieter than the threshold)")
        self.nf_gate.stateChanged.connect(self._save_settings)
        v.addWidget(self.nf_gate)

        h = QtWidgets.QHBoxLayout()

        sv = QtWidgets.QVBoxLayout()
        self.nf_slider = QtWidgets.QSlider(Qt.Vertical)
        self.nf_slider.setRange(-60, -10)
        self.nf_slider.setTickInterval(5)
        self.nf_slider.setTickPosition(QtWidgets.QSlider.TicksRight)
        self.nf_slider.valueChanged.connect(self._on_nf_slider)
        self.nf_slider.setToolTip("Noise-floor threshold (dBFS). Audio quieter "
                                  "than this is treated as noise.")
        sv.addWidget(self.nf_slider)
        self.nf_value = QtWidgets.QLabel("-45 dBFS")
        self.nf_value.setAlignment(Qt.AlignCenter)
        sv.addWidget(self.nf_value)
        h.addLayout(sv)

        self.nf_meter = LevelMeter()
        h.addWidget(self.nf_meter)

        info = QtWidgets.QVBoxLayout()
        self.nf_level = QtWidgets.QLabel("Input level: — dBFS")
        self.nf_level.setStyleSheet("font-size: 13px;")
        self.nf_state = QtWidgets.QLabel("Status: —")
        self.nf_state.setStyleSheet("font-size: 13px; font-weight: bold;")
        self.nf_state.setWordWrap(True)
        hint = QtWidgets.QLabel(
            "Speak and watch the meter. The orange line is the noise floor. "
            "Adjust the slider so background noise shows GATED (red, below the "
            "line) and your voice shows RECEIVED (green, above the line).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555;")
        info.addWidget(self.nf_level)
        info.addWidget(self.nf_state)
        info.addStretch(1)
        info.addWidget(hint)
        h.addLayout(info, 1)

        v.addLayout(h)
        v.addStretch(1)
        return w

    def _on_nf_slider(self, value):
        self.nf_value.setText(f"{value} dBFS")
        self.nf_meter.set_threshold(float(value))
        self._update_nf_labels()
        self._save_settings()

    def _on_device_changed(self, *a):
        self._save_settings()
        self._update_device_info()
        self._stop_meter()

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
        if self.cfg.get("delivery_mode", MODE_TERMINAL) == MODE_FOCUSED:
            self.mode_focused.setChecked(True)
        else:
            self.mode_terminal.setChecked(True)
        try:
            self.key_edit.setText(open(KEY_FILE).read().strip())
        except OSError:
            pass
        self.nf_gate.setChecked(bool(self.cfg.get("noise_gate", True)))
        nf = int(round(float(self.cfg.get("noise_floor", -45.0))))
        self.nf_slider.setValue(max(-60, min(-10, nf)))
        self.filter_fillers.setChecked(bool(self.cfg.get("filter_fillers", True)))
        try:
            self.filler_grace.setValue(float(self.cfg.get("filler_grace", 1.5)))
        except (TypeError, ValueError):
            pass
        self._engine_changed()

    def _save_settings(self, *a):
        if getattr(self, "_loading", False):
            return
        self.cfg.update({
            "engine": self.engine.currentData(),
            "lang": self.lang.currentText(),
            "auto_enter": self.auto_enter.isChecked(),
            "delivery_mode": self.delivery_mode(),
            "noise_gate": self.nf_gate.isChecked(),
            "noise_floor": self.nf_slider.value(),
            "source": self.device.currentData(),
            "filter_fillers": self.filter_fillers.isChecked(),
            "filler_grace": self.filler_grace.value(),
        })
        save_config(self.cfg)

    def _engine_changed(self, *a):
        is_vosk = self.engine.currentData() == "vosk"
        self.lang.setEnabled(is_vosk)
        self.lang_label.setEnabled(is_vosk)
        self._save_settings()

    # ---- delivery mode ---------------------------------------------------- #
    def delivery_mode(self):
        return MODE_FOCUSED if self.mode_focused.isChecked() else MODE_TERMINAL

    def _set_delivery_mode(self, mode):
        """Set the mode from outside the radio buttons (tray); syncs UI + config."""
        self.mode_terminal.setChecked(mode == MODE_TERMINAL)
        self.mode_focused.setChecked(mode == MODE_FOCUSED)

    def _on_mode_changed(self, *a):
        self._save_settings()
        self._update_pin_label()
        if self.proc is None:
            self._update_mode_status()

    def _update_mode_status(self):
        if self.delivery_mode() == MODE_TERMINAL:
            if self.pinned:
                self.set_status(f"Mode: Terminal — text goes to pinned {self.pinned['tty']}.")
            else:
                self.set_status("Mode: Terminal — pin a terminal in the Target box first.")
        else:
            self.set_status("Mode: Active window — text will be typed at the cursor.")

    # ---- terminals / devices -------------------------------------------- #
    def refresh_terminals(self):
        """Re-enumerate Terminator terminals in the background.

        Enumeration shells out to `ps` and to `remotinator` (DBus) per tab, which
        can take seconds — it must never run on the GUI thread.
        """
        if self._term_refresh_running:
            return
        self._term_refresh_running = True

        def work():
            try:
                terms = enumerate_terminals()
            except Exception as e:
                dbg(f"terminal refresh failed: {e}")
                terms = []
            try:
                self.sig.terminals_ready.emit(terms)
            finally:
                self._term_refresh_running = False

        threading.Thread(target=work, daemon=True).start()

    def _on_terminals_ready(self, terminals):
        self.terminals = terminals
        self.targets.clear()
        if not terminals:
            self.targets.addItem("(no Terminator terminals found)")
            self.targets.setEnabled(False)
            self.pin_btn.setEnabled(False)
        else:
            self.targets.setEnabled(True)
            self.pin_btn.setEnabled(True)
            for t in terminals:
                self.targets.addItem(t["label"], t)
            if self.pinned:
                for i, t in enumerate(terminals):
                    if t["pts"] == self.pinned["pts"]:
                        self.targets.setCurrentIndex(i)
                        break
        self._update_pin_label()

    def refresh_devices(self):
        """Re-read PulseAudio sources in the background (pactl can block)."""
        if self._dev_refresh_running:
            return
        self._dev_refresh_running = True

        def work():
            try:
                sources = list_pulse_sources()
                ds = default_source()
            except Exception as e:
                dbg(f"device refresh failed: {e}")
                sources, ds = [], ""
            try:
                self.sig.devices_ready.emit((sources, ds))
            finally:
                self._dev_refresh_running = False

        threading.Thread(target=work, daemon=True).start()

    def _on_devices_ready(self, payload):
        sources, ds = payload
        want = self.cfg.get("source")
        self.sources = sources
        self._default_source = ds
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
        self._stop_meter()   # source may have changed; restart on next tab visit

    def pin_selected(self):
        t = self.targets.currentData()
        if t:
            self.pinned = t
            self.mode_terminal.setChecked(True)   # pinning switches to Terminal mode
            self._update_pin_label()
            self.set_status(f"Pinned {t['tty']} — mode: Terminal.")
            dbg(f"pinned {t['pts']} ({t['label']})")

    def unpin(self):
        self.pinned = None
        self.mode_focused.setChecked(True)        # nothing left to inject into
        self._update_pin_label()
        self.set_status("Unpinned — mode: Active window (types at the cursor).")

    def _update_pin_label(self):
        if self.pinned:
            name = self.pinned.get("title") or self.pinned.get("cmd") or ""
            self.pin_label.setText(f"📌 Pinned → <b>{name}</b> ({self.pinned['tty']})")
            self.pin_label.setStyleSheet("color: #2e7d32;")
        else:
            self.pin_label.setText("No terminal pinned — switch to Terminal mode and "
                                   "pin one above (or use Active window mode).")
            self.pin_label.setStyleSheet("color: #888;")

    # ---- listening ------------------------------------------------------- #
    def _engine_cmd(self):
        src = self.device.currentData()    # PulseAudio source name, or None=default
        nf = (["--noise-floor", str(self.nf_slider.value())]
              if self.nf_gate.isChecked() else [])
        if self.engine.currentData() == "assemblyai":
            args = [AAI, "--print"] + nf
            if src:
                args += ["--source", src]
            return VENVPY, args, None
        # Vosk: go through parec via --source (Bluetooth-safe). For the default
        # device, resolve the current default source name; if that fails, fall
        # back to talk.py's built-in sounddevice default.
        args = ["--print", self.lang.currentText()] + nf
        ds = src or getattr(self, "_default_source", "")
        if ds:
            args += ["--source", ds]
        return TALK, args, {"TALK_NO_BT": "1"}

    def toggle_listen(self):
        if self.proc is None:
            self.start_listen()
        else:
            self.stop_listen()

    def start_listen(self):
        if self.delivery_mode() == MODE_TERMINAL and not self.pinned:
            self.set_status("⚠ Terminal mode needs a pinned terminal — pin one in "
                            "the Target box, or switch to Active window mode.")
            return
        self._stop_meter()            # the engine needs the mic to itself
        self._delivery.clear()        # drop any stale queued segments
        self._paused = False
        self.pause_btn.setText("⏸  Pause")
        self.pause_btn.setEnabled(True)
        self._announced = False
        self._stdout_buf = ""        # line-buffer for finalized segments
        self._held_filler = None     # nothing held by the filler filter yet
        self._filler_timer.stop()
        self._injected_any = False
        self._inject_failed = False
        self._inject_error = ""
        self._delivered_any = False
        self._last_window = None
        self._pending_deliveries = 0
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
        if self.delivery_mode() == MODE_TERMINAL:
            self.set_status(f"Starting… text will flow into pinned {self.pinned['tty']}.")
        else:
            self.set_status("Starting… text will be typed at the cursor in the "
                            "focused window.")
        dbg(f"start_listen engine={self.engine.currentData()} "
            f"prog={prog} args={args} mode={self.delivery_mode()} "
            f"pinned={self.pinned['pts'] if self.pinned else None} "
            f"auto_enter={self.auto_enter.isChecked()}")

    def stop_listen(self):
        if self.proc is None:
            return
        proc = self.proc
        self.set_status("Finalizing…")
        # Give the engine a moment to flush the last segment, then SIGKILL so a
        # wedged engine (or a hung parec child) can't leave us stuck listening.
        QTimer.singleShot(4000, lambda: self._force_kill_proc(proc))
        proc.terminate()               # SIGTERM → engine flushes & exits

    def _force_kill_proc(self, proc):
        if self.proc is proc and proc.state() != QProcess.NotRunning:
            dbg("force-killing engine after SIGTERM timeout")
            proc.kill()

    def toggle_pause(self):
        """Pause/resume the engine with SIGUSR1 — the model stays loaded."""
        if self.proc is None:
            return
        if self.proc.state() != QProcess.Running:
            return
        pid = int(self.proc.processId())
        if pid > 0:
            try:
                os.kill(pid, signal.SIGUSR1)
            except ProcessLookupError:
                return
        self._paused = not self._paused
        self.pause_btn.setText("▶  Resume" if self._paused else "⏸  Pause")
        self.set_status("⏸ Paused — audio discarded, model stays loaded."
                        if self._paused else "▶ Resumed — listening again.")
        dbg(f"toggle_pause -> {'paused' if self._paused else 'resumed'} (pid={pid})")

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
        """Handle one finalized segment, applying the filler-word filter.

        A segment made only of filler words ("the", "a", "um", …) is held for
        the grace period instead of being typed at once: if real speech follows,
        it is flushed first (FIFO order is preserved); if silence follows, it is
        discarded as a transient-sound artifact. Everything else is queued for
        background delivery immediately.
        """
        if self._inject_failed:
            return
        if (self.filter_fillers.isChecked()
                and _is_filler_segment(seg, self.cfg.get("filler_words"))):
            self._hold_filler(seg)
            return
        self._flush_held_filler()
        self._queue_text(seg)

    def _hold_filler(self, seg):
        """Hold a filler-only segment; drop a previously held one (chained noise)."""
        if self._held_filler is not None:
            dbg(f"filler filter: dropping chained {self._held_filler!r} before {seg!r}")
        self._held_filler = seg
        self._filler_timer.start(int(self.filler_grace.value() * 1000))
        dbg(f"filler filter: holding {seg!r} for {self.filler_grace.value():.1f}s")

    def _flush_held_filler(self):
        """Real speech followed a held filler — type the filler first, then stop holding."""
        if self._held_filler is None:
            return
        self._filler_timer.stop()
        seg, self._held_filler = self._held_filler, None
        dbg(f"filler filter: flushing {seg!r} (real speech followed)")
        self._queue_text(seg)

    def _drop_held_filler(self):
        """Silence followed a held filler (or the session ended) — discard it."""
        self._filler_timer.stop()
        if self._held_filler is not None:
            dbg(f"filler filter: dropping isolated {self._held_filler!r}")
            self._held_filler = None

    def _on_filler_timeout(self):
        self._drop_held_filler()

    def _queue_text(self, seg):
        """Queue one finalized segment for background delivery.

        xdotool / TIOCSTI delivery can take seconds (or block); it runs on the
        delivery thread so the GUI never freezes and Stop/Pause/Hide/Quit stay
        responsive.
        """
        piece = seg + " "
        mode = self.delivery_mode()
        if mode == MODE_TERMINAL and not self.pinned:
            self.set_status("Inject failed (terminal mode but nothing is pinned) — stopping.")
            self.stop_listen()
            return
        window = _active_window_id() if mode == MODE_FOCUSED else None
        if mode == MODE_FOCUSED and window:
            self._last_window = window
        dbg(f"emit_segment {seg!r} mode={mode} window={window} "
            f"pinned={self.pinned['pts'] if self.pinned else None}")
        self._delivery.submit({
            "kind": "text", "piece": piece, "mode": mode,
            "pts": self.pinned["pts"] if mode == MODE_TERMINAL else None,
            "window": window,
        })
        self._pending_deliveries += 1
        self._injected_any = True
        self._seg_count += 1
        self.transcript.setPlainText((self.transcript.toPlainText() + piece))
        if mode == MODE_TERMINAL and self.pinned:
            tgt = self.pinned["tty"]
        else:
            tgt = "active window (cursor)"
        self.set_status(f"🎙 Streaming → {tgt} ({self._seg_count} segment"
                        f"{'s' if self._seg_count != 1 else ''})")

    def _on_delivery_done(self, item):
        self._pending_deliveries = max(0, self._pending_deliveries - 1)
        if item.get("kind") == "text":
            self._delivered_any = True
            dbg(f"delivery done: {item['piece']!r}")
        # If the engine has exited and the queue has drained, show the real final
        # state instead of leaving "Delivering…" on screen.
        if (self._pending_deliveries == 0 and self.proc is None
                and not self._inject_failed and self._injected_any):
            self._set_finished_status()

    def _on_delivery_failed(self, msg):
        self._pending_deliveries = max(0, self._pending_deliveries - 1)
        self._inject_failed = True
        self._inject_error = msg
        dbg(f"delivery FAILED: {msg}")
        self.set_status(f"Inject failed ({msg}) — stopping.")
        self.stop_listen()

    def _set_finished_status(self):
        mode = self.delivery_mode()
        if mode == MODE_TERMINAL and self.pinned:
            tgt = self.pinned["tty"]
        else:
            tgt = "active window (cursor)"
        self.set_status(f"Done — streamed {self._seg_count} segment"
                        f"{'s' if self._seg_count != 1 else ''} → {tgt}.")

    def _on_stderr(self):
        text = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            dbg(f"engine stderr: {s!r}")
            if s.startswith("…"):
                # live partial preview (not yet finalized / injected)
                if self.delivery_mode() == MODE_TERMINAL and self.pinned:
                    tgt = self.pinned["tty"]
                else:
                    tgt = "cursor"
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
            elif s.startswith("[paused"):
                self._paused = True
                self.pause_btn.setText("▶  Resume")
                self.set_status("⏸ Paused — audio discarded, model stays loaded.")
            elif s.startswith("[resumed"):
                self._paused = False
                self.pause_btn.setText("⏸  Pause")
                self.set_status("▶ Resumed — listening again.")
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
        # The session ended — an isolated trailing filler ("the") gets dropped now
        # rather than waiting out its grace period after the engine is gone.
        self._drop_held_filler()
        self.proc = None
        self.listen_btn.setText("🎤  Start Listening")
        self._paused = False
        self.pause_btn.setText("⏸  Pause")
        self.pause_btn.setEnabled(False)
        # finalize usage if the engine didn't emit a [usage] line (crash/kill)
        self._usage_timer.stop()
        if self._conn_start is not None:
            self._usage_total += time.time() - self._conn_start
            save_usage_seconds(self._usage_total)
            self._conn_start = None
        self._refresh_usage_label()
        mode = self.delivery_mode()
        if self._injected_any and self.auto_enter.isChecked() and not self._inject_failed:
            # Queue Enter behind any segments still being typed, targeted at the
            # same window the segments went to.
            self._delivery.submit({
                "kind": "enter", "piece": "", "mode": mode,
                "pts": self.pinned["pts"] if mode == MODE_TERMINAL else None,
                "window": self._last_window if mode == MODE_FOCUSED else None,
            })
            self._pending_deliveries += 1
        if self._inject_failed:
            self.set_status(f"Inject failed ({self._inject_error}) — stopped.")
        elif not self._injected_any:
            self.set_status("No speech recognized.")
        elif self._pending_deliveries > 0:
            self.set_status("Delivering final text…")
        else:
            self._set_finished_status()
        self._sync_meter()   # restart the noise-floor meter if we're on that tab

    # ---- noise-floor meter ------------------------------------------------ #
    def _on_tab_changed(self, idx):
        self._sync_meter()

    def _sync_meter(self):
        on_noise = (self._noise_tab is not None
                    and self.tabs.currentWidget() is self._noise_tab)
        if on_noise and self.proc is None:
            self._start_meter()
        else:
            self._stop_meter()

    def _start_meter(self):
        if self.meter_proc is not None or self.proc is not None:
            return
        src = self.device.currentData()
        args = [RECMETER, "--meter"]
        if src:
            args += ["--source", src]
        self.meter_proc = QProcess(self)
        self.meter_proc.setProgram(VENVPY)
        self.meter_proc.setArguments(args)
        self.meter_proc.readyReadStandardError.connect(self._on_meter_stderr)
        self.meter_proc.finished.connect(self._on_meter_finished)
        self.meter_proc.start()
        dbg(f"start_meter args={args}")

    def _stop_meter(self):
        if self.meter_proc is not None:
            self.meter_proc.terminate()
            self.meter_proc = None

    def _on_meter_stderr(self):
        if self.meter_proc is None:
            return
        text = bytes(self.meter_proc.readAllStandardError()).decode("utf-8", "replace")
        for line in text.splitlines():
            if line.startswith("LEVEL "):
                try:
                    db = float(line.split()[1])
                except (ValueError, IndexError):
                    continue
                self.nf_meter.set_level(db)
                self._update_nf_labels(db)

    def _update_nf_labels(self, db=None):
        if db is None:
            db = self.nf_meter._level
        self.nf_level.setText(f"Input level: {db:.1f} dBFS")
        if db >= self.nf_slider.value():
            self.nf_state.setText("RECEIVED — above noise floor")
            self.nf_state.setStyleSheet("color: #2e7d32; font-size: 13px; font-weight: bold;")
        else:
            self.nf_state.setText("GATED — below noise floor")
            self.nf_state.setStyleSheet("color: #c62828; font-size: 13px; font-weight: bold;")

    def _on_meter_finished(self):
        self.meter_proc = None

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
        if listening:
            pause_a = m.addAction("▶  Resume" if self._paused else "⏸  Pause")
            pause_a.triggered.connect(self.toggle_pause)
        m.addSeparator()

        mhdr = m.addAction("Delivery mode:")
        mhdr.setEnabled(False)
        self._tray_mode_group = QtWidgets.QActionGroup(m)
        self._tray_mode_group.setExclusive(True)
        term_mode = m.addAction("Terminal (pinned)")
        term_mode.setCheckable(True)
        self._tray_mode_group.addAction(term_mode)
        focused_mode = m.addAction("Active window (cursor)")
        focused_mode.setCheckable(True)
        self._tray_mode_group.addAction(focused_mode)
        term_mode.setChecked(self.delivery_mode() == MODE_TERMINAL)
        focused_mode.setChecked(self.delivery_mode() == MODE_FOCUSED)
        term_mode.triggered.connect(lambda _checked: self._set_delivery_mode(MODE_TERMINAL))
        focused_mode.triggered.connect(lambda _checked: self._set_delivery_mode(MODE_FOCUSED))
        m.addSeparator()

        header = m.addAction("Pin a terminal (used by Terminal mode):")
        header.setEnabled(False)

        # Use the cached list; re-enumeration runs in the background (remotinator
        # can be slow) and the menu reflects it the next time it opens.
        m.addAction("⟳ Refresh terminal list").triggered.connect(self.refresh_terminals)

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

        unpin_a = m.addAction("Unpin terminal")
        unpin_a.setEnabled(self.pinned is not None)
        unpin_a.triggered.connect(lambda _checked=False: self.unpin())

        m.addSeparator()
        m.addAction("Quit").triggered.connect(self._quit)

    def _pin_to(self, t):
        """Pin (or unpin if t is None) from the tray, syncing the Dictate tab."""
        self.pinned = t
        if t:
            self.mode_terminal.setChecked(True)   # pinning implies Terminal mode
        self.refresh_terminals()      # re-syncs the Target combo + pin label
        if t:
            self.set_status(f"Pinned {t['tty']} — mode: Terminal.")
            dbg(f"tray pinned {t['pts']} ({t['label']})")
        else:
            self.mode_focused.setChecked(True)
            self.set_status("Unpinned — mode: Active window (cursor).")

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
        if self.meter_proc is not None:
            self.meter_proc.kill()
        try:
            self._delivery.stop()
        except Exception:
            pass
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
