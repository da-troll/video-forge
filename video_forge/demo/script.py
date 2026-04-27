"""SCRIPT stage — drafts a product-story voiceover from project facts.

Inputs: metadata.json (the actual product story), README.md (background),
optional scene plan (timing context only).
Output: script.md with optional frontmatter + 80–110 word body
(~32–44s of voiceover at narrator pace).

Design principle: the PRODUCT is the subject. The scene plan is timing
context, not content — narration must NOT lift demo data ("Alpine Ridge
Weekend", "ClawDash" placeholder values, sample dates etc.) from scenes.
The user prompt explicitly separates PRODUCT block from SCENE PLAN block,
and the system prompt + worked examples reinforce the distinction.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from openai import OpenAI

from ..references import canonicalize_brand_terms, get_brand_voice_rules
from ..tts import get_openai_key

SCRIPT_MODEL = "gpt-5.4"

BASE_SYSTEM = """\
You are scripting a 25-second product reveal — like a household member
showing a friend what they just built. The voice is conversational,
slightly proud, never listy.

You will receive a USER prompt with two clearly-separated blocks:

  === PRODUCT ===   The actual subject. Name, what it is, what it does,
                    who it's for. THIS is what the script is about.

  === SCENE PLAN === Visual choreography only — the timing of clicks,
                    waits, scrolls in the recorded walkthrough. NEVER
                    lift content from this block. It exists so you know
                    roughly how long the visuals are. Demo data on
                    screen (sample trip names, placeholder brand inputs,
                    fake dates, dummy descriptions) is throw-away —
                    NEVER mention any of it.

Rules:
- Total length: 80–110 spoken words (~32–44s at narrator pace). Hard cap.
- Subject is the PRODUCT, not the demo data shown on screen.
- One continuous voiceover. No 3-act structure, no "today we're looking
  at...", no sign-off.
- Concrete and confident. NO marketing fluff, NO superlatives.
- Mention only features the PRODUCT block confirms — never invent.
- Prefer outcomes over actions. "Expenses settle themselves" beats
  "click Expenses to see who owes who".
- Pick 1-2 high-leverage moments to anchor the voice. The rest carries
  visually without commentary. The voice should make sense even if a
  viewer ignored the screen entirely.

Worked examples (study these — they show the difference between
literal screen-reading and product storytelling):

PRODUCT: "Trip Command Center — Palantir Foundry-style trip planning
dashboard for coordinating multi-group trips with convergence maps,
itinerary timelines, meal assignments, and expense settlement."
SCENE PLAN includes demo data: "Alpine Ridge Weekend", "Lake Tahoe", crews
named "SF / LA / Vegas".

  ❌ Bad (demo-data leak, screen reader):
     "In Trollefsen, the Alpine Ridge Weekend dashboard opens with trip
     details, dates, and destination. Groups shows the SF, LA, and
     Vegas crews. Routes maps each crew with dashed lines. Itinerary
     lays out each day. Meals assigns groups. Expenses calculates who
     owes who."

  ✅ Good (product story, demo data ignored):
     "Coordinating multi-crew trips used to mean six text threads.
     Trip Command Center pulls it into one Palantir-style canvas —
     colour-coded crews on a convergence map, meals planned without
     the math, expenses settling themselves. One canvas, no chaos."

Same word count, same features mentioned, but the product is the
subject — not the demo trip.

PRODUCT: "Mark Forge — household logo generator. Three modes (Product
logo, MVP cover, Agent avatar), dual-lane SVG + raster generation, palette
swap, one-click apply-to-MVP."
SCENE PLAN includes demo data: brand="ClawDash", vibe words "playful,
technical, confident, bold".

  ❌ Bad: "Type ClawDash into the brand field. Add playful, technical,
     confident, bold. Pick the blue-orange palette. Click generate."
  ✅ Good: "Mark Forge turns a brand name and a vibe into four logo
     concepts at once — two SVG, two raster, all from the same palette.
     Pick the one that feels right and apply it straight to a project.
     A whole identity pass in under a minute."

Output ONLY the script body — no headings, no scene labels, no markdown.
"""


# Demoted from a hard rule to a single line of timing context. The full
# system prompt above tells the LLM what NOT to do with scenes.
PACING_NOTE = (
    "\n\n(Visual beats below are timing context only — do not enumerate "
    "or describe them.)"
)


def _build_system() -> str:
    rules = get_brand_voice_rules()
    if not rules:
        return BASE_SYSTEM
    rules_block = "\n\nBrand voice rules (apply strictly):\n" + "\n".join(f"- {r}" for r in rules)
    return BASE_SYSTEM + rules_block


def _read_safely(path: Path, max_chars: int = 8000) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:max_chars]
    except Exception:
        return ""


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Lightweight `---key: val---` frontmatter parser. No yaml dep."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    head, body = m.groups()
    fm: dict = {}
    for line in head.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, body


def _format_scenes(scenes: list[dict]) -> str:
    """Compact timing-only summary of visual beats — selectors and demo
    text intentionally OMITTED so the LLM can't lift them into the script."""
    lines: list[str] = ["\n=== SCENE PLAN ==="]
    cumulative = 0.0
    for i, s in enumerate(scenes, 1):
        ms = (s.get("ms") or 0) + (s.get("ms_after") or 0)
        if ms == 0:
            ms = 1500
        dur_s = ms / 1000.0
        action = s.get("action", "wait")
        # Generic action label only — no scene name, no demo text, no selector.
        # Removes the leak surface for "Alpine Ridge Weekend"-style demo data.
        lines.append(f"  {i}. [{cumulative:5.1f}s + {dur_s:4.1f}s] {action}")
        cumulative += dur_s
    lines.append(f"Total planned visual length: ~{cumulative:.1f}s.")
    lines.append(PACING_NOTE.strip())
    return "\n".join(lines)


def _user_prompt(metadata: dict, readme: str, scenes: list[dict] | None = None) -> str:
    bits: list[str] = ["=== PRODUCT (this is what the script is about) ==="]
    bits.append(f"Name: {metadata.get('project_name') or metadata.get('name') or '(unknown)'}")
    if metadata.get("description"):
        bits.append(f"What it does: {metadata['description']}")
    if metadata.get("features"):
        bits.append("Key features:")
        for f in metadata["features"][:8]:
            bits.append(f"  - {f}")
    if metadata.get("tech_stack"):
        bits.append(f"Built with: {', '.join(metadata['tech_stack'][:8])}")
    if metadata.get("inspired_by"):
        bits.append(f"Inspired by: {metadata['inspired_by']}")
    if readme:
        bits.append("\nREADME excerpt (background context — same content rules: ignore demo data, narrate the product):")
        bits.append(readme[:2500])
    if scenes:
        bits.append(_format_scenes(scenes))
    bits.append("")
    bits.append("=== INSTRUCTION (nightly-MVP demo voiceover) ===")
    bits.append(
        "Write an 80–110 word voiceover for a nightly-MVP demo reel "
        "ABOUT THE PRODUCT.\n"
        "\n"
        "OPENING (mandatory pattern):\n"
        "  The first sentence MUST set the scene by naming the *problem* "
        "the product solves, then introduce the product by name in that "
        "same sentence or the immediately following sentence. Examples of "
        "valid openers:\n"
        "    \"Multi-crew trips used to mean six text threads. Trip Command "
        "Center pulls them into one canvas...\"\n"
        "    \"Generating four logo concepts at once usually means four "
        "tabs. Mark Forge runs all four in parallel from one prompt...\"\n"
        "  FORBIDDEN OPENERS: anything starting with \"In Trollefsen,...\", "
        "\"The Tollefsen household...\", \"Today's MVP from...\", or family "
        "naming. Never make the household the subject. The PRODUCT is the "
        "subject.\n"
        "\n"
        "CLOSING (mandatory): the last sentence must be a one-line "
        "why-it-matters — what changes for the user — NOT a sign-off and "
        "NOT a feature recap.\n"
        "\n"
        "CONTENT RULES:\n"
        "- Subject = the PRODUCT block above. Mention only features it "
        "confirms.\n"
        "- The SCENE PLAN block is visual timing context only. DO NOT "
        "lift content from it.\n"
        "- IGNORE demo data anywhere it appears (sample trip names, "
        "placeholder brand inputs, dummy dates, fake user names). The "
        "viewer can see demo data on screen; the voice must explain why "
        "someone would use this PRODUCT.\n"
        "- One continuous voiceover, conversational tone, no act labels.\n"
        "\n"
        "Output ONLY the script body — no headings, no labels, no markdown."
    )
    return "\n".join(bits)


# ── Anti-leakage assertion ─────────────────────────────────────────────
# Catch scripts that lifted demo data despite the prompt. Builds a set of
# "leaky tokens" from scene names, scene `text` values, and `note` content,
# then checks how many of those tokens appear in the script body. High
# overlap = the script is a screen reader.

_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "to", "of", "in", "on", "at",
    "for", "with", "by", "from", "as", "is", "are", "was", "be", "been",
    "this", "that", "it", "its", "into", "show", "shows", "click", "open",
    "view", "page", "section", "tab", "wait", "land", "scroll", "hover",
    "fill", "type", "select", "choose", "demo", "test", "sample",
}

_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")


def _extract_leak_tokens(scenes: list[dict]) -> set[str]:
    """Collect distinctive (non-stopword, ≥3-char) tokens from scene-level
    demo data. These are tokens the script should NOT contain.
    """
    if not scenes:
        return set()
    sources: list[str] = []
    for s in scenes:
        for k in ("name", "text", "note"):
            v = s.get(k)
            if isinstance(v, str):
                sources.append(v)
    blob = " ".join(sources).lower()
    tokens = {t for t in _TOKEN_RE.findall(blob) if t not in _STOPWORDS}
    return tokens


def _leakage_ratio(script_body: str, leak_tokens: set[str]) -> float:
    """Fraction of distinct ≥3-char script tokens that also appear in the
    leak set. Higher = more demo-data leakage. ~0.4+ is a screen reader.
    """
    if not leak_tokens:
        return 0.0
    body_tokens = {t for t in _TOKEN_RE.findall(script_body.lower()) if t not in _STOPWORDS}
    if not body_tokens:
        return 0.0
    overlap = body_tokens & leak_tokens
    return len(overlap) / len(body_tokens)


# Threshold tuned on the failed Trip Command Center run: that script had
# ~50% overlap (Trollefsen, alpine, ridge, weekend, groups, routes,
# itinerary, meals, expenses, mission, launch all from scene names).
LEAKAGE_THRESHOLD = 0.35


def draft_script(
    project_dir: Path,
    scenes: list[dict] | None = None,
) -> tuple[str, dict]:
    """Returns (body, frontmatter).

    Re-attempts once if the first draft has high demo-data leakage,
    appending an explicit anti-leakage instruction to the user prompt.
    """
    metadata_path = project_dir / "metadata.json"
    readme_path = project_dir / "README.md"
    script_path = project_dir / "edit" / "script.md"

    metadata: dict = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    readme = _read_safely(readme_path)

    existing_fm: dict = {}
    if script_path.exists():
        existing_fm, _ = _parse_frontmatter(script_path.read_text(encoding="utf-8"))

    leak_tokens = _extract_leak_tokens(scenes or [])
    client = OpenAI(api_key=get_openai_key())

    def _call(extra_user_instruction: str = "") -> str:
        user_prompt = _user_prompt(metadata, readme, scenes)
        if extra_user_instruction:
            user_prompt += "\n\n" + extra_user_instruction
        res = client.chat.completions.create(
            model=SCRIPT_MODEL,
            messages=[
                {"role": "system", "content": _build_system()},
                {"role": "user", "content": user_prompt},
            ],
        )
        return (res.choices[0].message.content or "").strip()

    body = _call()
    body = canonicalize_brand_terms(body)

    leakage = _leakage_ratio(body, leak_tokens)
    if leakage > LEAKAGE_THRESHOLD:
        # Retry once with a direct anti-leakage callout listing the actual
        # leaky tokens we detected. This is more effective than a generic
        # warning because the LLM sees specifically what to avoid.
        leaked = sorted(
            t for t in _TOKEN_RE.findall(body.lower())
            if t in leak_tokens and t not in _STOPWORDS
        )[:15]
        retry_instruction = (
            "Your previous draft leaked demo data into the narration. "
            "These specific words from the SCENE PLAN appeared in your "
            f"script and MUST NOT appear: {', '.join(leaked)}. "
            "Rewrite the voiceover focusing on the PRODUCT (what it does, "
            "why it exists) and reference the demo data NOT AT ALL. "
            "Same 80–110 word budget."
        )
        body = _call(extra_user_instruction=retry_instruction)
        body = canonicalize_brand_terms(body)
        # No second retry — if it still leaks, ship anyway and log the metric.

    return body, existing_fm


def write_script(project_dir: Path, body: str, frontmatter: dict | None = None) -> Path:
    edit_dir = project_dir / "edit"
    edit_dir.mkdir(parents=True, exist_ok=True)
    out = edit_dir / "script.md"
    parts: list[str] = []
    if frontmatter:
        parts.append("---")
        for k, v in frontmatter.items():
            parts.append(f"{k}: {v}")
        parts.append("---")
        parts.append("")
    parts.append(body)
    out.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    return out
