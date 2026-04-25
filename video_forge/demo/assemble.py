"""Assemble walkthrough.mp4 + voiceover audio + word-timed SRT into demo.mp4.

The full upstream EDL/render pipeline targets cut-from-source A-roll, which
isn't quite our shape (we have B-roll walkthrough + separately-recorded
voiceover). We honor the upstream Hard Rules where they apply:

  - Rule 1: subtitles applied LAST in the filter chain
  - Rule 3: 30ms audio fades at boundaries
  - Rule 5: SRT times derived from word.start/end directly (single timeline,
            no segment offsets needed)
  - Rule 8: word-level Scribe transcript (verbatim, untouched)

Subtitle force_style mirrors render.py exactly (2-word UPPERCASE chunks,
Helvetica 18 Bold, MarginV=90 — the proven 1920×1080 / 1080×1920 setting).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PUNCT_BREAK = set(".,!?;:")
SUB_STYLE = (
    "FontName=Helvetica,FontSize=18,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
    "Bold=1,Alignment=2,MarginV=90"
)


def _srt_ts(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _chunk_words(words: list[dict]) -> list[list[dict]]:
    """Group word records into 2-word UPPERCASE chunks, breaking on punctuation."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for w in words:
        if w.get("type") != "word":
            continue
        text = (w.get("text") or "").strip()
        if not text:
            continue
        current.append(w)
        ends_in_punct = text[-1] in PUNCT_BREAK
        if len(current) >= 2 or ends_in_punct:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def build_master_srt(transcript_path: Path, srt_path: Path) -> int:
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    words = [w for w in transcript.get("words", []) if w.get("type") == "word"]
    chunks = _chunk_words(words)
    lines: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        start = float(chunk[0].get("start", 0.0))
        end = float(chunk[-1].get("end", start + 0.5))
        text = " ".join((w.get("text") or "").strip() for w in chunk).upper()
        lines.append(str(i))
        lines.append(f"{_srt_ts(start)} --> {_srt_ts(end)}")
        lines.append(text)
        lines.append("")
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return len(chunks)


def _probe_duration(media: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(media)],
        check=True, stdout=subprocess.PIPE, text=True,
    )
    return float(r.stdout.strip() or "0")


def assemble(edit_dir: Path, *, walkthrough: Path, voiceover: Path, srt: Path, out: Path) -> Path:
    """Compose final demo.mp4.

    - Loops walkthrough silently to match voiceover duration if the
      voiceover is longer; trims if shorter (we want voiceover to be the
      master timeline).
    - Burns SRT LAST in the filter chain.
    - Applies 30ms audio fade in/out.
    """
    voice_dur = _probe_duration(voiceover)
    video_dur = _probe_duration(walkthrough)

    # Decide stream loop count: ceil(voice_dur / video_dur) - 1.
    loop_count = 0
    if voice_dur > video_dur and video_dur > 0:
        loop_count = int(voice_dur / video_dur)  # -stream_loop is "additional loops"

    target_dur = max(voice_dur, 1.0)
    fade_out_start = max(0.0, target_dur - 0.03)

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", str(loop_count), "-i", str(walkthrough),
        "-i", str(voiceover),
        "-filter_complex",
        # Subtitles are applied LAST (Hard Rule 1)
        f"[0:v]trim=duration={target_dur:.3f},setpts=PTS-STARTPTS,subtitles={srt}:force_style='{SUB_STYLE}'[v];"
        f"[1:a]afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_start:.3f}:d=0.03[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-shortest",
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return out
