"""Multi-provider TTS for video-forge.

Key conventions mirror voice-palette's voice-palette/backend/main.py:

  - OpenAI TTS  → skills.apiKeys.openai_whisper
  - Gemini TTS  → skills.apiKeys.google_cloud_tts
  - ElevenLabs  → skills.apiKeys.elevenlabs

The unusual openai_whisper name is voice-palette's existing convention,
preserved here for billing alignment with the rest of the household.
"""

from __future__ import annotations

import json
from pathlib import Path

HOUSEHOLD_JSON = Path("/home/eve/config/household.json")


def _load_key(which: str) -> str:
    with open(HOUSEHOLD_JSON) as f:
        cfg = json.load(f)
    key = cfg.get("skills", {}).get("apiKeys", {}).get(which)
    if not key:
        raise RuntimeError(f"household.json: skills.apiKeys.{which} not found")
    return key


def get_openai_key() -> str:
    return _load_key("openai_whisper")


def get_gemini_key() -> str:
    return _load_key("google_cloud_tts")


def get_elevenlabs_key() -> str:
    return _load_key("elevenlabs")


def has_key(which: str) -> bool:
    try:
        _load_key(which)
        return True
    except RuntimeError:
        return False
