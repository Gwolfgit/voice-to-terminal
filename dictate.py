#!/usr/bin/env python3
"""Continuous offline dictation with Vosk.

Listen on the mic and type each finished phrase into the focused window as soon
as you pause — no per-sentence Ctrl-C, no partial-text spam. Speak, pause, the
clean text appears at your cursor. Ctrl-C once to stop for good.

Usage: dictate.py [lang] [--print] [--device N] [--list-devices]
    lang   en | ru | uk   (default: en, or $VOSK_LANG)
    --print   print finished phrases to stdout instead of typing them
"""
import argparse
import json
import os
import queue
import subprocess
import sys

import sounddevice as sd
from vosk import Model, KaldiRecognizer, SetLogLevel

BASE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(BASE, "models")
SAMPLE_RATE = 16000


def err(*a, end="\n"):
    print(*a, file=sys.stderr, flush=True, end=end)


def emit(text, to_stdout):
    """Type (or print) one finished phrase, with a trailing space."""
    if not text:
        return
    if to_stdout:
        print(text, flush=True)
        return
    try:
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--", text + " "],
                       check=True)
    except FileNotFoundError:
        print(text, flush=True)


def main():
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("lang", nargs="?", default=os.environ.get("VOSK_LANG", "en"))
    p.add_argument("--print", dest="to_stdout", action="store_true")
    p.add_argument("--device", type=int, default=None)
    p.add_argument("--list-devices", action="store_true")
    args = p.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return 0

    model_dir = os.path.join(MODELS, args.lang)
    if not os.path.isdir(model_dir):
        err(f"No model for '{args.lang}' at {model_dir}")
        err(f"Available: {', '.join(sorted(os.listdir(MODELS))) or '(none)'}")
        return 2

    SetLogLevel(-1)
    err(f"[loading {args.lang} model...]")
    model = Model(model_dir)
    rec = KaldiRecognizer(model, SAMPLE_RATE)
    rec.SetWords(False)

    q = queue.Queue()

    def cb(indata, frames, time, status):
        if status:
            err(status)
        q.put(bytes(indata))

    err("🎤 Dictating — just talk. Pause = it types your words. Ctrl-C to stop.")
    last_partial = ""
    try:
        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=8000,
                               device=args.device, dtype="int16",
                               channels=1, callback=cb):
            while True:
                data = q.get()
                if rec.AcceptWaveform(data):
                    text = json.loads(rec.Result()).get("text", "").strip()
                    if text:
                        # clear the live preview line, then commit the phrase
                        err("\r" + " " * (len(last_partial) + 6) + "\r", end="")
                        last_partial = ""
                        emit(text, args.to_stdout)
                else:
                    part = json.loads(rec.PartialResult()).get("partial", "")
                    if part and part != last_partial:
                        # single overwriting preview line — no stacking spam
                        pad = max(0, len(last_partial) - len(part))
                        err("\r  … " + part + " " * pad, end="")
                        last_partial = part
    except KeyboardInterrupt:
        pass

    # flush any trailing words captured at the moment of Ctrl-C
    tail = json.loads(rec.FinalResult()).get("text", "").strip()
    err("\r" + " " * 80 + "\r", end="")
    if tail:
        emit(tail, args.to_stdout)
    err("\n[stopped]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
