"""Forced-alignment-equivalent: get word-level timings for the SCRIPT text.

We don't have a real forced aligner available (aeneas is incompatible with
modern numpy / Python 3.13). Instead we use OpenAI's whisper-1 with word
timestamps to get *timings*, then substitute the script's actual words at
those positions. The text we emit is ALWAYS the script's words, so brand
mishearings ("Trollopson", "Markforged") are structurally impossible.

This is sometimes called "guided ASR" or "anchored transcription". For
synthesized speech with a known transcript, the alignment is near-perfect
because the audio was generated *from* this exact text.

Approach:
  1. Whisper word timestamps  → list of (asr_word, start, end)
  2. Script tokenized          → list of script_words (with original casing/punctuation)
  3. Sequential alignment      → pair script_words to asr_words; on length mismatch,
                                 distribute timings proportionally to absorb
                                 insertions/deletions
  4. Emit JSON in the same shape as ElevenLabs Scribe's output so downstream
     SRT chunkers and the lexicon canonicalizer don't change

Output schema mirrors Scribe (compatibility with build_master_srt):
  {
    "language_code": "...",
    "audio_duration_secs": 60.5,
    "text": "<full script text>",
    "words": [
      {"text": "Mark", "start": 0.12, "end": 0.51, "type": "word"},
      ...
    ]
  }
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from openai import OpenAI

from ..tts import get_openai_key

log = logging.getLogger(__name__)

WHISPER_MODEL = "whisper-1"

# Tokenize the script into "word" units that pair 1:1 with what Whisper emits.
# Strategy: split on whitespace and treat each whitespace-separated chunk as a
# token, preserving its punctuation. This matches Whisper's word_timestamps
# behavior (Whisper emits words with leading/trailing punctuation attached).
_TOKEN_SPLIT = re.compile(r"\s+")


def _script_tokens(script_text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(script_text.strip()) if t]


def _audio_duration(audio_path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        text=True,
    )
    return float(out.strip() or "0")


_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")
_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")


def _detect_speech_onset(audio_path: Path, noise_db: int = -30, min_dur_s: float = 0.05) -> float:
    """Return the timestamp (seconds) of the first non-silent audio sample.

    Whisper-1 word_timestamps tends to anchor the first word at t=0 even when
    the audio has natural lead-in silence (TTS engines almost always produce
    100-400ms of silence before speech). Without correction, every subtitle
    cue appears 100-400ms before the corresponding spoken word.

    Returns 0.0 if no leading silence is detected (rare for synth voice).
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats",
         "-i", str(audio_path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_dur_s}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    stderr = proc.stderr or ""
    # First silence_start is at 0 (lead silence). Its silence_end is the
    # speech onset.
    first_start_match = _SILENCE_START_RE.search(stderr)
    if not first_start_match:
        return 0.0
    first_start = float(first_start_match.group(1))
    if first_start > 0.05:
        # First detected silence isn't at the very start — audio begins with speech.
        return 0.0
    end_match = _SILENCE_END_RE.search(stderr)
    if not end_match:
        return 0.0
    return float(end_match.group(1))


def _shift_word_timings(words: list[dict], offset_s: float, audio_dur: float) -> list[dict]:
    """Apply a uniform offset to start/end and clamp to [0, audio_dur]."""
    if abs(offset_s) < 0.001:
        return words
    out: list[dict] = []
    for w in words:
        s = max(0.0, w["start"] + offset_s)
        e = min(audio_dur, w["end"] + offset_s)
        if e < s:  # never invert
            e = s
        out.append({**w, "start": round(s, 3), "end": round(e, 3)})
    return out


def _normalize_for_match(s: str) -> str:
    """Lowercase + strip punctuation for fuzzy comparison only."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _whisper_word_timestamps(audio_path: Path) -> tuple[str, list[dict]]:
    """Call Whisper API. Returns (full_text, [{word, start, end}, ...])."""
    client = OpenAI(api_key=get_openai_key())
    with open(audio_path, "rb") as f:
        resp = client.audio.transcriptions.create(
            file=f,
            model=WHISPER_MODEL,
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )
    # SDK returns either dict or pydantic; normalize.
    if hasattr(resp, "model_dump"):
        data = resp.model_dump()
    else:
        data = dict(resp)
    full_text = data.get("text", "") or ""
    words = data.get("words") or []
    out: list[dict] = []
    for w in words:
        word = (w.get("word") or w.get("text") or "").strip()
        if not word:
            continue
        out.append({"word": word, "start": float(w.get("start", 0.0)), "end": float(w.get("end", 0.0))})
    return full_text, out


def _align(script_tokens: list[str], asr_words: list[dict], audio_dur: float) -> list[dict]:
    """Pair script tokens to ASR word-timings via sequential matching.

    Strategy (rare-but-handled cases):
      - Equal counts: pair index-for-index, use ASR start/end, emit script text.
      - ASR has fewer (Whisper merged some): each ASR span gets multiple script
        tokens distributed proportionally by character length.
      - ASR has more (Whisper hallucinated extras): drop extras at the tail.

    Always emits len(script_tokens) entries.
    """
    n_script = len(script_tokens)
    n_asr = len(asr_words)
    out: list[dict] = []

    if n_script == 0:
        return out

    if n_asr == 0:
        # Total fallback: distribute uniformly across audio_dur.
        if audio_dur <= 0:
            audio_dur = max(1.0, n_script * 0.4)
        per = audio_dur / n_script
        for i, t in enumerate(script_tokens):
            out.append({
                "text": t,
                "start": round(i * per, 3),
                "end": round((i + 1) * per, 3),
                "type": "word",
            })
        return out

    if n_script == n_asr:
        for t, a in zip(script_tokens, asr_words):
            out.append({
                "text": t,
                "start": round(a["start"], 3),
                "end": round(a["end"], 3),
                "type": "word",
            })
        return out

    # Mismatched counts — distribute proportionally.
    # Map each script token to a fractional position along the ASR timeline.
    asr_start = asr_words[0]["start"]
    asr_end = asr_words[-1]["end"]
    if asr_end <= asr_start:
        asr_end = asr_start + max(1.0, audio_dur - asr_start)

    # Total character weight of script
    char_weights = [max(1, len(t)) for t in script_tokens]
    total_chars = sum(char_weights)
    cursor = asr_start
    for t, w in zip(script_tokens, char_weights):
        share = (w / total_chars) * (asr_end - asr_start)
        out.append({
            "text": t,
            "start": round(cursor, 3),
            "end": round(cursor + share, 3),
            "type": "word",
        })
        cursor += share
    return out


def align_script_to_audio(
    script_text: str,
    audio_path: Path,
    out_json_path: Path,
) -> dict:
    """Produce a Scribe-shaped JSON file with word-level timings for the script.

    Returns a measurement dict for pipeline.log.json:
      {audio_dur_s, asr_word_count, script_word_count, alignment_drift_s,
       fallback_used: bool, json_path}
    """
    audio_dur = _audio_duration(audio_path)
    script_tokens = _script_tokens(script_text)

    asr_text = ""
    asr_words: list[dict] = []
    fallback_used = False
    try:
        asr_text, asr_words = _whisper_word_timestamps(audio_path)
    except Exception as e:
        log.warning("whisper word-timestamps failed (%s) — falling back to uniform distribution", e)
        fallback_used = True

    aligned = _align(script_tokens, asr_words, audio_dur)

    # Whisper-anchor correction: detect actual speech onset; if Whisper put
    # the first word earlier than that, shift everything forward so the SRT
    # lines up with what the listener actually hears.
    speech_onset_s = _detect_speech_onset(audio_path)
    onset_offset_s = 0.0
    if aligned and speech_onset_s > 0.05:
        first_aligned_start = aligned[0]["start"]
        if first_aligned_start < speech_onset_s:
            onset_offset_s = speech_onset_s - first_aligned_start
            aligned = _shift_word_timings(aligned, onset_offset_s, audio_dur)

    # Drift: distance between last cue end and audio duration.
    last_end = aligned[-1]["end"] if aligned else 0.0
    drift = abs(audio_dur - last_end)

    payload = {
        "language_code": "eng",  # whisper-1 verbose_json contains language; we don't propagate
        "audio_duration_secs": round(audio_dur, 3),
        "text": script_text.strip(),
        "asr_text": asr_text.strip(),  # what Whisper actually heard, for QC
        "words": aligned,
        "alignment_meta": {
            "asr_word_count": len(asr_words),
            "script_word_count": len(script_tokens),
            "fallback_used": fallback_used,
            "drift_s": round(drift, 3),
            "speech_onset_s": round(speech_onset_s, 3),
            "onset_offset_applied_s": round(onset_offset_s, 3),
        },
    }
    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    out_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return {
        "audio_dur_s": round(audio_dur, 3),
        "asr_word_count": len(asr_words),
        "script_word_count": len(script_tokens),
        "alignment_drift_s": round(drift, 3),
        "fallback_used": fallback_used,
        "speech_onset_s": round(speech_onset_s, 3),
        "onset_offset_applied_s": round(onset_offset_s, 3),
        "json_path": str(out_json_path),
    }
