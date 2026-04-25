"""WALKTHROUGH stage — Playwright records a scripted browser session.

Default plan: landing → wait → click first prominent button → wait → screenshot
focal area → end. The orchestrator can override the scene plan; this module
just executes it. Output: walkthrough.webm (Playwright native), then re-encoded
to walkthrough.mp4 by ffmpeg.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

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


def record(live_url: str, edit_dir: Path, *, max_seconds: int = 18) -> dict:
    """Record the live URL → edit/walkthrough.mp4. Returns scene metadata."""
    edit_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = edit_dir / "_raw"
    raw_dir.mkdir(exist_ok=True)
    scenes: list[Scene] = []
    start = 0.0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": VIDEO_W, "height": VIDEO_H},
            record_video_dir=str(raw_dir),
            record_video_size={"width": VIDEO_W, "height": VIDEO_H},
        )
        page = context.new_page()
        page.goto(live_url, timeout=20000)
        plan = _default_scene_plan(page)

        clock = 0.0
        for step in plan:
            action = step["action"]
            if clock >= max_seconds:
                break
            if action == "wait":
                ms = min(step["ms"], int((max_seconds - clock) * 1000))
                _wait_settle(page, ms)
                scenes.append(Scene(name=step.get("note", action), start_s=clock, end_s=clock + ms / 1000.0, note=step.get("note", "")))
                clock += ms / 1000.0
            elif action == "scroll_into":
                target = step["selector_handle"]
                try:
                    target.scroll_into_view_if_needed()
                except Exception:
                    pass
                _wait_settle(page, 500)
                clock += 0.5
            elif action == "click":
                target = step["selector_handle"]
                try:
                    target.click(timeout=5000)
                except Exception:
                    pass
                scenes.append(Scene(name=step.get("note", "click"), start_s=clock, end_s=clock + 0.3, note=step.get("note", "")))
                clock += 0.3
                _wait_settle(page, 500)
                clock += 0.5

        # Ensure we always reach a stable end frame.
        if clock < max_seconds:
            _wait_settle(page, int((max_seconds - clock) * 1000))

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

    scene_meta = {"duration_s": duration_s, "scenes": [asdict(s) for s in scenes]}
    (edit_dir / "walkthrough.scenes.json").write_text(json.dumps(scene_meta, indent=2))
    return scene_meta
