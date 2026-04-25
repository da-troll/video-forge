"""SCRIPT stage — agent drafts a 3-act voiceover script.md from project facts.

Inputs: metadata.json, README.md, optional source comments.
Output: script.md with optional frontmatter + 150–220 word body.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from openai import OpenAI

from ..tts import get_openai_key

SCRIPT_MODEL = "gpt-5.4"

SYSTEM = (
    "You are a senior product video scriptwriter. You write a single short "
    "voiceover script in three acts: HOOK (8–12 seconds), WALKTHROUGH "
    "(35–55 seconds), CLOSE (5–10 seconds). Total length must be 150–220 "
    "spoken words (~60–90 seconds at narrator pace). The voice is "
    "confident, plain, and concrete. NO marketing fluff. NO superlatives. "
    "Mention only features the source material confirms — never invent."
)


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


def _user_prompt(metadata: dict, readme: str) -> str:
    bits = [f"Project name: {metadata.get('project_name') or metadata.get('name') or '(unknown)'}"]
    if metadata.get("description"):
        bits.append(f"\nDescription: {metadata['description']}")
    if metadata.get("features"):
        bits.append("\nFeatures:")
        for f in metadata["features"][:8]:
            bits.append(f"  - {f}")
    if metadata.get("tech_stack"):
        bits.append(f"\nTech: {', '.join(metadata['tech_stack'][:8])}")
    if metadata.get("live_url"):
        bits.append(f"\nLive URL: {metadata['live_url']}")
    if readme:
        bits.append("\n\nREADME excerpt:\n" + readme[:3500])
    bits.append(
        "\n\nWrite the voiceover script. Output ONLY the script body — "
        "no headings, no scene labels, no markdown — just the words a "
        "narrator will read. 150 to 220 words. Three acts blend "
        "naturally; do not label them."
    )
    return "\n".join(bits)


def draft_script(project_dir: Path) -> tuple[str, dict]:
    """Returns (body, frontmatter). Frontmatter pulled from existing script.md if present."""
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

    # Reuse author-overridden frontmatter if a previous script.md is present.
    existing_fm: dict = {}
    if script_path.exists():
        existing_fm, _ = _parse_frontmatter(script_path.read_text(encoding="utf-8"))

    client = OpenAI(api_key=get_openai_key())
    res = client.chat.completions.create(
        model=SCRIPT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": _user_prompt(metadata, readme)},
        ],
    )
    body = (res.choices[0].message.content or "").strip()
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
