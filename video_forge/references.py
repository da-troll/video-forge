"""Brand lexicon loader. Single source: references/brand-lexicon.yaml.

Loaded once on first call and cached in-process. To reload, call
``reload_lexicon()``.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import yaml

LEXICON_PATH = Path(__file__).resolve().parent.parent / "references" / "brand-lexicon.yaml"

# Hard cap on the pronunciation hint string we add to TTS instructions.
# OpenAI gpt-4o-mini-tts caps `instructions` at 500 chars; leave headroom for
# any caller-supplied instructions appended on the front.
PRONUNCIATION_HINTS_MAX_CHARS = 400


@functools.lru_cache(maxsize=1)
def _cached_load() -> dict:
    if not LEXICON_PATH.exists():
        return {}
    with open(LEXICON_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def reload_lexicon() -> dict:
    _cached_load.cache_clear()
    return _cached_load()


def get_lexicon() -> dict:
    return _cached_load()


def get_brand_voice_rules() -> list[str]:
    return list(get_lexicon().get("brand_voice_rules") or [])


def get_pronunciation_hints(text: str | None = None) -> str:
    """Return a TTS-instructions-friendly pronunciation hint string.

    If `text` is given, only emit hints for tokens that appear in it (avoids
    bloating the instructions field with unused names). Otherwise return the
    full household-wide set.

    Output is capped at PRONUNCIATION_HINTS_MAX_CHARS; over-budget hints are
    truncated with a trailing marker so callers know clipping happened.
    """
    pron = get_lexicon().get("pronunciations") or {}
    if not pron:
        return ""

    if text:
        keys = [k for k in pron.keys() if k in text]
    else:
        keys = list(pron.keys())

    if not keys:
        return ""

    parts = [f"'{k}' as '{pron[k]}'" for k in keys]
    out = "Pronunciations: " + ". ".join(parts) + "."
    if len(out) > PRONUNCIATION_HINTS_MAX_CHARS:
        out = out[: PRONUNCIATION_HINTS_MAX_CHARS - 6].rstrip() + "[…]"
    return out


_QUOTED_RE = re.compile(r'"[^"]+"|\'[^\']+\'')


def canonicalize_brand_terms(text: str) -> str:
    """Apply lexicon canonicalization_rules with whole-word + quote-skipping safety."""
    rules = get_lexicon().get("canonicalization_rules") or []
    if not rules:
        return text

    # Collect protected (quoted) substrings + replace with placeholders so we
    # never edit inside a quoted proper noun like "Daniel Tollefsen".
    placeholders: list[str] = []

    def _stash(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"\x00QUO{len(placeholders) - 1}\x00"

    stashed = _QUOTED_RE.sub(_stash, text)

    for rule in rules:
        find = rule.get("find")
        replace = rule.get("replace")
        if not find or replace is None:
            continue
        # Whole-token boundary either side
        pattern = r"(?<!\w)" + re.escape(find) + r"(?!\w)"
        stashed = re.sub(pattern, replace, stashed)

    # Restore stashed
    def _unstash(m: re.Match) -> str:
        idx = int(m.group(1))
        return placeholders[idx]

    return re.sub(r"\x00QUO(\d+)\x00", _unstash, stashed)


def get_household_product_names() -> list[str]:
    products = get_lexicon().get("products") or []
    return [p["name"] for p in products if p.get("name")]
