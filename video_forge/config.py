"""Single source of truth: ~/config/household.json → skills.video_forge.

Read at runtime per call. NEVER cache the fallback chain — Daniel will
flip provider order at runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

HOUSEHOLD_JSON = Path("/home/eve/config/household.json")


def _load() -> dict:
    with open(HOUSEHOLD_JSON) as f:
        return json.load(f)


def video_forge_config() -> dict:
    cfg = _load().get("skills", {}).get("video_forge")
    if not cfg:
        raise RuntimeError("household.json: skills.video_forge block missing")
    return cfg


def fallback_chain() -> list[str]:
    return list(video_forge_config().get("fallback_chain", ["openai", "gemini", "elevenlabs"]))


def transcription_fallback_chain() -> list[str]:
    return list(video_forge_config().get("transcription_fallback_chain", ["elevenlabs", "openai", "gemini"]))


def default_provider() -> str:
    return video_forge_config().get("default_provider", "openai")


def default_voice() -> str:
    return video_forge_config().get("default_voice", "alloy")


def default_speed() -> float:
    """TTS speed multiplier, applied at synth time.

    Default 1.15 (mild speed-up) — empirically tightens local sync between
    visual scenes and narration without sounding rushed. Override in
    household.json under skills.video_forge.default_speed if 1.0 (natural)
    or 1.25 (faster) suits a particular project better. Range: 0.25-4.0
    per OpenAI TTS spec.
    """
    val = video_forge_config().get("default_speed", 1.15)
    try:
        speed = float(val)
    except (TypeError, ValueError):
        return 1.15
    return max(0.25, min(4.0, speed))


def default_instructions() -> str | None:
    return video_forge_config().get("default_instructions")


def tail_strategy() -> str:
    val = video_forge_config().get("tail_strategy", "hold")
    if val not in {"hold", "loop", "trim_voice"}:
        return "hold"
    return val
