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

# gpt-4o-transcribe is the same OpenAI Audio API surface as whisper-1 but
# significantly faster (3-5x real-time vs whisper-1's ~1x observed in prod).
# Same word_timestamps response shape, same key.
WHISPER_MODEL = "gpt-4o-transcribe"

# Tokenize the script into "word" units that pair 1:1 with what Whisper emits.
# Strategy: split on whitespace and treat each whitespace-separated chunk as a
# token, preserving its punctuation. This matches Whisper's word_timestamps
# behavior (Whisper emits words with leading/trailing punctuation attached).
_TOKEN_SPLIT = re.compile(r"\s+")


def _script_tokens(script_text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(script_text.strip()) if t]


def _audio_duration(audio_path: Path) -> float:
    # Decode-truth via shared helper. Format-header durations on
    # loudnorm-output mp3s can lie under some conditions.
    from ._ffprobe import media_duration
    return media_duration(audio_path)


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

    # Mismatched counts → Needleman-Wunsch sequential alignment.
    return _align_nw(script_tokens, asr_words, audio_dur)


# ── Needleman-Wunsch alignment ──────────────────────────────────────────────
# When ASR ≠ script word counts, naive char-weight redistribution discards
# Whisper's actual per-word timings and drifts (natural speech rate is not
# proportional to character count — common short words pronounce fast,
# polysyllabic technical words pronounce slow). Instead, do classic
# DP sequence alignment between script tokens and ASR words. For matched
# pairs, take Whisper's actual timestamp. For runs of insertions/deletions,
# interpolate timestamps between the surrounding anchor pairs.

# Match score thresholds. Higher = stricter.
_MATCH_SCORE = 2
_MISMATCH_PENALTY = -1
_GAP_PENALTY = -1


def _normalize(token: str) -> str:
    """Case-fold + strip surrounding punctuation for fuzzy comparison."""
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _levenshtein(a: str, b: str, cap: int = 2) -> int:
    """Bounded Levenshtein. Returns cap+1 if distance exceeds cap (cheap exit)."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return cap + 1
    if la == 0 or lb == 0:
        return max(la, lb)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        curr = [i] + [0] * lb
        for j in range(1, lb + 1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if a[i - 1] == b[j - 1] else 1)
            curr[j] = min(ins, dele, sub)
        if min(curr) > cap:
            return cap + 1
        prev = curr
    return prev[lb]


def _token_match(s_tok: str, a_tok: str) -> bool:
    """Lenient match: case-insensitive, punctuation-stripped, Levenshtein ≤ 1."""
    s_norm = _normalize(s_tok)
    a_norm = _normalize(a_tok)
    if not s_norm or not a_norm:
        return False
    if s_norm == a_norm:
        return True
    return _levenshtein(s_norm, a_norm, cap=1) <= 1


def _nw_align_indices(script_tokens: list[str], asr_words: list[dict]) -> list[tuple[int | None, int | None]]:
    """Standard Needleman-Wunsch on (script_tokens, asr_words). Returns a list
    of (i, j) pairs where None signals an insertion or deletion. The list
    walks left-to-right covering every script index and every asr index.
    """
    n, m = len(script_tokens), len(asr_words)
    # Score matrix
    score = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0] = score[i - 1][0] + _GAP_PENALTY
    for j in range(1, m + 1):
        score[0][j] = score[0][j - 1] + _GAP_PENALTY
    for i in range(1, n + 1):
        s_tok = script_tokens[i - 1]
        for j in range(1, m + 1):
            a_tok = (asr_words[j - 1].get("word") or asr_words[j - 1].get("text") or "")
            match = _MATCH_SCORE if _token_match(s_tok, a_tok) else _MISMATCH_PENALTY
            score[i][j] = max(
                score[i - 1][j - 1] + match,
                score[i - 1][j] + _GAP_PENALTY,  # script gap (delete from script — won't actually happen, we keep all script)
                score[i][j - 1] + _GAP_PENALTY,  # asr gap (insertion in asr — drop ASR word)
            )

    # Traceback
    pairs: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            s_tok = script_tokens[i - 1]
            a_tok = (asr_words[j - 1].get("word") or asr_words[j - 1].get("text") or "")
            match = _MATCH_SCORE if _token_match(s_tok, a_tok) else _MISMATCH_PENALTY
            if score[i][j] == score[i - 1][j - 1] + match:
                pairs.append((i - 1, j - 1))
                i -= 1
                j -= 1
                continue
        if i > 0 and (j == 0 or score[i][j] == score[i - 1][j] + _GAP_PENALTY):
            pairs.append((i - 1, None))
            i -= 1
            continue
        # j > 0
        pairs.append((None, j - 1))
        j -= 1
    pairs.reverse()
    return pairs


def _align_nw(script_tokens: list[str], asr_words: list[dict], audio_dur: float) -> list[dict]:
    """NW-driven alignment. Every script token gets a (start, end) timestamp.

    - If a script token matches an ASR word: use the ASR word's actual times.
    - For runs of unmatched script tokens (between two anchor pairs):
      interpolate uniformly across the time span between anchor.end and the
      next anchor.start. Char-weighted within the run for sub-token granularity.
    """
    pairs = _nw_align_indices(script_tokens, asr_words)
    n_script = len(script_tokens)

    # Build a [script_idx -> matched_asr_word | None] map; track only matches.
    matched: dict[int, dict] = {}
    for i, j in pairs:
        if i is not None and j is not None:
            # Verify it was actually a match (NW also pairs mismatches at the
            # diagonal to consume both sides). Re-test with our match function.
            s_tok = script_tokens[i]
            a_tok = (asr_words[j].get("word") or asr_words[j].get("text") or "")
            if _token_match(s_tok, a_tok):
                matched[i] = asr_words[j]

    # Synthesize anchor list: virtual head/tail anchors for unmatched bookends.
    asr_start = asr_words[0]["start"] if asr_words else 0.0
    asr_end = asr_words[-1]["end"] if asr_words else max(audio_dur, 1.0)

    # Minimum per-word duration when interpolating into a degenerate run
    # (no anchors, or anchors collapsed). Keeps SRT chunks above the
    # assertions floor (0.15s) and prevents zero-width cues.
    MIN_INTERPOLATED_WORD_S = 0.20

    out: list[dict] = []
    # Whisper-1 sometimes reports start == end on very short words (e.g.
    # "the", "a", isolated punctuation like "Here,"). The 2-word SRT chunker
    # would then emit a 0-width cue. Floor matched-word duration to 160ms
    # so even a single-word chunk (punctuation-broken) clears the 150ms
    # assertion / readability threshold on its own. Whisper's actual timing
    # is preserved for the start; only end is extended when needed.
    MIN_MATCHED_WORD_S = 0.16

    # Walk script tokens; for each, either use the matched ASR timing or
    # interpolate from the surrounding matched anchors.
    for idx in range(n_script):
        if idx in matched:
            a = matched[idx]
            s = float(a["start"])
            e = float(a["end"])
            if e - s < MIN_MATCHED_WORD_S:
                e = s + MIN_MATCHED_WORD_S
            out.append({
                "text": script_tokens[idx],
                "start": round(s, 3),
                "end": round(e, 3),
                "type": "word",
            })
            continue

        # Find left + right anchors among matched indices.
        left_idx = next((k for k in range(idx - 1, -1, -1) if k in matched), None)
        right_idx = next((k for k in range(idx + 1, n_script) if k in matched), None)
        left_t = matched[left_idx]["end"] if left_idx is not None else asr_start
        right_t = matched[right_idx]["start"] if right_idx is not None else asr_end

        # Collect this run of unmatched indices between left_idx and right_idx.
        run_start = (left_idx + 1) if left_idx is not None else 0
        run_end = right_idx if right_idx is not None else n_script
        run_tokens = script_tokens[run_start:run_end]
        run_len = len(run_tokens)

        # Ensure the time span available for interpolation is at least
        # (run_len * MIN_INTERPOLATED_WORD_S). When anchors collapsed
        # (right_t ≤ left_t) or were too tight, expand right_t — clamped to
        # asr_end so we never run past the audio.
        min_span = run_len * MIN_INTERPOLATED_WORD_S
        span = max(0.0, right_t - left_t)
        if span < min_span:
            right_t = min(asr_end, left_t + min_span)
            span = right_t - left_t

        if span <= 0.0:
            # Truly nothing to fill (we're past asr_end). Pin the token at
            # left_t with a small positive width so SRT chunks stay healthy.
            start_t = left_t
            end_t = left_t + MIN_INTERPOLATED_WORD_S
        else:
            char_weights = [max(1, len(t)) for t in run_tokens]
            total_w = sum(char_weights)
            local_pos = idx - run_start
            cum_before = sum(char_weights[:local_pos])
            cum_after = cum_before + char_weights[local_pos]
            start_t = left_t + (cum_before / total_w) * span
            end_t = left_t + (cum_after / total_w) * span
            # Floor each token's width to avoid zero-duration cues on tiny spans.
            if (end_t - start_t) < (MIN_INTERPOLATED_WORD_S * 0.5):
                end_t = start_t + (MIN_INTERPOLATED_WORD_S * 0.5)

        out.append({
            "text": script_tokens[idx],
            "start": round(start_t, 3),
            "end": round(end_t, 3),
            "type": "word",
        })

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

    # Count NW match rate for observability — non-zero only when we hit the
    # mismatch path (i.e. NW ran). This is the load-bearing health signal:
    # high match rate = trustworthy timings; low match rate = lots of
    # interpolation = drift risk.
    nw_matches = 0
    if asr_words and len(script_tokens) != len(asr_words):
        # Re-derive matches via the alignment we just computed: each script
        # token whose end-start window equals an ASR word's window was a match.
        asr_windows = {(round(a["start"], 3), round(a["end"], 3)) for a in asr_words}
        nw_matches = sum(1 for w in aligned if (w["start"], w["end"]) in asr_windows)

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
            "nw_matches": nw_matches,
            "nw_match_rate": round(nw_matches / max(1, len(script_tokens)), 3),
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
        "nw_matches": nw_matches,
        "nw_match_rate": round(nw_matches / max(1, len(script_tokens)), 3),
        "json_path": str(out_json_path),
    }
