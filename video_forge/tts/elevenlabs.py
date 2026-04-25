"""ElevenLabs TTS adapter. Uses the v1 text-to-speech endpoint directly —
upstream's helpers don't include a synth call (only Scribe transcription),
so this is new.
"""

from __future__ import annotations

import logging

import requests

from . import get_elevenlabs_key
from ..references import get_pronunciation_hints
from .voices_elevenlabs import ELEVENLABS_VOICE_IDS, ELEVENLABS_VOICES

log = logging.getLogger(__name__)

DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel
DEFAULT_MODEL = "eleven_multilingual_v2"
TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"


def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    *,
    model: str = DEFAULT_MODEL,
    instructions: str | None = None,
    stability: float = 0.5,
    similarity_boost: float = 0.75,
) -> tuple[bytes, str]:
    """Synthesize text → mp3 bytes via ElevenLabs."""
    # Allow caller to pass a friendly name like "Rachel" — match against catalog.
    if voice not in ELEVENLABS_VOICE_IDS:
        match = next((v for v in ELEVENLABS_VOICES if v["name"].lower() == voice.lower()), None)
        if match is None:
            raise ValueError(f"unknown ElevenLabs voice: {voice}")
        voice = match["id"]

    payload: dict = {
        "text": text,
        "model_id": model,
        "voice_settings": {"stability": stability, "similarity_boost": similarity_boost},
    }
    # ElevenLabs v1 has no native `instructions` field on the synth endpoint —
    # the modern v3 API supports inline emotion tags but v1 does not. We
    # prepend any caller `instructions` as a parenthetical (best-effort).
    # Brand pronunciation hints from the lexicon are intentionally NOT
    # injected here — log a warning so callers know they're best-effort.
    hints = get_pronunciation_hints(text)
    if hints:
        log.info("brand pronunciation hints not injected for ElevenLabs v1 (best-effort only): %s", hints[:120])
    if instructions:
        payload["text"] = f"({instructions}) {text}"

    r = requests.post(
        f"{TTS_URL}/{voice}",
        headers={"xi-api-key": get_elevenlabs_key(), "accept": "audio/mpeg", "content-type": "application/json"},
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return r.content, "audio/mpeg"
