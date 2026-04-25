"""Multi-provider synth with config-driven fallback.

The fallback chain is read from household.json on EVERY call. Daniel
flips order at runtime, so we never cache.
"""

from __future__ import annotations

import importlib
import logging
from typing import Optional

from ..config import default_instructions, default_voice, fallback_chain
from . import has_key
from .catalog import find_voice
from .profiles import get_profile

log = logging.getLogger(__name__)

PROVIDER_KEY_NAME = {
    "openai": "openai_whisper",
    "gemini": "google_cloud_tts",
    "elevenlabs": "elevenlabs",
}

# Each provider's default voice when the caller didn't pin one.
PROVIDER_DEFAULT_VOICE = {
    "openai": "alloy",
    "gemini": "Kore",
    "elevenlabs": "21m00Tcm4TlvDq8ikWAM",
}


def _adapter_for(provider: str):
    """Lazy-import the adapter so an unconfigured provider doesn't blow up at import time."""
    return importlib.import_module(f".{provider}", package=__package__)


def _resolve_voice_or_profile(value: str | None) -> tuple[str, str | None, str | None]:
    """Resolve a value that may be a raw voice_id OR a profile_id.

    Returns (provider_hint, voice_id, instructions). provider_hint is "" if
    no hint can be derived from the value alone.
    """
    if not value:
        return "", None, None
    profile = get_profile(value)
    if profile:
        return profile["provider"], profile["voice_id"], profile.get("instructions")
    catalog_hit = find_voice(value)
    if catalog_hit:
        return catalog_hit["provider"], catalog_hit["id"], None
    return "", value, None


def synthesize_with_fallback(
    text: str,
    voice: Optional[str] = None,
    instructions: Optional[str] = None,
    chain: Optional[list[str]] = None,
) -> tuple[bytes, str, str, list[dict]]:
    """Synthesize text to audio, walking the fallback chain on failure.

    `voice` may be a raw voice_id OR a profile_id; resolved at call time.

    Returns (audio_bytes, mime, provider_used, fallback_log).

    fallback_log is a list of {provider, ok, error?} entries — also written
    by callers to pipeline.log.json by observability.py.
    """
    chain = chain or fallback_chain()
    if not chain:
        raise RuntimeError("fallback_chain is empty — fix household.json")

    hinted_provider, resolved_voice, resolved_instructions = _resolve_voice_or_profile(voice or default_voice())
    instructions = instructions or resolved_instructions or default_instructions()

    # If the resolved voice belongs to a specific provider, try that first
    # regardless of chain order — but still walk the rest of the chain on failure.
    walk_order: list[str] = []
    if hinted_provider:
        walk_order.append(hinted_provider)
    for p in chain:
        if p not in walk_order:
            walk_order.append(p)

    log_entries: list[dict] = []

    for provider in walk_order:
        key_name = PROVIDER_KEY_NAME.get(provider)
        if not key_name or not has_key(key_name):
            log_entries.append({"provider": provider, "ok": False, "error": "no api key"})
            continue
        try:
            adapter = _adapter_for(provider)
        except Exception as e:
            log_entries.append({"provider": provider, "ok": False, "error": f"adapter import: {e}"})
            continue

        # Pick voice for this provider. If the resolved voice doesn't belong
        # to this provider, fall back to the provider's default voice.
        voice_for_provider = resolved_voice
        if voice_for_provider:
            v = find_voice(voice_for_provider)
            if v and v["provider"] != provider:
                voice_for_provider = PROVIDER_DEFAULT_VOICE[provider]
        else:
            voice_for_provider = PROVIDER_DEFAULT_VOICE[provider]

        try:
            audio, mime = adapter.synthesize(
                text,
                voice=voice_for_provider,
                instructions=instructions,
            )
            log_entries.append({"provider": provider, "ok": True, "voice": voice_for_provider, "bytes": len(audio)})
            return audio, mime, provider, log_entries
        except Exception as e:
            log.warning("provider %s failed: %s", provider, e)
            log_entries.append({"provider": provider, "ok": False, "error": str(e)})
            continue

    raise RuntimeError(f"all providers failed: {log_entries}")
