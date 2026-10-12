#!/usr/bin/env python3
"""Transcribe one media file with mlx-whisper (run with ~/venvs/whisper/bin/python).

Usage: transcribe_one.py <media-path>
Prints one JSON line {"ok": true, "text": "..."} and exits 0, or
{"ok": false, "error": "..."} and exits 1. Empty text is a failure.
Needs ffmpeg on PATH (/opt/homebrew/bin). Everything stays on this machine.
"""
import json
import sys

MODEL = "mlx-community/whisper-large-v3-turbo"


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: transcribe_one.py <media-path>"}))
        return 1
    try:
        import mlx_whisper
        res = mlx_whisper.transcribe(sys.argv[1], path_or_hf_repo=MODEL, language="en")
        text = (res.get("text") or "").strip()
        if not text:
            print(json.dumps({"ok": False, "error": "empty transcript"}))
            return 1
        print(json.dumps({"ok": True, "text": text}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:300])}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
