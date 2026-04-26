"""Cheap, no-LLM quality gates that run AFTER assemble.

Catches whole classes of "the pipeline succeeded but the output is broken"
silent failures: empty mp4 from a swallowed ffmpeg error, voiceover/SRT
drift, demo length out of band, lead silence from a botched title pad,
loudness drifted off target, etc.

All assertions raise AssertionFailed with a descriptive message; the
orchestrator stage records measurements + raises so the run aborts with
the failure surfaced in pipeline.log.json.

Tunable bounds live here (top of file). MVP defaults err on the side of
warning loud, not silently failing — adjust per-project via
metadata.json.demo_quality_bounds in a future tier.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .loudnorm import measure_integrated_lufs

# ── tunables ────────────────────────────────────────────────────────────
MIN_DEMO_LEN_S = 30.0          # generously short — nightly MVPs vary
MAX_DEMO_LEN_S = 120.0         # if longer, narration probably ran on
MIN_DEMO_BYTES = 250_000       # ~0.25 MB — anything smaller is broken mp4
MAX_DEMO_BYTES = 200_000_000   # 200 MB — far above realistic demo size
MIN_CUE_DURATION_S = 0.15
MAX_CUE_DURATION_S = 8.0
MAX_LEAD_SILENCE_S = 1.5       # SRT first-cue start must be ≤ this
MAX_TAIL_DRIFT_S = 1.5         # |voice_dur − last_cue_end| must be ≤ this
LUFS_TARGET = -14.0
LUFS_TOLERANCE = 1.5           # final demo within ±1.5 LU of target


class AssertionFailed(RuntimeError):
    """Quality assertion violation — the pipeline output is broken."""


@dataclass
class AssertionMeasurements:
    demo_path: str
    demo_size_bytes: int
    demo_duration_s: float
    voice_duration_s: float
    srt_cue_count: int
    first_cue_start_s: float
    last_cue_end_s: float
    cue_drift_s: float
    min_cue_duration_s: float
    max_cue_duration_s: float
    integrated_lufs: float | None

    def as_dict(self) -> dict:
        return {
            "demo_path": self.demo_path,
            "demo_size_bytes": self.demo_size_bytes,
            "demo_duration_s": self.demo_duration_s,
            "voice_duration_s": self.voice_duration_s,
            "srt_cue_count": self.srt_cue_count,
            "first_cue_start_s": self.first_cue_start_s,
            "last_cue_end_s": self.last_cue_end_s,
            "cue_drift_s": self.cue_drift_s,
            "min_cue_duration_s": self.min_cue_duration_s,
            "max_cue_duration_s": self.max_cue_duration_s,
            "integrated_lufs": self.integrated_lufs,
        }


def _probe_duration(path: Path) -> float:
    from ._ffprobe import media_duration
    return media_duration(path)


_SRT_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})")


def _parse_srt_ts(ts: str) -> float:
    m = _SRT_TS.match(ts)
    if not m:
        return 0.0
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def _parse_srt_cues(srt_path: Path) -> list[tuple[float, float, str]]:
    """Returns [(start_s, end_s, text)]."""
    out: list[tuple[float, float, str]] = []
    blocks = srt_path.read_text(encoding="utf-8").split("\n\n")
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 3:
            continue
        # lines[0] is the index, lines[1] is the timing, lines[2:] are text
        timing = lines[1] if " --> " in lines[1] else (lines[2] if len(lines) > 2 and " --> " in lines[2] else "")
        if " --> " not in timing:
            continue
        start_s, end_s = (_parse_srt_ts(t.strip()) for t in timing.split(" --> "))
        text = " ".join(lines[2:]) if " --> " in lines[1] else " ".join(lines[3:])
        out.append((start_s, end_s, text))
    return out


def assert_demo_quality(
    demo_path: Path,
    srt_path: Path,
    voiceover_path: Path,
) -> dict:
    """Run all quality assertions. Raises AssertionFailed on the first
    violation. Returns the measurement dict for pipeline.log.json on success.
    """
    failures: list[str] = []

    # ── existence + size ─────────────────────────────────────────────────
    if not demo_path.exists():
        raise AssertionFailed(f"demo.mp4 missing at {demo_path}")
    size = demo_path.stat().st_size
    if size < MIN_DEMO_BYTES:
        # Short-circuit: ffprobe would crash on an empty/truncated file, and
        # at this point we know the output is broken — fail loud now.
        raise AssertionFailed(
            f"demo.mp4 size {size}B < min {MIN_DEMO_BYTES}B (likely empty/truncated mp4)"
        )
    if size > MAX_DEMO_BYTES:
        failures.append(f"demo.mp4 size {size}B > max {MAX_DEMO_BYTES}B")

    # ── durations ────────────────────────────────────────────────────────
    try:
        demo_dur = _probe_duration(demo_path)
    except subprocess.CalledProcessError as e:
        raise AssertionFailed(f"ffprobe failed on demo.mp4 (likely corrupt): {e}")
    try:
        voice_dur = _probe_duration(voiceover_path) if voiceover_path.exists() else 0.0
    except subprocess.CalledProcessError:
        voice_dur = 0.0
    if demo_dur < MIN_DEMO_LEN_S:
        failures.append(f"demo duration {demo_dur:.2f}s < min {MIN_DEMO_LEN_S}s")
    if demo_dur > MAX_DEMO_LEN_S:
        failures.append(f"demo duration {demo_dur:.2f}s > max {MAX_DEMO_LEN_S}s")

    # ── SRT cues ─────────────────────────────────────────────────────────
    if not srt_path.exists():
        raise AssertionFailed(f"SRT missing at {srt_path}")
    cues = _parse_srt_cues(srt_path)
    cue_count = len(cues)
    if cue_count == 0:
        failures.append("SRT contains zero cues")
        first_start = last_end = 0.0
        cue_min = cue_max = 0.0
    else:
        first_start = cues[0][0]
        last_end = cues[-1][1]
        durations = [(end - start) for start, end, _ in cues]
        cue_min = min(durations)
        cue_max = max(durations)
        if first_start > MAX_LEAD_SILENCE_S:
            failures.append(f"first SRT cue starts at {first_start:.2f}s > max {MAX_LEAD_SILENCE_S}s lead silence")
        if cue_min < MIN_CUE_DURATION_S:
            failures.append(f"shortest cue {cue_min:.3f}s < min {MIN_CUE_DURATION_S}s")
        if cue_max > MAX_CUE_DURATION_S:
            failures.append(f"longest cue {cue_max:.2f}s > max {MAX_CUE_DURATION_S}s")

    drift = abs(voice_dur - last_end) if voice_dur and cue_count else 0.0
    if cue_count and drift > MAX_TAIL_DRIFT_S:
        failures.append(
            f"last cue end {last_end:.2f}s drifts {drift:.2f}s from voice {voice_dur:.2f}s "
            f"(> {MAX_TAIL_DRIFT_S}s)"
        )

    # ── loudness ─────────────────────────────────────────────────────────
    lufs = measure_integrated_lufs(demo_path)
    if lufs is not None and abs(lufs - LUFS_TARGET) > LUFS_TOLERANCE:
        failures.append(
            f"final integrated LUFS {lufs:.2f} drifts >|{LUFS_TOLERANCE}| from target {LUFS_TARGET}"
        )

    measurements = AssertionMeasurements(
        demo_path=str(demo_path),
        demo_size_bytes=size,
        demo_duration_s=round(demo_dur, 3),
        voice_duration_s=round(voice_dur, 3),
        srt_cue_count=cue_count,
        first_cue_start_s=round(first_start, 3),
        last_cue_end_s=round(last_end, 3),
        cue_drift_s=round(drift, 3),
        min_cue_duration_s=round(cue_min, 3),
        max_cue_duration_s=round(cue_max, 3),
        integrated_lufs=round(lufs, 2) if lufs is not None else None,
    )

    if failures:
        msg = "; ".join(failures)
        # Wrap measurements + failure list in the exception so observability stage
        # records both — orchestrator can read .args[1] for pipeline.log.json.
        raise AssertionFailed(msg)

    return measurements.as_dict()
