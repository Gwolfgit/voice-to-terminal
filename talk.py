#!/usr/bin/env python3
"""Push-to-talk offline transcription with Vosk.

Listen on the microphone, show live partial results, and on Ctrl-C finalize the
transcript. By default the final text is typed into the currently focused X11
window via xdotool (so it lands at a coding-agent CLI prompt). Use --print to
emit to stdout instead (handy for `claude "$(talk --print)"`).

Usage:
    talk.py [lang] [options]

    lang            en | ru | uk   (default: en, or $VOSK_LANG)

Options:
    --print         Print final transcript to stdout; do NOT type it.
    --enter         After typing, also press Return (submit to the agent).
    --delay SEC     Wait SEC seconds before typing (alt-tab to target window).
    --list-devices  List audio input devices and exit.
    --device N      Use input device index N (default: system default).
"""
import argparse
import os
import queue
import subprocess
import sys

import sounddevice as sd
from vosk import Model, KaldiRecognizer, SetLogLevel

BASE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(BASE, "models")
SAMPLE_RATE = 16000


def err(*a):
    print(*a, file=sys.stderr, flush=True)


def main():
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("lang", nargs="?", default=os.environ.get("VOSK_LANG", "en"))
    p.add_argument("--print", dest="to_stdout", action="store_true")
    p.add_argument("--enter", action="store_true")
    p.add_argument("--delay", type=float, default=float(os.environ.get("VOSK_DELAY", "0")))
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--device", type=int, default=None)
    p.add_argument("--source", default=None,
                   help="capture this PulseAudio source via parec (handles "
                        "Bluetooth / odd rates); overrides --device")
    p.add_argument("--stream", action="store_true",
                   help="with --print: emit each finalized segment on its own "
                        "line as it is produced (live dictation), instead of one "
                        "aggregate transcript at the end")
    args = p.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return 0

    model_dir = os.path.join(MODELS, args.lang)
    if not os.path.isdir(model_dir):
        err(f"No model for '{args.lang}' at {model_dir}")
        err(f"Available: {', '.join(sorted(os.listdir(MODELS))) or '(none)'}")
        return 2

    SetLogLevel(-1)  # silence Kaldi logging
    err(f"[loading {args.lang} model...]")
    model = Model(model_dir)
    rec = KaldiRecognizer(model, SAMPLE_RATE)
    rec.SetWords(False)

    q = queue.Queue()

    def cb(indata, frames, time, status):
        if status:
            err(status)
        q.put(bytes(indata))

    err("[listening... speak, then Ctrl-C to finish]")
    import json
    import signal
    last_partial = ""
    segments = []
    stop = {"v": False}

    def on_stop(sig, frame):
        stop["v"] = True
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    def consume(data):
        nonlocal last_partial
        if rec.AcceptWaveform(data):
            seg = json.loads(rec.Result()).get("text", "").strip()
            if seg:
                segments.append(seg)
                last_partial = ""
                if args.stream and args.to_stdout:
                    sys.stdout.write(seg + "\n")
                    sys.stdout.flush()
        else:
            part = json.loads(rec.PartialResult()).get("partial", "")
            if part and part != last_partial:
                last_partial = part
                err(f"  … {part}")

    if args.source:
        # PulseAudio capture (Bluetooth-safe, auto-resampled to 16 kHz).
        pcmd = ["parec", "--format=s16le", f"--rate={SAMPLE_RATE}", "--channels=1",
                "--latency-msec=50", f"--device={args.source}"]
        rec_proc = {"p": subprocess.Popen(pcmd, stdout=subprocess.PIPE)}

        def kill_parec(*_):
            stop["v"] = True
            try:
                rec_proc["p"].terminate()
            except Exception:
                pass
        signal.signal(signal.SIGINT, kill_parec)
        signal.signal(signal.SIGTERM, kill_parec)
        while not stop["v"]:
            data = rec_proc["p"].stdout.read(16000)
            if not data:
                if stop["v"]:
                    break
                err("[mic dropped — reconnecting]")   # respawn on Bluetooth SCO drop
                try:
                    rec_proc["p"].terminate()
                except Exception:
                    pass
                rec_proc["p"] = subprocess.Popen(pcmd, stdout=subprocess.PIPE)
                continue
            consume(data)
        try:
            rec_proc["p"].terminate()
        except Exception:
            pass
    else:
        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=8000,
                               device=args.device, dtype="int16",
                               channels=1, callback=cb):
            while not stop["v"]:
                try:
                    data = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                consume(data)

    tail = json.loads(rec.FinalResult()).get("text", "").strip()

    if args.stream:
        # Segments were already emitted live; only flush the trailing piece
        # (the last utterance that hadn't finalized when we stopped).
        last = tail or last_partial
        if last and args.to_stdout:
            sys.stdout.write(last + "\n")
            sys.stdout.flush()
        err(f'\n> "(streamed)"')
        _hard_exit(0)

    if tail:
        segments.append(tail)
    elif last_partial:               # long single utterance never finalized
        segments.append(last_partial)
    final = " ".join(segments).strip()
    err(f"\n> \"{final}\"")

    if not final:
        err("[no speech recognized]")
        _hard_exit(0)

    if args.to_stdout:
        sys.stdout.write(final + "\n")
        sys.stdout.flush()
        _hard_exit(0)

    if args.delay > 0:
        err(f"[typing in {args.delay:g}s — focus target window]")
        import time
        time.sleep(args.delay)
    try:
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--", final], check=True)
        if args.enter:
            subprocess.run(["xdotool", "key", "Return"], check=True)
    except FileNotFoundError:
        err("[xdotool not found — printing instead]")
        sys.stdout.write(final + "\n")
        sys.stdout.flush()
    _hard_exit(0)


def _hard_exit(code):
    # PortAudio/Bluetooth SCO teardown can hang at interpreter shutdown;
    # flush our output and bypass it.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


if __name__ == "__main__":
    main()
