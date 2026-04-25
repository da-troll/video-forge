# Attribution

This skill is **based on** [browser-use/video-use](https://github.com/browser-use/video-use) (4576★).

**Author:** browser-use organization
**Repo:** https://github.com/browser-use/video-use
**License:** ⚠️ **No LICENSE file in upstream as of clone date 2026-04-25.** README states "100% open source" but no formal license is attached. **Distribution is therefore deferred** — this fork is not pushed to GitHub until either (a) upstream adds a permissive license, or (b) Daniel makes an explicit attribution-only call.

## What's vendored verbatim

- `SKILL.md` — the 12 hard rules and skill description
- `helpers/transcribe.py`, `helpers/transcribe_batch.py`, `helpers/pack_transcripts.py`, `helpers/timeline_view.py`, `helpers/render.py`, `helpers/grade.py`
- `pyproject.toml`, `install.md`, `poster.html`, `static/`, `skills/manim-video`

## What's added by the household (not in upstream)

All household-specific code lives in **new files only** — upstream files are not edited:

- `video_forge/demo/` — nightly-MVP demo orchestrator (Layer B)
- `video_forge/tts/` — multi-provider TTS (Layer B step 2), ported from voice-palette
- `video_forge/api.py` — JSON in/out seams for phase-2 UI (Layer C)
- `video_forge/observability.py` — `pipeline.log.json` per run + `--gantt` (Layer C)
- `docs/PHASE2_UI.md` — phase-2 ClawDash drop-zone UI surface spec
- `ATTRIBUTION.md` — this file

## When upstream drifts

Pull strategy: `git fetch upstream && git merge upstream/main`. Conflicts should only ever appear in upstream files (which we don't edit), so a `git merge --strategy-option=theirs upstream/main` is the safe path if upstream changes anything we care about.

## Dependencies

- **Upstream** deps live in `pyproject.toml` (`requests`, `librosa`, `matplotlib`, `pillow`, `numpy`). **Never edit this file** — installed via `uv sync`.
- **Household** deps live in `household-requirements.txt` (`openai`, `google-genai`, `playwright`). Installed *separately* via `uv pip install -r household-requirements.txt`. If you're hunting for where `from openai import OpenAI` resolves, look there — not in `pyproject.toml`.

## OpenAI / Gemini TTS code

The `video_forge/tts/` module reuses logic from the household's voice-palette MVP:

- Source: `/home/eve/projects/nightly-mvps/2026-04-14-voice-palette/backend/main.py`
- License: voice-palette is a household-internal MVP (no public license, internal use)

OpenAI/Gemini voice catalogs (`OPENAI_VOICES`, `GEMINI_VOICES`) and SQLite profile CRUD (`tts/profiles.py`) are lifted verbatim with FastAPI decorators stripped.
