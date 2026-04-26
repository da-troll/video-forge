"""Audio loudness normalization to broadcast standard.

Targets:
  I  = -14 LUFS  (integrated loudness — YouTube/IG/TikTok/X/LinkedIn standard)
  TP = -1   dBTP (true peak headroom)
  LRA= 11   LU   (loudness range)

Two-pass:
  1. measure_loudness_audio: ffmpeg loudnorm with print_format=json — returns
     measured_I/TP/LRA/thresh/target_offset for the input
  2. apply_loudnorm_two_pass_audio: feeds those measurements back into a
     second loudnorm filter pass for accurate normalization

Mirrors the algorithm in upstream helpers/render.py but emits audio (mp3
@ 192kbps / 48kHz) instead of video. Falls back to a one-pass
approximation if the measurement step fails.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness_audio(audio_path: Path) -> dict | None:
    """Run loudnorm first pass on an audio file. Returns the measurement dict
    (input_i, input_tp, input_lra, input_thresh, target_offset, ...) or None.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(audio_path),
            "-af", filter_str,
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    stderr = proc.stderr or ""
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def measure_integrated_lufs(audio_or_video_path: Path) -> float | None:
    """Compute integrated LUFS using the ebur128 filter (used for QC after
    normalization to verify we hit the -14 target). Returns None on failure.
    """
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats",
            "-i", str(audio_or_video_path),
            "-af", "ebur128",
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    # ebur128 emits a "Summary:" block with "I:" line containing integrated LUFS.
    stderr = proc.stderr or ""
    marker = stderr.rfind("Summary:")
    if marker == -1:
        return None
    tail = stderr[marker:]
    for line in tail.splitlines():
        line = line.strip()
        if line.startswith("I:"):
            # Format: "I:         -14.0 LUFS"
            parts = line.split()
            for tok in parts[1:]:
                try:
                    return float(tok)
                except ValueError:
                    continue
    return None


def _detect_audio_codec(audio_path: Path) -> tuple[str, int]:
    """Return (output_codec, output_bitrate_k). Match input where reasonable;
    default to libmp3lame @ 192k for mp3 inputs, pcm for wav."""
    suffix = audio_path.suffix.lower()
    if suffix == ".wav":
        return "pcm_s16le", 0  # bitrate ignored for PCM
    return "libmp3lame", 192


def apply_loudnorm_two_pass_audio(
    in_path: Path,
    out_path: Path,
) -> dict:
    """Two-pass loudnorm on audio. Returns:
      {ok, measured_in: {...}, target: {I, TP, LRA},
       measured_out_lufs: float | None, fallback_used: bool}
    """
    target = {"I": LOUDNORM_I, "TP": LOUDNORM_TP, "LRA": LOUDNORM_LRA}

    measured = measure_loudness_audio(in_path)
    fallback_used = measured is None
    codec, bitrate_k = _detect_audio_codec(out_path)

    common_codec_args: list[str] = ["-c:a", codec]
    if codec == "libmp3lame":
        common_codec_args += ["-b:a", f"{bitrate_k}k"]
    common_codec_args += ["-ar", "48000"]

    if not fallback_used:
        filter_str = (
            f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
            f":measured_I={measured['input_i']}"
            f":measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}"
            f":measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}"
            f":linear=true"
        )
    else:
        # Single-pass approximation when measurement failed.
        log.warning("loudnorm measurement failed for %s — using single-pass", in_path)
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"

    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(in_path),
            "-vn",
            "-af", filter_str,
            *common_codec_args,
            str(out_path),
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "fallback_used": fallback_used,
            "measured_in": measured,
            "target": target,
            "measured_out_lufs": None,
            "error": (proc.stderr or "")[-400:],
        }

    measured_out = measure_integrated_lufs(out_path)
    return {
        "ok": True,
        "fallback_used": fallback_used,
        "measured_in": measured,
        "target": target,
        "measured_out_lufs": measured_out,
    }
