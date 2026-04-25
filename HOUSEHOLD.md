# video-forge — Household Additions

This file is the household-side entry point. The upstream `README.md` is a
verbatim mirror of [browser-use/video-use](https://github.com/browser-use/video-use)
and is never edited.

> **License notice:** upstream has no LICENSE file as of 2026-04-25.
> This fork is **not pushed publicly** until that's resolved with Daniel.
> See `ATTRIBUTION.md`.

## What's added on top of upstream

```
ATTRIBUTION.md                  ← upstream attribution + license flag
HOUSEHOLD.md                    ← this file
household-requirements.txt      ← household deps (NOT in pyproject.toml)
docs/PHASE2_UI.md               ← phase-2 ClawDash UI surface spec
video_forge/                    ← all household code lives here
  config.py                     ← ~/config/household.json → skills.video_forge
  observability.py              ← pipeline.log.json + Mermaid Gantt
  api.py                        ← JSON in/out seams for phase-2 UI
  tts/
    voices_openai.py            ← 13 voices (verbatim from voice-palette MVP)
    voices_gemini.py            ← 30 voices (verbatim from voice-palette MVP)
    voices_elevenlabs.py        ←  2 defaults — TODO phase 2 for full catalog
    openai.py                   ← gpt-4o-mini-tts adapter
    gemini.py                   ← Gemini 2.5 / 3.1 TTS adapter
    elevenlabs.py               ← ElevenLabs v1 TTS adapter
    catalog.py                  ← aggregated voice catalog
    profiles.py                 ← SQLite at ~/.local/share/video-forge/profiles.db
    synth.py                    ← config-driven multi-provider fallback walker
  demo/
    orchestrator.py             ← `python -m video_forge.demo --project <dir>`
    script.py                   ← gpt-5.4 drafts a 60–90s voiceover script
    walkthrough.py              ← Playwright records 1280×720 walkthrough
    assemble.py                 ← ffmpeg mux: walkthrough + voiceover + SRT
                                  (subtitles applied LAST per upstream Rule 1)
```

Upstream files (`SKILL.md`, `helpers/*`, `pyproject.toml`, `README.md`,
`install.md`, `poster.html`, `static/`, `skills/`) are **never modified**.

## Install

```bash
cd /opt/apps/video-forge
uv sync                                       # upstream deps (pyproject.toml)
uv pip install -r household-requirements.txt  # household deps
uv run playwright install chromium            # for walkthrough recorder
```

## Usage

### Generate a demo for any nightly-MVP project

```bash
uv run python -m video_forge.demo \
  --project ~/projects/nightly-mvps/2026-04-24-logo-generator
```

Outputs:
- `<project>/edit/demo.mp4`
- `~/workspaces/shared/images/mvps/<slug>/demo.mp4`
- Public URL: `https://clawdash.trollefsen.com/media/mvps/<slug>/demo.mp4`
- `metadata.json` extended with `demo_video_url`
- `<project>/edit/pipeline.log.json` (per-stage timings, fallback chains, errors)

Add `--gantt` to render a Mermaid Gantt at the end.

### Override voice / instructions / live URL

```bash
--voice <voice_id_or_profile_id>    # e.g. "alloy", "Kore", or a saved profile UUID
--instructions "warm, narrator pace"  # only used by providers that support it
--max-walkthrough-s 22                # cap browser recording length
```

## Configuration

`~/config/household.json → skills.video_forge` is the single source of truth:

```json
{
  "default_provider": "openai",
  "default_voice": "alloy",
  "default_instructions": null,
  "fallback_chain": ["openai", "gemini", "elevenlabs"],
  "transcription_provider": "elevenlabs",
  "transcription_fallback_chain": ["elevenlabs", "openai", "gemini"]
}
```

Read fresh on every call — Daniel can flip provider order at runtime without restarting anything.

API keys (`skills.apiKeys`):

| Provider     | Key name           | Used for             |
|--------------|--------------------|----------------------|
| OpenAI       | `openai_whisper`   | TTS *and* STT        |
| Gemini       | `google_cloud_tts` | TTS                  |
| ElevenLabs   | `elevenlabs`       | TTS *and* Scribe STT |

The `openai_whisper` name is voice-palette's existing convention — kept for billing alignment with the rest of the household.

## Pipeline

```
metadata.json + README.md
         │
         ▼
   SCRIPT          gpt-5.4 drafts script.md (3 acts, 150–220 words)
         │
         ▼
   TTS             fallback walker → voiceover.mp3 (or .wav for Gemini)
         │
         ▼
   WALKTHROUGH     Playwright chromium 1280×720 → walkthrough.mp4
         │
         ▼
   TRANSCRIBE      helpers/transcribe.py (Scribe, verbatim word-level)
         │
         ▼
   ASSEMBLE        ffmpeg mux honoring upstream Hard Rules:
                     - Rule 1: subtitles LAST in filter chain
                     - Rule 3: 30ms audio fades at boundaries
                     - Rule 5: SRT times from word.start/end directly
                     - Rule 8: word-level Scribe transcript untouched
         │
         ▼
   OUTPUT          → <project>/edit/demo.mp4
                   → ~/workspaces/shared/images/mvps/<slug>/demo.mp4
                   → metadata.json.demo_video_url
```

## Verified run (2026-04-25, smoke test on Mark Forge)

| Stage         | Duration |
|---------------|----------|
| script        | 6.4s     |
| tts (openai)  | 13.3s    |
| walkthrough   | 23.1s    |
| transcribe    | 8.8s     |
| assemble      | 8.3s     |
| **total**     | **~60s** |

Result: 81.8s `demo.mp4` (1280×720 H.264, 113 SRT cues, voiceover speed 1.04×).

## Phase-2 UI

Surface defined in `docs/PHASE2_UI.md`. The `video_forge/api.py` module is the JSON contract — no UI built yet.

## Known issues / TODOs

- **ElevenLabs synth returns 402 Payment Required** on the household account. Fallback walker handles it correctly (skips to OpenAI). ElevenLabs Scribe transcription is on a separate billing tier and works fine.
- **Scribe mishears proper nouns** with no acoustic anchor: "Mark Forge" → "Markforged", "Tollefsen" → "Tollefson". Fixable later via TTS instructions or a post-Scribe lexicon override.
- **Walkthrough is generic.** Auto scene plan: load → scroll → hover first CTA → settle. Project-specific scene scripts (`<project>/edit/scenes.json`) are not yet supported but the walkthrough module is structured to accept them.
- **ElevenLabs voice catalog is 2 defaults.** Phase 2 will pull the full /v1/voices list.
