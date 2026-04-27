"""Scene planner — drafts a Playwright scene plan for a project's live URL.

Input: project_dir (with metadata.json + README.md) + live_url.
Process: 5s headless DOM probe (visible inputs/buttons/list-counts) + 1280x720
PNG screenshot → fed to gpt-5.4 with strict JSON schema.
Output: scenes.json at <project>/edit/scenes.json (atomic via tempfile).

The agent is instructed to prefer household product names (ClawDash, Podda,
Trollspace, Mark Forge, ClawPulse, Hermes) for any brand-name input — keeps
demos visibly part of the same ecosystem.

Failure modes (LLM 5xx, parse error, schema violation) → return None and
let the caller fall back to walkthrough._default_scene_plan.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from openai import OpenAI
from playwright.sync_api import sync_playwright

from ..tts import get_openai_key

log = logging.getLogger(__name__)

PLANNER_MODEL = "gpt-5.4"
SCHEMA_VERSION = 1

VALID_ACTIONS = {"goto", "wait", "fill", "click", "hover", "scroll", "screenshot"}

SYSTEM_PROMPT = (
    "You are a product video director. Given a project's metadata and a "
    "screenshot/DOM probe of its live URL, produce a Playwright scene plan "
    "that demonstrates the product's primary user journey in 30-60 seconds. "
    "Output strict JSON matching the provided schema.\n\n"
    "Hard rules:\n"
    "1. Selectors MUST come from the DOM probe — never invent selector "
    "strings. Prefer #id selectors; fall back to [data-testid], then "
    "class+nth-child as last resort.\n"
    "2. Do not click destructive actions (delete, sign out, payment, drop, "
    "remove).\n"
    "3. When the target page has a brand-name input, prefer one of the "
    "household products as the demo value (ClawDash, Podda, Trollspace, "
    "Mark Forge, ClawPulse, Hermes) — these are listed in "
    "brand-lexicon.yaml under 'products'. Fall back to a generic "
    "placeholder (e.g. 'Lighthouse') only if no household product fits "
    "the page's intended use case.\n"
    "4. Total estimated_duration_s should be 50-75. Each scene's ms_after "
    "is the quiet gap AFTER the action completes (used for visual settle "
    "and to wait for slow async operations like LLM calls or image "
    "generation).\n"
    "5. Open with a 2-3s 'land' wait scene. Close with a 'scroll' or "
    "'hover' on a key result, not abruptly mid-action.\n"
    "6. PACING — minimum dwell times (the demo must NOT feel rushed):\n"
    "   - Every 'wait' scene: ms ≥ 4000.\n"
    "   - Every action scene (click/hover/fill/scroll/screenshot): "
    "ms_after ≥ 3500.\n"
    "   - Total walkthrough should land at 50-75s. Use 12-18 scenes; the "
    "sum of all ms + ms_after across all scenes MUST equal the total. "
    "Do NOT rely on an inflated estimated_duration_s; the orchestrator "
    "validates by summing per-scene ms+ms_after.\n"
    "7. OBSERVATION BEATS — after EVERY action scene (click/fill/hover), "
    "insert a brief 'wait' scene with ms=2500 and a note describing what "
    "the viewer should NOTICE about the result. Skip only if the next "
    "action is on the same target element."
)

PLAN_SCHEMA = {
    "name": "scene_plan",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["version", "estimated_duration_s", "scenes"],
        "properties": {
            "version": {"type": "integer"},
            "estimated_duration_s": {"type": "number"},
            "scenes": {
                "type": "array",
                "minItems": 3,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    # OpenAI strict mode requires every property listed in required.
                    # Optional values are typed as nullable; the LLM emits null
                    # when the field doesn't apply. We strip nulls in _normalize_plan.
                    "required": ["name", "action", "selector", "text", "ms", "ms_after", "y", "note"],
                    "properties": {
                        "name": {"type": "string"},
                        "action": {
                            "type": "string",
                            "enum": ["wait", "fill", "click", "hover", "scroll", "screenshot"],
                        },
                        "selector": {"type": ["string", "null"]},
                        "text": {"type": ["string", "null"]},
                        "ms": {"type": ["integer", "null"]},
                        "ms_after": {"type": ["integer", "null"]},
                        "y": {"type": ["integer", "null"]},
                        "note": {"type": "string"},
                    },
                },
            },
        },
    },
}


def _probe_dom(live_url: str, screenshot_path: Path, timeout_ms: int = 12_000) -> dict:
    """Headless 5s probe: visible inputs, buttons, common containers, screenshot."""
    out: dict[str, Any] = {"url": live_url, "inputs": [], "buttons": [], "lists": {}, "title": ""}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()
        try:
            page.goto(live_url, timeout=timeout_ms, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=4_000)
            except Exception:
                pass
            out["title"] = (page.title() or "")[:120]

            for el in page.query_selector_all("input:not([type=hidden]), textarea, select"):
                try:
                    if not el.is_visible():
                        continue
                    out["inputs"].append({
                        "id": el.get_attribute("id"),
                        "name": el.get_attribute("name"),
                        "type": el.get_attribute("type") or el.evaluate("e=>e.tagName.toLowerCase()"),
                        "placeholder": el.get_attribute("placeholder"),
                        "data_testid": el.get_attribute("data-testid"),
                    })
                except Exception:
                    continue

            for el in page.query_selector_all("button, [role=button], a.btn, a.button"):
                try:
                    if not el.is_visible():
                        continue
                    label = (el.inner_text() or "").strip()[:60]
                    out["buttons"].append({
                        "id": el.get_attribute("id"),
                        "label": label,
                        "data_testid": el.get_attribute("data-testid"),
                        "classes": el.get_attribute("class"),
                    })
                except Exception:
                    continue

            for sel, key in [(".tile", "tiles"), (".card", "cards"), (".grid", "grids"), ("li", "list_items")]:
                try:
                    out["lists"][key] = page.locator(sel).count()
                except Exception:
                    pass

            page.screenshot(path=str(screenshot_path), full_page=False)
        finally:
            context.close()
            browser.close()
    return out


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


# Pacing floors — enforced regardless of what the LLM emits. See
# SYSTEM_PROMPT rule 6 for the full rationale: short dwells produce
# walkthroughs that race ahead of the narration. Belt + suspenders with
# the prompt instruction.
MIN_WAIT_MS = 4000
MIN_MS_AFTER_FOR_ACTIONS = 3500


def _enforce_pacing_floors(scene: dict) -> dict:
    """Lift any too-short dwell times to the configured floors."""
    action = scene.get("action")
    if action == "wait":
        ms = int(scene.get("ms") or 0)
        if ms < MIN_WAIT_MS:
            scene["ms"] = MIN_WAIT_MS
    elif action in ("click", "hover", "fill", "scroll", "screenshot"):
        ms_after = int(scene.get("ms_after") or 0)
        if ms_after < MIN_MS_AFTER_FOR_ACTIONS:
            scene["ms_after"] = MIN_MS_AFTER_FOR_ACTIONS
    return scene


def _normalize_plan(raw: dict, live_url: str) -> dict:
    """Tag the LLM output with metadata fields the schema doesn't enforce, drop bad scenes."""
    from datetime import datetime, timezone
    scenes_in = raw.get("scenes", [])
    scenes_out: list[dict] = []
    for s in scenes_in:
        if s.get("action") not in VALID_ACTIONS:
            continue
        # Drop null fields so the on-disk file is tidy, then enforce floors.
        clean = {k: v for k, v in s.items() if v is not None}
        scenes_out.append(_enforce_pacing_floors(clean))
    # Recompute estimated_duration_s as the PURE sum of scene dwells.
    # The LLM's self-reported estimated_duration_s is unreliable —
    # MemPalace 2026-04-27 shipped a plan claiming 57s while the actual
    # scene sums totalled 27.9s. We trust the scene sums, not the LLM's
    # number. (Typing/click latency adds ~3-5s on top, but downstream
    # consumers should treat this as a lower bound.)
    estimated = sum(
        ((s.get("ms") or 0) + (s.get("ms_after") or 0)) / 1000.0
        for s in scenes_out
    )
    return {
        "version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "live_url": live_url,
        "estimated_duration_s": round(estimated, 1),
        "scenes": scenes_out,
    }


def plan_scenes(
    project_dir: Path,
    live_url: str,
    *,
    max_total_seconds: int = 60,
    probe_dom: bool = True,
    out_path: Path | None = None,
) -> dict | None:
    """Draft a scene plan; persist to <project>/edit/scenes.json. Return the dict, or None on failure."""
    edit_dir = project_dir / "edit"
    edit_dir.mkdir(parents=True, exist_ok=True)
    scenes_path = out_path or (edit_dir / "scenes.json")

    metadata: dict = {}
    metadata_path = project_dir / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("metadata.json unreadable: %s", e)

    readme = ""
    readme_path = project_dir / "README.md"
    if readme_path.exists():
        try:
            readme = readme_path.read_text(encoding="utf-8")[:3000]
        except Exception:
            pass

    probe: dict = {}
    screenshot_path = edit_dir / "_scene_probe.png"
    if probe_dom:
        try:
            probe = _probe_dom(live_url, screenshot_path)
        except Exception as e:
            log.warning("DOM probe failed: %s — proceeding without selectors", e)
            probe = {"url": live_url, "error": str(e)}

    user_prompt = (
        f"PROJECT METADATA:\n{json.dumps(metadata, indent=2)[:2500]}\n\n"
        f"README EXCERPT:\n{readme}\n\n"
        f"DOM PROBE (live URL: {live_url}):\n{json.dumps(probe, indent=2)[:3000]}\n\n"
        f"Constraints:\n"
        f"- max_total_seconds = {max_total_seconds}\n"
        f"- estimated_duration_s should be in [25, {max_total_seconds}]\n"
        f"- Use ONLY selectors that appear in the DOM probe.\n"
        f"- Output JSON matching the scene_plan schema."
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    # If the screenshot exists, include it as a vision attachment on the user message.
    if screenshot_path.exists():
        try:
            b64 = base64.b64encode(screenshot_path.read_bytes()).decode("ascii")
            messages[-1] = {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        except Exception as e:
            log.warning("screenshot attach failed: %s", e)

    client = OpenAI(api_key=get_openai_key())
    try:
        res = client.chat.completions.create(
            model=PLANNER_MODEL,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": PLAN_SCHEMA},
        )
    except Exception as e:
        log.warning("scene-planner LLM call failed: %s", e)
        return None

    content = res.choices[0].message.content or ""
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as e:
        log.warning("scene-planner returned non-JSON: %s -- content=%s", e, content[:200])
        return None

    plan = _normalize_plan(raw, live_url)
    if not plan["scenes"]:
        log.warning("scene-planner produced 0 valid scenes")
        return None

    _atomic_write(scenes_path, json.dumps(plan, indent=2) + "\n")
    return plan
