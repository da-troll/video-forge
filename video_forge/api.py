"""Pure JSON in/out seams for phase-2 ClawDash UI.

Tonight: minimal but stable surface so phase-2 work can wire against
real functions. No interactive prompts, no UI.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .config import default_voice
from .tts import has_key
from .tts.catalog import list_all_voices
from .tts.profiles import (
    create_profile,
    delete_profile,
    get_profile,
    list_profiles,
    update_profile,
)
from .tts.synth import synthesize_with_fallback

# Content-hash cache for preview_voice — keyed by (provider, voice_id, instructions, sample_text).
_PREVIEW_CACHE_DIR = Path.home() / ".cache" / "video-forge" / "previews"


def list_voices() -> dict[str, list[dict]]:
    """All voices, grouped by provider, with `available` based on key presence."""
    catalog = list_all_voices()
    available_providers = {
        "openai": has_key("openai_whisper"),
        "gemini": has_key("google_cloud_tts"),
        "elevenlabs": has_key("elevenlabs"),
    }
    out: dict[str, list[dict]] = {}
    for provider, voices in catalog.items():
        out[provider] = []
        for v in voices:
            entry = dict(v)
            entry["available"] = available_providers.get(provider, False)
            entry["preview_url"] = None  # phase 2 streams via /api/preview
            out[provider].append(entry)
    return out


def preview_voice(provider: str, voice_id: str, instructions: str | None, sample_text: str) -> bytes:
    """Render a short preview clip; cache by content hash."""
    if not sample_text or len(sample_text) > 400:
        raise ValueError("sample_text must be 1..400 chars")
    h = hashlib.sha256(f"{provider}|{voice_id}|{instructions or ''}|{sample_text}".encode()).hexdigest()[:16]
    _PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _PREVIEW_CACHE_DIR / f"{h}.bin"
    if cached.exists():
        return cached.read_bytes()
    audio, _mime, _used, _log = synthesize_with_fallback(
        sample_text,
        voice=voice_id,
        instructions=instructions,
        chain=[provider],
    )
    cached.write_bytes(audio)
    return audio


def list_profiles_api() -> list[dict]:
    return list_profiles()


def get_profile_api(profile_id: str) -> dict | None:
    return get_profile(profile_id)


def save_profile_api(name: str, provider: str, voice_id: str, instructions: str | None = None) -> dict:
    return create_profile(name, provider, voice_id, instructions)


def delete_profile_api(profile_id: str) -> dict:
    ok = delete_profile(profile_id)
    return {"ok": ok}


def generate_demo(project_dir: Path | str, options: dict[str, Any] | None = None) -> dict:
    """Phase-2 entry point. Tonight: thin wrapper that delegates to the
    demo orchestrator, so the surface is stable even before the UI exists.
    """
    from .demo.orchestrator import run as run_demo

    options = options or {}
    return run_demo(Path(project_dir), options)
