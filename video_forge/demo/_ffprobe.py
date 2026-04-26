"""Single source of truth for media duration probing.

Why a dedicated helper: ffprobe's `format=duration` is unreliable for some
mp3 outputs from ffmpeg's loudnorm filter — the format header reports a
shorter duration than the actual decoded audio. This silently broke
SRT/audio alignment in the pipeline (subs computed against 81.4s timeline
while audio was actually 83s).

Fix: decode the media end-to-end with `ffmpeg -f null` and parse the
final `time=HH:MM:SS.ms` from stderr. Always returns the true playback
length. Tiny cost (~100ms for an 80s file).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")


def media_duration(path: Path) -> float:
    """Return decoded duration of an audio or video file in seconds.

    Decode-based: runs `ffmpeg -i path -f null -` and reads the last
    `time=HH:MM:SS.ms` line from stderr. Reliable across mp3 / wav / mp4
    regardless of header lies.
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-stats",
         "-i", str(path),
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    stderr = proc.stderr or ""
    matches = _TIME_RE.findall(stderr)
    if not matches:
        # Fall back to ffprobe format duration if parsing fails for any reason.
        return _format_duration_fallback(path)
    h, m, s = matches[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def _format_duration_fallback(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float((proc.stdout or "").strip() or "0")
    except ValueError:
        return 0.0


def format_duration(path: Path) -> float:
    """Format-header duration (the unreliable one). Exposed for parity-check
    assertions — the orchestrator can compare media_duration vs this and flag
    a mismatch as signal that an mp3 has lying headers.
    """
    return _format_duration_fallback(path)
