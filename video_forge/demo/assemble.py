"""Assemble walkthrough.mp4 + voiceover audio + word-timed SRT into demo.mp4.

The full upstream EDL/render pipeline targets cut-from-source A-roll, which
isn't quite our shape (we have B-roll walkthrough + separately-recorded
voiceover). We honor the upstream Hard Rules where they apply:

  - Rule 1: subtitles applied LAST in the filter chain
  - Rule 3: 30ms audio fades at boundaries
  - Rule 5: SRT times derived from word.start/end directly (single timeline,
            no segment offsets needed)
  - Rule 8: word-level Scribe transcript (verbatim, untouched)

Subtitle force_style: phrase-length UPPERCASE chunks (~5 words, punctuation-broken),
Helvetica 14 Bold, MarginV=24. Wider chunks reduce sync-checkpoint perception
vs the prior 2-word style — fewer cue boundaries, longer reading windows.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..references import canonicalize_brand_terms

PUNCT_BREAK = set(".,!?;:")
# Target words per SRT cue. 2-word chunks felt sparse and over-emphasized
# every minor desync; ~5-word phrasing gives natural caption rhythm and
# longer per-cue dwell. Punctuation always closes a cue early.
SUB_CHUNK_WORDS = 5
SUB_STYLE = (
    "FontName=Helvetica,FontSize=14,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=1.2,Shadow=0,"
    "Bold=1,Alignment=2,MarginV=24,WrapStyle=2"
)


def _srt_ts(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _chunk_words(words: list[dict]) -> list[list[dict]]:
    """Group word records into phrase-length UPPERCASE chunks (~SUB_CHUNK_WORDS),
    closing a chunk early on terminal punctuation so phrases break naturally."""
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
        if len(current) >= SUB_CHUNK_WORDS or ends_in_punct:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


# Minimum SRT cue display duration. Below this, captions flash too fast to
# read; assertions also floor at 0.15s. We enforce by extending each cue's
# end timestamp toward the next cue's start (or +MIN_CUE_S, whichever is
# sooner). The underlying word-level timings stay honest; only the visible
# cue is stretched.
SRT_MIN_CUE_S = 0.20


# Minimum gap (seconds) between consecutive SRT cues. libass treats
# overlapping cues by stacking them — if cue N's end-time spills past
# cue N+1's start, the renderer puts cue N+1 ABOVE the bottom row to
# keep both visible. That's the "row 1 vs row 2" symptom Daniel saw on
# 0:26-0:27 and 0:29-0:31 of the H1+H2 demo.
SRT_INTER_CUE_GAP_S = 0.010


def _clip_overlaps(cues: list[tuple[float, float, str]]) -> tuple[list[tuple[float, float, str]], int]:
    """Ensure cue N+1.start > cue N.end + SRT_INTER_CUE_GAP_S.

    Whisper-1 occasionally returns word timestamps where word_n.end is
    later than word_{n+1}.start (or even identical to it), and the cue
    chunker propagates these overlaps when it takes word[0].start /
    word[-1].end as cue boundaries. This pass walks the cue list and
    pulls each cue's end back so it leaves at least a 10ms gap before
    the next cue starts. The cue's start is never touched, only the
    end is shortened — caller's min-duration enforcement runs AFTER
    this so any cue clipped below the readable floor is then either
    stretched (if room) or merged with its neighbor.

    Returns (clipped_cues, clip_count) where clip_count is the number
    of cues whose end was actually moved.
    """
    if not cues:
        return cues, 0
    out = list(cues)
    clipped = 0
    for i in range(len(out) - 1):
        s, e, t = out[i]
        next_s = out[i + 1][0]
        if e > next_s - SRT_INTER_CUE_GAP_S:
            new_e = max(s + 0.05, next_s - SRT_INTER_CUE_GAP_S)
            if abs(new_e - e) > 0.001:
                out[i] = (s, new_e, t)
                clipped += 1
    return out, clipped


def _enforce_cue_min_duration(cues: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Ensure visible duration of every cue ≥ SRT_MIN_CUE_S.

    Strategy (in order of preference):
      1. Stretch this cue's end toward the next cue's start
      2. If still too short AND there's a next cue: merge with next cue
         (combined text, end = next.end). The merged cue's duration
         strictly grows so will pass the floor.

    Operates on (start, end, text) tuples; returns a new list (length may
    shrink due to merges).
    """
    if not cues:
        return cues
    out: list[tuple[float, float, str]] = []
    i = 0
    while i < len(cues):
        s, e, t = cues[i]
        if e - s >= SRT_MIN_CUE_S:
            out.append((s, e, t))
            i += 1
            continue
        next_start = cues[i + 1][0] if i + 1 < len(cues) else (e + SRT_MIN_CUE_S)
        stretched_end = min(next_start, s + SRT_MIN_CUE_S)
        if stretched_end - s >= SRT_MIN_CUE_S:
            out.append((s, stretched_end, t))
            i += 1
            continue
        # Can't reach the floor by stretching → merge with next cue.
        if i + 1 < len(cues):
            ns, ne, nt = cues[i + 1]
            out.append((s, ne, f"{t} {nt}".strip()))
            i += 2
        else:
            # Last cue and can't extend — emit floored regardless (assertions
            # may still flag, but that's a signal worth seeing).
            out.append((s, s + SRT_MIN_CUE_S, t))
            i += 1
    return out


def _canonicalize_word_stream(words: list[dict]) -> tuple[list[dict], int]:
    """Apply lexicon canonicalize_brand_terms() across the JOINED word stream,
    then redistribute the canonicalized tokens back to per-word records.

    Why not per-cue: the 2-word chunker can split a multi-word rule pattern
    ("Tollefsen household") into separate cues, so per-cue canon misses it.
    Word-count-preserving substitution rules in brand-lexicon.yaml guarantee
    that token count is invariant — so we can splice the new tokens back to
    the original word records by index.

    Returns (mutated_words, replacements_count). Original timing/type
    metadata is preserved on each word; only `text` may change.
    """
    if not words:
        return [], 0
    joined = " ".join((w.get("text") or "") for w in words)
    canon = canonicalize_brand_terms(joined)
    if canon == joined:
        return words, 0
    new_tokens = canon.split(" ")
    replacements = 0
    if len(new_tokens) != len(words):
        # Unexpected — rules should be word-count-preserving. Fall back to
        # per-word leaving original; logged via the returned 0 count.
        return words, 0
    out: list[dict] = []
    for w, new_text in zip(words, new_tokens):
        if (w.get("text") or "") != new_text:
            replacements += 1
            out.append({**w, "text": new_text})
        else:
            out.append(w)
    return out, replacements


def build_master_srt(transcript_path: Path, srt_path: Path) -> dict:
    """Build the burned-in SRT from a Scribe-shaped transcript.

    Pipeline (the canonicalization order matters):
      1. canonicalize the FULL word stream (multi-word rules can span cues —
         must run before chunking, or "Tollefsen household" rules miss when
         the chunker split them apart)
      2. group word records into 2-word chunks, breaking on punctuation
      3. join each chunk to source-cased text
      4. uppercase + write

    Canonicalization is the last line of defense after script-side voice
    rules (Item B) and script-substitution alignment (Item C). Most of the
    time it should fire 0 times; it exists for the regression case where
    the script writer accidentally regresses to "Tollefsen household".

    Returns {cue_count, canonicalizations_applied} for pipeline.log.json.
    """
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    words = [w for w in transcript.get("words", []) if w.get("type") == "word"]

    canon_words, canon_applied = _canonicalize_word_stream(words)

    chunks = _chunk_words(canon_words)

    # Build raw cue tuples first so we can apply minimum-duration enforcement.
    raw_cues: list[tuple[float, float, str]] = []
    for chunk in chunks:
        start = float(chunk[0].get("start", 0.0))
        end = float(chunk[-1].get("end", start + 0.5))
        text = " ".join((w.get("text") or "").strip() for w in chunk).upper()
        raw_cues.append((start, end, text))

    # 1) Clip overlapping cues (libass would stack them on row 1 otherwise).
    clipped_cues, overlap_clips = _clip_overlaps(raw_cues)
    # 2) Stretch / merge cues below readable floor.
    cues = _enforce_cue_min_duration(clipped_cues)
    stretches = sum(1 for r, c in zip(clipped_cues, cues) if c[1] != r[1] or c[2] != r[2])

    lines: list[str] = []
    for i, (s, e, t) in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{_srt_ts(s)} --> {_srt_ts(e)}")
        lines.append(t)
        lines.append("")
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "cue_count": len(cues),
        "canonicalizations_applied": canon_applied,
        "overlap_clips_applied": overlap_clips,
        "min_duration_stretches": stretches,
    }


def _probe_duration(media: Path) -> float:
    # Decode-truth duration via shared helper. Format-header was swapped out
    # because loudnorm-output mp3 headers can lie about their decoded length.
    from ._ffprobe import media_duration
    return media_duration(media)


def _probe_video_fps(media: Path) -> float:
    """Return the video stream's frame rate as a float. Returns 25.0 if probing fails."""
    r = subprocess.run(
        ["ffprobe", "-v", "error",
         "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", str(media)],
        capture_output=True, text=True,
    )
    out = (r.stdout or "").strip()
    # r_frame_rate is "num/den"; e.g. "25/1" or "30000/1001".
    if "/" in out:
        try:
            num, den = out.split("/", 1)
            return float(num) / float(den) if float(den) else 25.0
        except (ValueError, ZeroDivisionError):
            pass
    try:
        return float(out)
    except ValueError:
        return 25.0


# Below this gap (seconds), the hold strategy is skipped — a sub-half-second
# tail isn't worth a re-encode and rounding noise in fade-out can synthesize
# spurious gaps.
HOLD_MIN_GAP_S = 0.5


def _extract_last_frame(walkthrough: Path, out_png: Path) -> bool:
    """Extract the walkthrough's final visible frame as PNG. Returns True on success."""
    walk_dur = _probe_duration(walkthrough)
    seek_at = max(0.0, walk_dur - 0.04)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{seek_at:.3f}",
        "-i", str(walkthrough),
        "-frames:v", "1",
        "-q:v", "2",
        str(out_png),
    ]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0 or not out_png.exists() or out_png.stat().st_size == 0:
        # fallback to mid-clip
        cmd[2] = f"{walk_dur / 2:.3f}"
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return out_png.exists() and out_png.stat().st_size > 0


def _build_held_tail(last_png: Path, gap_s: float, out_mp4: Path, *, fps: float = 25.0) -> None:
    """Encode a silent video of `gap_s` seconds holding `last_png` at `fps`.

    Critical: must match the walkthrough's frame rate exactly. concat-demuxer
    with -c copy reinterprets the held tail's stream PTS at the walkthrough's
    declared rate, so a mismatch produces visible time-scaling at the seam
    (the freeze at 0:23 in 63fda3b's demo was caused by 25fps walkthrough
    concatenated with 30fps held-tail).
    """
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(last_png),
        "-t", f"{gap_s:.3f}",
        "-r", str(fps),
        "-vsync", "cfr",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-an",
        str(out_mp4),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _concat_demuxer(parts: list[Path], out_mp4: Path, edit_dir: Path) -> bool:
    """Lossless concat with -c copy. Returns True on success; caller falls back to filter-graph if False."""
    list_path = edit_dir / "_concat_list.txt"
    list_path.write_text("\n".join(f"file '{p.resolve()}'" for p in parts) + "\n")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c", "copy", "-movflags", "+faststart",
        str(out_mp4),
    ]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    list_path.unlink(missing_ok=True)
    return r.returncode == 0


def _concat_filter_graph(parts: list[Path], out_mp4: Path) -> None:
    """Re-encode concat — used when -c copy fails due to codec param drift."""
    inputs: list[str] = []
    for p in parts:
        inputs += ["-i", str(p)]
    n = len(parts)
    fc = "".join(f"[{i}:v:0]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", fc,
        "-map", "[v]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        str(out_mp4),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def assemble(
    edit_dir: Path,
    *,
    walkthrough: Path,
    voiceover: Path,
    srt: Path,
    out: Path,
    tail_strategy: str = "hold",
) -> dict:
    """Compose final demo.mp4. Returns metadata dict for pipeline.log.json.

    Tail strategies (when voiceover is longer than walkthrough):
      - "hold"        : extract last frame, generate held silent video, concat (default)
      - "loop"        : -stream_loop the walkthrough (legacy behavior)
      - "trim_voice"  : trim voiceover to walkthrough length (debug-only)

    When walkthrough >= voiceover, we always trim walkthrough to voiceover.

    Honors upstream Hard Rule 1 (subtitles LAST in filter chain) and Hard
    Rule 3 (30ms audio fades).
    """
    if tail_strategy not in {"hold", "loop", "trim_voice"}:
        raise ValueError(f"unknown tail_strategy: {tail_strategy}")

    voice_dur = _probe_duration(voiceover)
    video_dur = _probe_duration(walkthrough)
    gap = max(0.0, voice_dur - video_dur)
    meta: dict = {
        "tail_strategy": tail_strategy,
        "voice_dur": round(voice_dur, 3),
        "walkthrough_dur": round(video_dur, 3),
        "tail_gap_s": round(gap, 3),
        "held_frame_path": None,
    }

    # When the walkthrough is already as long as (or longer than) the voiceover,
    # all strategies degenerate to: trim walkthrough to voiceover length.
    composed_video = walkthrough
    if voice_dur > video_dur and video_dur > 0:
        if tail_strategy == "trim_voice":
            # Use walkthrough as-is; the filter graph below will -shortest the audio.
            target_dur = video_dur
            composed_video = walkthrough
            meta["effective_target_dur"] = round(target_dur, 3)
        elif tail_strategy == "loop" or gap < HOLD_MIN_GAP_S:
            # Legacy / sub-half-second case: stream_loop and trim.
            target_dur = voice_dur
            meta["effective_target_dur"] = round(target_dur, 3)
            return _assemble_with_stream_loop(
                walkthrough, voiceover, srt, out,
                target_dur=target_dur, voice_dur=voice_dur, video_dur=video_dur,
                meta=meta,
            )
        else:  # "hold"
            held_png = edit_dir / "_last_frame.png"
            held_mp4 = edit_dir / "_hold_tail.mp4"
            stitched = edit_dir / "_stitched_walkthrough.mp4"
            ok = _extract_last_frame(walkthrough, held_png)
            if not ok:
                # Degrade to loop rather than crash.
                meta["hold_fallback"] = "frame_extract_failed"
                return _assemble_with_stream_loop(
                    walkthrough, voiceover, srt, out,
                    target_dur=voice_dur, voice_dur=voice_dur, video_dur=video_dur,
                    meta=meta,
                )
            walkthrough_fps = _probe_video_fps(walkthrough)
            _build_held_tail(held_png, gap, held_mp4, fps=walkthrough_fps)
            if not _concat_demuxer([walkthrough, held_mp4], stitched, edit_dir):
                meta["concat_method"] = "filter_graph_fallback"
                _concat_filter_graph([walkthrough, held_mp4], stitched)
            else:
                meta["concat_method"] = "demuxer_copy"
            composed_video = stitched
            meta["held_frame_path"] = str(held_png)
            meta["walkthrough_fps"] = round(walkthrough_fps, 3)
            meta["effective_target_dur"] = round(voice_dur, 3)

    target_dur = (
        meta.get("effective_target_dur")
        or (video_dur if (tail_strategy == "trim_voice" and voice_dur > video_dur) else max(voice_dur, video_dur, 1.0))
    )
    fade_out_start = max(0.0, target_dur - 0.03)

    # When trim_voice and voice > video, audio gets trimmed via -shortest below.
    afade_target_dur = (
        video_dur if tail_strategy == "trim_voice" and voice_dur > video_dur else target_dur
    )
    afade_out_start = max(0.0, afade_target_dur - 0.03)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(composed_video),
        "-i", str(voiceover),
        "-filter_complex",
        # Subtitles applied LAST (Hard Rule 1)
        f"[0:v]trim=duration={target_dur:.3f},setpts=PTS-STARTPTS,subtitles={srt}:force_style='{SUB_STYLE}'[v];"
        f"[1:a]afade=t=in:st=0:d=0.03,afade=t=out:st={afade_out_start:.3f}:d=0.03[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-shortest",
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return meta


def _assemble_with_stream_loop(
    walkthrough: Path, voiceover: Path, srt: Path, out: Path,
    *, target_dur: float, voice_dur: float, video_dur: float, meta: dict,
) -> dict:
    """Legacy stream_loop path — preserved for `tail_strategy='loop'` and short-gap fallback."""
    loop_count = 0
    if voice_dur > video_dur and video_dur > 0:
        loop_count = int(voice_dur / video_dur)
    fade_out_start = max(0.0, target_dur - 0.03)
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", str(loop_count), "-i", str(walkthrough),
        "-i", str(voiceover),
        "-filter_complex",
        f"[0:v]trim=duration={target_dur:.3f},setpts=PTS-STARTPTS,subtitles={srt}:force_style='{SUB_STYLE}'[v];"
        f"[1:a]afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_start:.3f}:d=0.03[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-shortest",
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    meta["loop_count"] = loop_count
    meta["effective_target_dur"] = round(target_dur, 3)
    return meta
