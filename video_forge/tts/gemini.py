"""Gemini TTS adapter. Lifted from voice-palette/backend/main.py:_gemini_tts,
with FastAPI/StreamingResponse stripped. Returns WAV bytes (PCM wrapped).
"""

from __future__ import annotations

import io
import wave

from . import get_gemini_key
from ..references import get_pronunciation_hints
from .voices_gemini import GEMINI_MODEL_MAP, GEMINI_VOICE_IDS

DEFAULT_MODEL = "gemini-2.5-flash-tts"


def synthesize(
    text: str,
    voice: str = "Kore",
    *,
    model: str = DEFAULT_MODEL,
    instructions: str | None = None,
    audio_tags: str | None = None,
) -> tuple[bytes, str]:
    """Synthesize text → wav bytes.

    Gemini accepts `instructions` baked into the prompt body (no separate
    field). voice-palette's pattern: prepend `audio_tags: ` if provided.
    """
    if voice not in GEMINI_VOICE_IDS:
        raise ValueError(f"unknown Gemini voice: {voice}")
    if model not in GEMINI_MODEL_MAP:
        raise ValueError(f"unknown Gemini model: {model}")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=get_gemini_key())

    prompt_text = text
    prefix_parts = []
    if audio_tags:
        prefix_parts.append(audio_tags)
    if instructions:
        prefix_parts.append(instructions)
    hints = get_pronunciation_hints(text)
    if hints:
        prefix_parts.append(hints)
    if prefix_parts:
        prompt_text = f"{'. '.join(prefix_parts)}: {text}"

    response = client.models.generate_content(
        model=GEMINI_MODEL_MAP[model],
        contents=prompt_text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice,
                    )
                ),
            ),
        ),
    )

    pcm = response.candidates[0].content.parts[0].inline_data.data
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm)
    return buf.getvalue(), "audio/wav"
