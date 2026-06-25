#!/usr/bin/env python3
"""AssemblyAI Universal-Streaming push-to-talk → stdout (voice-GUI engine).

Mirrors the `talk --print` contract so the GUI can drive either engine the same
way: stream mic audio to AssemblyAI while running, show interim text on stderr
as "  … partial", and on SIGINT/SIGTERM print the final accumulated transcript
to stdout and exit. Delivery (typing / TIOCSTI injection) is the caller's job,
so this only prints.

Usage:
    aai_print.py [lang] [--device N] [--list-devices] [--print]

API key: $ASSEMBLYAI_API_KEY, else ~/.config/agent-dictate/api_key
"""
import argparse
import audioop
import collections
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import websocket  # websocket-client

SAMPLE_RATE = 16000
SPEECH_MODEL = os.environ.get("SPEECH_MODEL", "u3-rt-pro")
BLOCK = 1600   # 100 ms at 16 kHz


def err(*a):
    print(*a, file=sys.stderr, flush=True)


def load_api_key():
    k = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()
    if k:
        return k
    f = Path.home() / ".config" / "agent-dictate" / "api_key"
    if f.is_file():
        return f.read_text().strip()
    err("[no AssemblyAI API key: set ASSEMBLYAI_API_KEY or write "
        "~/.config/agent-dictate/api_key]")
    sys.exit(2)


class Stream:
    """One long-lived AssemblyAI streaming session, accumulating turns."""

    def __init__(self, key, url, stream=False):
        self.q = queue.Queue()
        self.connected = threading.Event()
        self.alive = True
        self.closed = False      # set when the websocket closes (drop / rate-limit)
        self.stream = stream     # emit each formatted turn to stdout immediately
        self.turns = {}          # turn_order -> formatted transcript
        self.partial = ""
        self.lock = threading.Lock()
        self.t_open = None       # wall-clock the connection opened / closed
        self.t_close = None
        self.ws = websocket.WebSocketApp(
            url,
            header={"Authorization": key},
            on_open=self._on_open,
            on_message=self._on_msg,
            on_error=lambda ws, e: err("[ws error]", e),
            on_close=self._on_close,
        )
        threading.Thread(target=self.ws.run_forever, daemon=True).start()
        threading.Thread(target=self._sender, daemon=True).start()

    def _on_open(self, ws):
        self.t_open = time.time()
        self.connected.set()

    def _on_close(self, ws, *a):
        if self.t_close is None:
            self.t_close = time.time()
        self.closed = True
        err(f"[ws closed] {a}")

    def open_seconds(self):
        """Wall-clock seconds the billable connection was open."""
        if self.t_open is None:
            return 0.0
        return (self.t_close or time.time()) - self.t_open

    def _on_msg(self, ws, message):
        try:
            msg = json.loads(message)
        except Exception:
            return
        if msg.get("type") != "Turn":
            return
        order = msg.get("turn_order", -1)
        text = (msg.get("transcript") or "").strip()
        if msg.get("end_of_turn") and msg.get("turn_is_formatted"):
            with self.lock:
                self.turns[order] = text
                self.partial = ""
            if text:
                err(f"  … {text}")
                if self.stream:
                    sys.stdout.write(text + "\n")
                    sys.stdout.flush()
        elif text and text != self.partial:
            self.partial = text
            err(f"  … {text}")

    def _sender(self):
        self.connected.wait()
        while self.alive:
            try:
                data = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            if data is None:
                break
            try:
                self.ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception:
                break

    def feed(self, pcm):
        self.q.put(pcm)

    def finish(self, wait=2.0):
        """Stop sending, ask AssemblyAI to finalize, collect the transcript."""
        self.alive = False
        self.q.put(None)
        try:
            self.ws.send(json.dumps({"type": "Terminate"}))
        except Exception:
            pass
        deadline = time.time() + wait
        while time.time() < deadline:          # let the last turn format
            with self.lock:
                if not self.partial:
                    break
            time.sleep(0.1)
        try:
            self.ws.close()
        except Exception:
            pass
        with self.lock:
            leftover = self.partial.strip()
            parts = [self.turns[k] for k in sorted(self.turns) if self.turns[k]]
            if leftover:                        # last utterance never formatted
                parts.append(leftover)
        if self.stream and leftover:            # emit trailing words of this phrase
            sys.stdout.write(leftover + "\n")
            sys.stdout.flush()
        return " ".join(parts).strip()


def main():
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("lang", nargs="?", default="en")   # accepted for CLI parity
    p.add_argument("--source", default=None, help="PulseAudio source name")
    p.add_argument("--print", dest="to_stdout", action="store_true")  # default anyway
    p.add_argument("--stream", action="store_true",
                   help="emit each finalized turn on its own line as produced")
    args = p.parse_args()

    key = load_api_key()
    url = "wss://streaming.assemblyai.com/v3/ws?" + urlencode(
        {"speech_model": SPEECH_MODEL, "sample_rate": SAMPLE_RATE,
         "format_turns": "true"})

    # Capture via parec (PulseAudio): handles Bluetooth, resamples to 16 kHz,
    # and never hangs the way sounddevice/PortAudio does on this system.
    cmd = ["parec", "--format=s16le", f"--rate={SAMPLE_RATE}", "--channels=1",
           "--latency-msec=50"]   # without this, piped parec stalls after one fragment
    if args.source:
        cmd.append(f"--device={args.source}")
    rec = {"p": subprocess.Popen(cmd, stdout=subprocess.PIPE)}

    stop = {"v": False}

    def on_stop(sig, frame):
        stop["v"] = True
        try:
            rec["p"].terminate()   # unblock a pending read so we exit promptly
        except Exception:
            pass
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    FRAME = int(SAMPLE_RATE * 0.1) * 2          # 100 ms, s16 mono = 3200 B
    SILENCE = b"\x00" * FRAME                    # sent for sub-threshold frames (noise gate)
    HANG_FRAMES = max(3, int(os.environ.get("SILENCE_MS", "600")) // 100)
    START_FRAMES = 3                             # 300 ms of speech (re)opens a session
    PREROLL = 3                                  # 300 ms kept so the first word isn't clipped
    # Close the (billed) connection after this much continuous silence so idle time
    # isn't billed; it reopens on your next words. Long enough that normal pauses
    # between phrases don't churn connections (which would hit the rate limit).
    IDLE_CLOSE_FRAMES = max(50, int(os.environ.get("IDLE_CLOSE_SEC", "20")) * 10)

    def read_frame():
        """One 100 ms frame. Respawns parec on EOF (e.g. a Bluetooth SCO drop)
        so a single hiccup never ends the listening session."""
        buf = b""
        while len(buf) < FRAME:
            chunk = rec["p"].stdout.read(FRAME - len(buf))
            if not chunk:
                if stop["v"]:
                    return None
                err("[capture: parec ended — respawning mic]")
                try:
                    rec["p"].terminate()
                except Exception:
                    pass
                rec["p"] = subprocess.Popen(cmd, stdout=subprocess.PIPE)
                buf = b""
                continue
            buf += chunk
        return buf

    # Decide the speech threshold: explicit override, else calibrate the noise
    # floor (your speech into a headset mic is far louder than the background).
    override = os.environ.get("AAI_VAD_RMS")
    if override:
        threshold = int(override)
        err(f"[vad threshold={threshold} (AAI_VAD_RMS)]")
    else:
        err("[calibrating noise — stay quiet a moment…]")
        floor_vals = []
        for _ in range(7):                       # ~0.7 s of ambient
            f = read_frame()
            if f is None:
                break
            floor_vals.append(audioop.rms(f, 2))
        floor = sorted(floor_vals)[len(floor_vals) // 2] if floor_vals else 0
        threshold = max(250, floor * 4 + 100)
        err(f"[noise floor={floor}, vad threshold={threshold}]")

    # Billing is by connection-open time (AssemblyAI bills idle time on an open
    # socket too). So: stay DORMANT (no connection, no cost) until you speak, open
    # ONE connection for the talking burst, and close it after IDLE_CLOSE seconds
    # of silence — reopening on your next words. Closing per-phrase would trip the
    # connection rate limit (close 1008); a long idle gap won't. While connected we
    # send real audio when speaking (+hangover) and SILENCE otherwise (noise gate).
    def open_session():
        s = Stream(key, url, stream=args.stream)
        if s.connected.wait(timeout=8):
            err("[connected]")
        else:
            err("[warning: not connected — check API key / network]")
        return s

    def bill(s):                                 # report this session's billable time
        err(f"[usage] seconds={s.open_seconds():.1f}")

    err("[listening… speak; phrases stream as you talk. Stop when done.]")
    ring = collections.deque(maxlen=PREROLL)
    session = None
    active = False           # currently passing real audio (within speech/hangover)
    speech_run = 0
    idle_run = 0
    try:
        while not stop["v"]:
            frame = read_frame()
            if frame is None:
                break
            speaking = audioop.rms(frame, 2) > threshold

            if session is None or session.closed:
                if session is not None:          # connection dropped on its own
                    bill(session)
                    session = None
                ring.append(frame)
                speech_run = speech_run + 1 if speaking else 0
                if speech_run >= START_FRAMES:   # speech began → open a connection
                    session = open_session()
                    for f in ring:
                        session.feed(f)
                    ring.clear()
                    active, speech_run, idle_run = True, 0, 0
                continue

            # connected
            if speaking:
                active, idle_run = True, 0
            else:
                idle_run += 1
                if active and idle_run >= HANG_FRAMES:
                    active = False
            session.feed(frame if active else SILENCE)
            if idle_run >= IDLE_CLOSE_FRAMES:    # long silence → stop billing
                err("[idle — closing connection]")
                session.finish()
                bill(session)
                session = None
                ring.clear()
                active, speech_run, idle_run = False, 0, 0
    finally:
        if session is not None:
            try:
                session.finish()
            except Exception:
                pass
            bill(session)
        try:
            rec["p"].terminate()
        except Exception:
            pass
    err('\n> "(streamed)"')
    # websocket / teardown can hang at interpreter shutdown.
    os._exit(0)


if __name__ == "__main__":
    main()
