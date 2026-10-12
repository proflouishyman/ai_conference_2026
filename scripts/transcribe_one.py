#!/usr/bin/env python3
"""Transcribe one media file with mlx-whisper (run with ~/venvs/whisper/bin/python).

Usage: transcribe_one.py <media-path>
Prints one JSON line {"ok": true, "text": "..."} and exits 0, or
{"ok": false, "error": "..."} and exits 1. Empty text is a failure.
Needs ffmpeg on PATH (/opt/homebrew/bin). Everything stays on this machine.
"""
import json
import os
import subprocess
import sys

MODEL = "mlx-community/whisper-large-v3-turbo"
MAX_DURATION_S = 100            # the site caps recordings at 90 s (Apps Script allows 95)
PROBE_TIMEOUT_S = 30
TOOL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
EXT_MIME = {".mp4": "video/mp4", ".webm": "video/webm"}


class MediaRejected(Exception):
    """The file is not an acceptable recording. Never retried, never passed to ffmpeg/whisper."""


def magic_ok(head, mime):
    """Magic bytes must match the declared mime: mp4 'ftyp' at offset 4, webm EBML header."""
    if mime == "video/mp4":
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if mime == "video/webm":
        return head[:4] == b"\x1a\x45\xdf\xa3"
    return False


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _packet_duration(path):
    """Last packet timestamp via ffprobe (demux only, no decode), for files with no duration header."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "packet=pts_time",
                            "-of", "csv=p=0", path], capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT_S, env=dict(os.environ, PATH=TOOL_PATH))
    except subprocess.TimeoutExpired:
        raise MediaRejected("ffprobe packet scan timed out")
    except OSError as exc:
        raise MediaRejected("ffprobe could not run: %s" % exc)
    vals = [_num(x.split(",")[0]) for x in (r.stdout or "").splitlines()]
    vals = [v for v in vals if v is not None]
    if r.returncode != 0 or not vals:
        raise MediaRejected("cannot determine duration")
    return max(vals)


def check_media(path, mime=None, probe=True):
    """Gate before ffmpeg/whisper: regular file, extension from the fixed map, magic bytes match
    mime, then ffprobe (argv list, timeout) must find an audio or video stream and a duration
    of at most MAX_DURATION_S. Returns the duration in seconds. Raises MediaRejected."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in EXT_MIME:
        raise MediaRejected("extension %r not allowed" % ext)
    if mime is None:
        mime = EXT_MIME[ext]
    elif EXT_MIME[ext] != mime:
        raise MediaRejected("extension %s does not match mime %s" % (ext, mime))
    if not os.path.isfile(path) or os.path.islink(path):
        raise MediaRejected("not a regular file")
    with open(path, "rb") as fh:
        head = fh.read(16)
    if not magic_ok(head, mime):
        raise MediaRejected("magic bytes do not match %s" % mime)
    if not probe:
        return 0.0
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                            "-show_streams", path], capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT_S, env=dict(os.environ, PATH=TOOL_PATH))
    except subprocess.TimeoutExpired:
        raise MediaRejected("ffprobe timed out after %ds" % PROBE_TIMEOUT_S)
    except OSError as exc:
        raise MediaRejected("ffprobe could not run: %s" % exc)
    if r.returncode != 0:
        raise MediaRejected("ffprobe rejected the file: %s" % (r.stderr or "").strip()[:200])
    try:
        info = json.loads(r.stdout)
        kinds = [st.get("codec_type") for st in info.get("streams", [])]
        dur = _num((info.get("format") or {}).get("duration"))
        if dur is None:
            for st in info.get("streams", []):
                dur = _num(st.get("duration")) or dur
    except Exception as exc:
        raise MediaRejected("ffprobe output unusable: %s" % type(exc).__name__)
    if dur is None:
        dur = _packet_duration(path)         # browser-made webm often carries no duration header
    if not any(k in ("audio", "video") for k in kinds):
        raise MediaRejected("no audio or video stream")
    if not (0 < dur <= MAX_DURATION_S):
        raise MediaRejected("duration %.1fs outside 0..%ds" % (dur, MAX_DURATION_S))
    return dur


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: transcribe_one.py <media-path>"}))
        return 1
    try:
        check_media(sys.argv[1])
    except MediaRejected as exc:
        print(json.dumps({"ok": False, "error": "media rejected: %s" % exc}))
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
