"""WALKTHROUGH stage — Playwright records a scripted browser session.

Scene-plan source order:
  1. Hand-authored override file passed via `scene_plan_override`.
  2. <project>/edit/scenes.json (left from a prior planner run; user may edit).
  3. video_forge.demo.scene_planner.plan_scenes() (LLM-driven, MVP-aware).
  4. _default_scene_plan() — generic CTA hunter, last resort.

Output: walkthrough.webm (Playwright native), re-encoded to walkthrough.mp4.

# TODO v1.1: support a 'wait_for' action with {selector, timeout_ms} for
# content that loads variably (e.g., LLM-driven UIs where 'tiles ready'
# takes 5-30s). For now ms_after is a hard timer to avoid Playwright
# wait_for_selector cliffs.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

log = logging.getLogger(__name__)

VIDEO_W, VIDEO_H = 1280, 720  # Downscale from 1920x1080 — kinder to a busy VPS, still HD.


@dataclass
class Scene:
    name: str
    start_s: float
    end_s: float
    note: str = ""


def _wait_settle(page: Page, ms: int = 1500) -> None:
    page.wait_for_timeout(ms)


def _default_scene_plan(page: Page) -> list[dict[str, Any]]:
    """Probe the live URL DOM and pick a sensible 3-scene plan.

    Scene 1: landing (3s of stillness on the rendered page)
    Scene 2: hover/click the first prominent CTA (a primary button or a
             button matching ['generate', 'go', 'play', 'start', 'try', 'begin']
             — falls back to the first non-disabled button if none match)
    Scene 3: rest on whatever the click revealed for 5s
    """
    # Wait for content
    page.wait_for_load_state("networkidle", timeout=15000)
    candidates = page.query_selector_all("button:not([disabled]), a.primary, a.btn-primary, [role='button']")
    target = None
    keywords = ("generate", "start", "go", "play", "try", "run", "begin", "build", "demo", "create", "preview")
    for c in candidates:
        try:
            label = (c.inner_text() or "").strip().lower()
        except Exception:
            label = ""
        if any(k in label for k in keywords):
            target = c
            break
    if target is None and candidates:
        target = candidates[0]

    plan = [{"action": "wait", "ms": 3000, "note": "landing"}]
    if target:
        plan.append({"action": "scroll_into", "selector_handle": target, "note": "reveal CTA"})
        plan.append({"action": "click", "selector_handle": target, "note": "primary CTA"})
        plan.append({"action": "wait", "ms": 6000, "note": "post-CTA result"})
    else:
        plan.append({"action": "wait", "ms": 6000, "note": "no CTA found, hold"})
    return plan


def _resolve_scene_plan(
    live_url: str,
    edit_dir: Path,
    *,
    scene_plan_override: Path | None,
    project_dir: Path | None,
    regen_scenes: bool,
) -> tuple[list[dict] | None, str]:
    """Pick a scene plan source. Returns (scenes_list, source_label) or (None, "default")."""
    if scene_plan_override and scene_plan_override.exists():
        try:
            data = json.loads(scene_plan_override.read_text(encoding="utf-8"))
            return data.get("scenes") or data, f"override:{scene_plan_override.name}"
        except Exception as e:
            log.warning("scene-plan override unreadable: %s", e)

    scenes_json = edit_dir / "scenes.json"
    if scenes_json.exists() and not regen_scenes:
        try:
            data = json.loads(scenes_json.read_text(encoding="utf-8"))
            return data.get("scenes") or data, "cached:edit/scenes.json"
        except Exception as e:
            log.warning("scenes.json unreadable: %s", e)

    if project_dir is not None:
        try:
            from .scene_planner import plan_scenes
            plan = plan_scenes(project_dir, live_url)
            if plan and plan.get("scenes"):
                return plan["scenes"], "scene_planner:llm"
        except Exception as e:
            log.warning("scene_planner.plan_scenes raised: %s", e)

    return None, "default"


def _execute_scene(page: Page, step: dict, scenes: list[Scene], clock: float) -> float:
    """Execute one scene from a scenes.json scene; returns the clock value after."""
    action = step.get("action", "wait")
    name = step.get("name") or step.get("note") or action
    note = step.get("note", "")

    started = clock
    ms_after = int(step.get("ms_after", 0) or 0)

    if action == "wait":
        ms = int(step.get("ms", step.get("ms_after", 1500)))
        page.wait_for_timeout(ms)
        clock += ms / 1000.0
    elif action == "fill":
        # Use press_sequentially (not .fill()) so the field is visibly typed
        # frame-by-frame during recording. .fill() sets the value atomically,
        # which makes the field appear pre-populated — no typing animation.
        # Clear first in case the field has any residual state, then focus + type.
        sel = step.get("selector")
        text = step.get("text", "")
        type_delay_ms = int(step.get("type_delay_ms", 55))  # ~18 chars/sec
        if sel:
            try:
                loc = page.locator(sel).first
                loc.fill("", timeout=5000)        # clear
                loc.click(timeout=3000)            # focus (caret visible)
                loc.press_sequentially(text, delay=type_delay_ms, timeout=15000)
            except Exception as e:
                log.warning("type('%s') failed: %s", sel, e)
        if ms_after:
            page.wait_for_timeout(ms_after)
            clock += ms_after / 1000.0
        else:
            # Settle pause after typing finishes.
            page.wait_for_timeout(600)
            clock += 0.6
        # press_sequentially blocks for len(text)*delay; account for it on the clock.
        if sel and text:
            typed_s = (len(text) * type_delay_ms) / 1000.0
            clock += typed_s
    elif action == "click":
        sel = step.get("selector")
        if sel:
            try:
                page.locator(sel).first.click(timeout=5000)
            except Exception as e:
                log.warning("click('%s') failed: %s", sel, e)
        if ms_after:
            page.wait_for_timeout(ms_after)
            clock += ms_after / 1000.0
        else:
            page.wait_for_timeout(1500)
            clock += 1.5
    elif action == "hover":
        sel = step.get("selector")
        if sel:
            try:
                page.locator(sel).first.hover(timeout=3000)
            except Exception as e:
                log.warning("hover('%s') failed: %s", sel, e)
        if ms_after:
            page.wait_for_timeout(ms_after)
            clock += ms_after / 1000.0
        else:
            page.wait_for_timeout(1200)
            clock += 1.2
    elif action == "scroll":
        if step.get("selector"):
            try:
                page.locator(step["selector"]).first.scroll_into_view_if_needed(timeout=3000)
            except Exception as e:
                log.warning("scroll('%s') failed: %s", step["selector"], e)
        else:
            y = int(step.get("y", 0))
            page.evaluate(f"window.scrollTo({{ top: {y}, behavior: 'smooth' }})")
        wait = ms_after or 1500
        page.wait_for_timeout(wait)
        clock += wait / 1000.0
    elif action == "screenshot":
        wait = ms_after or 400
        page.wait_for_timeout(wait)
        clock += wait / 1000.0
    elif action == "scroll_into":  # legacy inline-default plan
        h = step.get("selector_handle")
        if h is not None:
            try:
                h.scroll_into_view_if_needed()
            except Exception:
                pass
        page.wait_for_timeout(500)
        clock += 0.5
    else:
        log.warning("unknown action '%s' — skipping", action)

    scenes.append(Scene(name=name, start_s=started, end_s=clock, note=note))
    return clock


def record(
    live_url: str,
    edit_dir: Path,
    *,
    max_seconds: int = 60,
    project_dir: Path | None = None,
    scene_plan_override: Path | None = None,
    regen_scenes: bool = False,
) -> dict:
    """Record the live URL → edit/walkthrough.mp4. Returns scene metadata."""
    edit_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = edit_dir / "_raw"
    raw_dir.mkdir(exist_ok=True)
    scenes: list[Scene] = []

    plan, plan_source = _resolve_scene_plan(
        live_url, edit_dir,
        scene_plan_override=scene_plan_override,
        project_dir=project_dir,
        regen_scenes=regen_scenes,
    )
    log.info("walkthrough scene-plan source: %s", plan_source)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": VIDEO_W, "height": VIDEO_H},
            record_video_dir=str(raw_dir),
            record_video_size={"width": VIDEO_W, "height": VIDEO_H},
        )
        page = context.new_page()
        page.goto(live_url, timeout=20000)

        if plan is None:
            plan = _default_scene_plan(page)

        clock = 0.0
        for step in plan:
            if clock >= max_seconds:
                log.info("max_seconds=%s reached, truncating plan at scene '%s'", max_seconds, step.get("name"))
                break
            clock = _execute_scene(page, step, scenes, clock)

        # Stable end frame — pad to a clean second boundary if we finished early.
        tail_pad = max(0, int((min(max_seconds, clock + 1.0) - clock) * 1000))
        if tail_pad > 0:
            page.wait_for_timeout(tail_pad)
            clock += tail_pad / 1000.0

        page.close()
        context.close()
        browser.close()

    # Find the produced video file (Playwright names it deterministically — first webm in raw_dir).
    raw_videos = sorted(raw_dir.glob("*.webm"))
    if not raw_videos:
        raise RuntimeError("playwright produced no video")
    raw_video = raw_videos[0]

    out_mp4 = edit_dir / "walkthrough.mp4"
    # Re-encode with H.264 + AAC silence track so render.py / ffmpeg downstream
    # don't have to deal with VP8/VP9 + missing audio. -shortest caps the silent
    # track so audio doesn't extend past the video.
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(raw_video),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(out_mp4),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Probe duration so the orchestrator can pad/trim audio to fit.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(out_mp4)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    duration_s = float(probe.stdout.strip() or "0")

    # Clean up raw webm to save space.
    shutil.rmtree(raw_dir, ignore_errors=True)

    scene_meta = {
        "duration_s": duration_s,
        "plan_source": plan_source,
        "scenes": [asdict(s) for s in scenes],
    }
    (edit_dir / "walkthrough.scenes.json").write_text(json.dumps(scene_meta, indent=2))
    return scene_meta
