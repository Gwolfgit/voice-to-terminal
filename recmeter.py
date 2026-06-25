#!/usr/bin/env python3
"""Record a PulseAudio source (via parec) to a WAV file with live levels.

Used by the voice GUI's 2-way mic test: manual start/stop (NO timer). Stop with
SIGINT/SIGTERM to finalize the WAV. parec is killed on stop, so a dead/silent
source (e.g. a Bluetooth mic that isn't streaming) can never hang the recorder.

Usage:  recmeter.py --out FILE [--source NAME]
Emits on stderr:  "LEVEL <peak_dBFS>"  every ~100 ms; "[stopped] N bytes, T s".
"""
import argparse
import array
import math
import os
import signal
import subprocess
import sys
import wave

RATE = 16000


def err(*a):
    print(*a, file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--source", default=None)
    a = ap.parse_args()

    cmd = ["parec", "--format=s16le", f"--rate={RATE}", "--channels=1",
           "--latency-msec=50"]   # without this, piped parec stalls after one fragment
    if a.source:
        cmd.append(f"--device={a.source}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    wf = wave.open(a.out, "wb")
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(RATE)

    stop = {"v": False}

    def on_stop(sig, frame):
        stop["v"] = True
        try:
            proc.terminate()        # close the pipe so a blocked read returns EOF
        except Exception:
            pass
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    err("[recording]")
    chunk = int(RATE * 0.1) * 2     # 100 ms of s16le mono
    total = 0
    try:
        while not stop["v"]:
            data = proc.stdout.read(chunk)
            if not data:
                break
            wf.writeframes(data)
            total += len(data)
            s = array.array("h")
            s.frombytes(data)
            peak = max((abs(x) for x in s), default=0)
            db = 20 * math.log10(peak / 32768.0) if peak > 0 else -120.0
            err(f"LEVEL {db:.1f}")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        wf.close()
    err(f"[stopped] {total} bytes, {total / 2 / RATE:.1f}s")
    os._exit(0)


if __name__ == "__main__":
    main()
