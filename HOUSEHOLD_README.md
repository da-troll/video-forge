# video-forge — Household Demo Reel Forge

Vendored fork of [browser-use/video-use](https://github.com/browser-use/video-use) (4576★) with a household-specific orchestrator that auto-produces a 60–90s demo video for any nightly MVP at `~/projects/nightly-mvps/<date>-<slug>/`.

> ⚠️ **Distribution:** upstream has no LICENSE file as of clone (2026-04-25). This fork is **not pushed** until either (a) upstream adds a permissive license, or (b) Daniel makes an explicit attribution-only call. See [`ATTRIBUTION.md`](ATTRIBUTION.md).

## What you get

```
python -m video_forge.demo --project ~/projects/nightly-mvps/2026-04-24-logo-generator
```

Pipeline (each stage logged to `<project>/edit/pipeline.log.json`):

1. **SCRIPT** — agent reads `metadata.json + README.md`, drafts `script.md` (3 acts, 150–220 words). Optional frontmatter (`provider`, `voice`, `instructions`) overrides defaults.
2. **TTS** — multi-provider synth with config-driven fallback. Reads `~/config/household.json → skills.video_forge.fallback_chain` at call time.
3. **WALKTHROUGH** — Playwright (chromium, 1280×720, headless) records the live URL, probing the DOM for a primary CTA.
4. **TRANSCRIBE** — upstream's `helpers/transcribe.py` (ElevenLabs Scribe) with word-level timestamps.
5. **ASSEMBLE** — burns 2-word UPPERCASE subtitles last in the filter chain (Hard Rule 1), 30ms audio fades (Hard Rule 3), loops walkthrough silently to match voiceover duration.
6. **OUTPUT** — copies `demo.mp4` to `<project>/edit/demo.mp4` AND `~/workspaces/shared/images/mvps/<slug>/demo.mp4` (auto-public via Caddy `/media/` mount), extends `metadata.json` with `demo_video_url`.

## Configuration

`~/config/household.json → skills.video_forge`:

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

Provider order is read **at every call**, never cached. Edit this file and the next render uses the new chain.

API key conventions (mirrored from voice-palette for billing alignment):

- OpenAI TTS → `skills.apiKeys.openai_whisper`
- Gemini TTS → `skills.apiKeys.google_cloud_tts`
- ElevenLabs (Scribe + TTS) → `skills.apiKeys.elevenlabs`

## Voice selection

`--voice` accepts either a raw voice ID (`alloy`, `Kore`, `21m00Tcm4TlvDq8ikWAM`) or a profile UUID. Profiles are saved triples of `(provider, voice_id, instructions)`, stored at `~/.local/share/video-forge/profiles.db`.

```python
from video_forge.api import save_profile_api
save_profile_api(name="brand-narrator", provider="openai", voice_id="alloy", instructions="confident product launch tone")
```

## Layout

```
/opt/apps/video-forge/
├── SKILL.md, helpers/, pyproject.toml, …  ← upstream, never edited
├── ATTRIBUTION.md                          ← citation + license flag
├── HOUSEHOLD_README.md                     ← this file
├── household-requirements.txt              ← household deps (openai, google-genai, playwright)
├── docs/PHASE2_UI.md                       ← future ClawDash drop-zone surface spec
└── video_forge/                            ← household-specific package
    ├── config.py                           ← reads skills.video_forge from household.json
    ├── observability.py                    ← Pipeline + Mermaid Gantt
    ├── api.py                              ← JSON in/out for phase-2 UI
    ├── tts/
    │   ├── voices_openai.py                ← 13 voices, verbatim from voice-palette
    │   ├── voices_gemini.py                ← 30 voices, verbatim
    │   ├── voices_elevenlabs.py            ← 2 voices, full catalog deferred
    │   ├── openai.py / gemini.py / elevenlabs.py  ← provider adapters
    │   ├── catalog.py                      ← list_all_voices() across providers
    │   ├── profiles.py                     ← SQLite CRUD (~/.local/share/video-forge/profiles.db)
    │   └── synth.py                        ← fallback walker, reads chain at runtime
    └── demo/
        ├── script.py                       ← draft + write script.md (gpt-5.4)
        ├── walkthrough.py                  ← Playwright recorder + ffmpeg transcode
        ├── assemble.py                     ← word-timed SRT + final ffmpeg compose
        └── orchestrator.py                 ← run() + CLI entry
```

## Hard rules from upstream (memorise — production correctness)

The full list lives in [`SKILL.md`](SKILL.md). The ones that matter for the demo orchestrator:

1. **Subtitles applied LAST in the filter chain.** Otherwise overlays hide them.
3. **30ms audio fades at every boundary.** Otherwise audible pops.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode.
9. **Cache transcripts per source.** Upstream's `transcribe.py` already does this — reuse it, don't roll our own.

`assemble.py` enforces 1, 3, and inherits 8/9 by calling upstream's helpers.

## Smoke test

```bash
cd /opt/apps/video-forge
uv run python -m video_forge.demo --project ~/projects/nightly-mvps/2026-04-24-logo-generator
```

Should produce `<project>/edit/demo.mp4` (~80s, h264+aac, 1280×720) and copy to `~/workspaces/shared/images/mvps/<slug>/demo.mp4`. Verify via `https://clawdash.trollefsen.com/media/mvps/<slug>/demo.mp4`.

## Pulling upstream updates

```bash
cd /opt/apps/video-forge
git fetch upstream
git merge upstream/main
```

Conflicts should only ever appear in upstream files (we don't edit them). If something we depend on changes, `git merge --strategy-option=theirs upstream/main` is safe.

## Phase 2

[`docs/PHASE2_UI.md`](docs/PHASE2_UI.md) sketches the planned ClawDash drop-zone surface — endpoints, WS event schema, voice-picker UX. Not built tonight; the seams in `video_forge/api.py` are the contract phase-2 UI work will wire against.
