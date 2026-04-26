"""Preflight — verify a live URL is healthy enough to render a demo.

Runs BEFORE any LLM or TTS call so we don't burn API cost on broken projects.

Checks (cheapest → most expensive):
  1. HTTP GET returns 2xx (timeout 10s, follows redirects)
  2. Playwright headless load at 1280x720, domcontentloaded fires within 10s
  3. At least one visible interactive element (button / input / a) — warn if none
  4. No console errors over a 5s observation window — warn if many

Returns a structured dict; raises PreflightError on hard failure (HTTP / load).
Soft failures (no buttons, console errors) become warnings in the returned dict.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests
from playwright.sync_api import sync_playwright


class PreflightError(RuntimeError):
    """Hard failure — orchestrator should abort before any LLM/TTS spend."""


@dataclass
class PreflightResult:
    ok: bool
    status_code: int | None = None
    load_ms: int | None = None
    title: str = ""
    interactive_count: int = 0
    console_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status_code": self.status_code,
            "load_ms": self.load_ms,
            "title": self.title,
            "interactive_count": self.interactive_count,
            "console_errors": self.console_errors[:8],  # cap for log size
            "warnings": self.warnings,
            "error": self.error,
        }


def _http_check(live_url: str) -> tuple[int, str | None]:
    try:
        r = requests.get(live_url, timeout=10, allow_redirects=True)
    except requests.RequestException as e:
        return 0, f"http error: {e}"
    if not (200 <= r.status_code < 300):
        return r.status_code, f"http {r.status_code}"
    return r.status_code, None


def _browser_check(live_url: str, observe_ms: int = 5000) -> tuple[int, str, int, list[str], str | None]:
    """Returns (load_ms, title, interactive_count, console_errors, hard_error_or_None)."""
    console_errors: list[str] = []
    started = time.time()
    title = ""
    interactive_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()

        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        try:
            page.goto(live_url, timeout=12_000, wait_until="domcontentloaded")
        except Exception as e:
            context.close()
            browser.close()
            return 0, "", 0, console_errors, f"page.goto failed: {e}"

        load_ms = int((time.time() - started) * 1000)

        # Best-effort networkidle (lots of SPAs never settle); ignore timeout.
        try:
            page.wait_for_load_state("networkidle", timeout=4_000)
        except Exception:
            pass

        title = (page.title() or "")[:120]

        try:
            interactive_count = page.evaluate(
                """() => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const cs = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
                    };
                    return [...document.querySelectorAll('button, input:not([type=hidden]), a, [role=button], textarea, select')]
                        .filter(visible).length;
                }"""
            ) or 0
        except Exception:
            interactive_count = 0

        # Observation window for any post-load console errors.
        page.wait_for_timeout(observe_ms)

        context.close()
        browser.close()

    return load_ms, title, interactive_count, console_errors, None


def preflight(
    live_url: str,
    *,
    observe_ms: int = 5000,
    project_hint: str | None = None,
) -> PreflightResult:
    """Verify live_url is healthy. Raises PreflightError on hard failure.

    project_hint: optional project_name. If supplied, we soft-warn when the page
    title contains none of the hint's words AND interactive_count is low — a
    common signal that we landed on a Caddy catch-all page rather than the
    actual project.
    """
    if not live_url or not live_url.startswith(("http://", "https://")):
        raise PreflightError(f"invalid live_url: {live_url!r}")

    res = PreflightResult(ok=False)

    # 1) HTTP check (cheap, fail fast)
    status, err = _http_check(live_url)
    res.status_code = status
    if err is not None:
        res.error = err
        raise PreflightError(err)

    # 2) Browser load
    load_ms, title, interactive_count, console_errors, hard_err = _browser_check(live_url, observe_ms=observe_ms)
    res.load_ms = load_ms
    res.title = title
    res.interactive_count = interactive_count
    res.console_errors = console_errors

    if hard_err is not None:
        res.error = hard_err
        raise PreflightError(hard_err)

    # 3) Soft signals → warnings
    if interactive_count == 0:
        res.warnings.append("no visible interactive elements (button/input/a) — walkthrough may have nothing to interact with")
    if len(console_errors) >= 5:
        res.warnings.append(f"{len(console_errors)} console errors observed (showing first 8 in log)")

    # 4) Identity check — Caddy catch-all on mvp.trollefsen.com returns 200 with
    # a default Trollefsen page for unknown slugs. If a project_hint is given
    # and the title doesn't contain ANY hint word AND the page has few
    # interactive elements, flag — likely a fallback, not the project.
    if project_hint and title:
        hint_words = {w.lower() for w in project_hint.split() if len(w) > 2}
        title_lc = title.lower()
        title_match = any(w in title_lc for w in hint_words)
        if not title_match and interactive_count < 3:
            res.warnings.append(
                f"page title {title!r} doesn't reference project hint {project_hint!r} "
                f"and only {interactive_count} interactive element(s) — possible Caddy fallback page"
            )

    res.ok = True
    return res
