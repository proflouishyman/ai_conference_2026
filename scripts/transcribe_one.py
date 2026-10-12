#!/usr/bin/env python3
"""Transcribe one media file with mlx-whisper (run with ~/venvs/whisper/bin/python).

Usage: transcribe_one.py <media-path>
Prints one JSON line {"ok": true, "text": "..."} and exits 0, or
{"ok": false, "error": "..."} and exits 1. Empty text is a failure.
Needs ffmpeg on PATH (/opt/homebrew/bin). Everything stays on this machine.

v5 review: the container's duration header is attacker-controlled (a 40 MB Opus file can hold many hours
of audio while claiming 30 s), so ffprobe's duration alone does not bound the work. decode_audio() runs
ffmpeg itself (argv list, absolute path, timeout) and stops after MAX_DURATION_S seconds of output, and
whisper only ever sees those samples. Paths are made absolute so a name can never be read as an option.
"""
import json
import os
import subprocess
import sys

MODEL = "mlx-community/whisper-large-v3-turbo"
MAX_DURATION_S = 100            # the site caps recordings at 90 s (Apps Script allows 95)
PROBE_TIMEOUT_S = 30
DECODE_TIMEOUT_S = 120
SAMPLE_RATE = 16000
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
    path = os.path.abspath(path)
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


def decode_audio(path):
    """Mono 16 kHz float32 samples of at most the first MAX_DURATION_S seconds. Raises MediaRejected."""
    import numpy as np
    path = os.path.abspath(path)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "2", "-i", path,
           "-t", str(MAX_DURATION_S), "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(SAMPLE_RATE),
           "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=DECODE_TIMEOUT_S, env=dict(os.environ, PATH=TOOL_PATH))
    except subprocess.TimeoutExpired:
        raise MediaRejected("ffmpeg decode timed out after %ds" % DECODE_TIMEOUT_S)
    except OSError as exc:
        raise MediaRejected("ffmpeg could not run: %s" % exc)
    if r.returncode != 0:
        raise MediaRejected("ffmpeg could not decode the audio: %s" % (r.stderr or b"").decode("utf-8", "replace").strip()[:200])
    limit = (MAX_DURATION_S + 1) * SAMPLE_RATE * 2
    if len(r.stdout) > limit:
        raise MediaRejected("decoded audio longer than %ds" % MAX_DURATION_S)
    if len(r.stdout) < SAMPLE_RATE // 10 * 2:
        raise MediaRejected("no decodable audio")
    return np.frombuffer(r.stdout[:len(r.stdout) // 2 * 2], np.int16).astype(np.float32) / 32768.0


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: transcribe_one.py <media-path>"}))
        return 1
    try:
        check_media(sys.argv[1])
        audio = decode_audio(sys.argv[1])
    except MediaRejected as exc:
        print(json.dumps({"ok": False, "error": "media rejected: %s" % exc}))
        return 1
    try:
        import mlx_whisper
        res = mlx_whisper.transcribe(audio, path_or_hf_repo=MODEL, language="en")
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
