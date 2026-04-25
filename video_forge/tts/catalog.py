"""Aggregated voice catalog across providers."""

from __future__ import annotations

from .voices_openai import OPENAI_VOICES
from .voices_gemini import GEMINI_VOICES
from .voices_elevenlabs import ELEVENLABS_VOICES


def list_all_voices() -> dict[str, list[dict]]:
    """Return voices grouped by provider, matching the phase-2 UI shape."""
    return {
        "openai": list(OPENAI_VOICES),
        "gemini": list(GEMINI_VOICES),
        "elevenlabs": list(ELEVENLABS_VOICES),
    }


def find_voice(voice_id: str) -> dict | None:
    for v in OPENAI_VOICES + GEMINI_VOICES + ELEVENLABS_VOICES:
        if v["id"] == voice_id or v["name"].lower() == voice_id.lower():
            return v
    return None


def voice_provider(voice_id: str) -> str | None:
    v = find_voice(voice_id)
    return v["provider"] if v else None
