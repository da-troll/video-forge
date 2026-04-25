"""OpenAI TTS adapter. Lifted from voice-palette/backend/main.py:_openai_tts,
with FastAPI/StreamingResponse stripped — pure Python: bytes in, bytes out.
"""

from __future__ import annotations

from openai import OpenAI

from . import get_openai_key
from ..references import get_pronunciation_hints
from .voices_openai import OPENAI_VOICE_IDS

# Default model — gpt-4o-mini-tts is the only model that accepts free-form
# `instructions` for style/tone. Cheaper and sounds clean for narration.
DEFAULT_MODEL = "gpt-4o-mini-tts"


def synthesize(
    text: str,
    voice: str = "alloy",
    *,
    model: str = DEFAULT_MODEL,
    speed: float = 1.0,
    instructions: str | None = None,
) -> tuple[bytes, str]:
    """Synthesize text → mp3 bytes.

    Returns (audio_bytes, mime).
    """
    if voice not in OPENAI_VOICE_IDS:
        raise ValueError(f"unknown OpenAI voice: {voice}")
    client = OpenAI(api_key=get_openai_key())
    kwargs: dict = dict(
        model=model,
        voice=voice,
        input=text,
        speed=speed,
        response_format="mp3",
    )
    # Append text-aware pronunciation hints for branded tokens (capped at 400 chars).
    hints = get_pronunciation_hints(text)
    merged_instructions = "\n".join(p for p in [instructions, hints] if p) or None
    if merged_instructions and model == "gpt-4o-mini-tts":
        kwargs["instructions"] = merged_instructions
    response = client.audio.speech.create(**kwargs)
    audio = b"".join(response.iter_bytes(chunk_size=4096))
    return audio, "audio/mpeg"


def synthesize_with_instructions(
    text: str,
    voice: str = "alloy",
    instructions: str | None = None,
    speed: float = 1.0,
) -> tuple[bytes, str]:
    """Convenience wrapper that locks in gpt-4o-mini-tts (the model that supports instructions)."""
    return synthesize(text, voice=voice, model="gpt-4o-mini-tts", speed=speed, instructions=instructions)
