# video-forge

Conversation-driven video editor and demo-reel generator for the household.

<video src="https://github.com/da-troll/video-forge/raw/main/assets/mark-forge-demo.mp4" controls width="100%"></video>

> **Demo above:** [Mark Forge](https://github.com/da-troll/logo-generator) — a household logo generator — fully auto-produced by `python -m video_forge.demo` from nothing but the project's `metadata.json` + `README.md`. End-to-end in ~60–90s wall-clock, no human in the loop.

Drop a folder of footage and chat with Claude Code to produce `final.mp4`. Or point the orchestrator at a nightly MVP project directory and get a 45–75s demo video — voiceover + walkthrough + subtitles — without lifting a finger.

## Two modes

**Skill mode (interactive)** — drop into a folder of takes, run your agent, say "edit these into a launch video." The agent reads transcripts, proposes a strategy, waits for confirmation, then cuts. Audio-first reasoning, on-demand visual composites — the LLM never watches the video, it *reads* it.

**Module mode (autonomous)** — point at a nightly MVP project directory; the orchestrator runs a scene planner against the live URL, drafts a 130–200-word product-story voiceover (problem-first opener, no demo-data leak), synthesizes at 1.10× speed via OpenAI nova, records a Playwright walkthrough (12–18 scenes, 4s wait floors / 3.5s post-action floors), transcribes for word-level subtitles, and assembles `demo.mp4`. Output is versioned (`demo-<run_id>.mp4`) so the URL is cache-stable across runs. End-to-end in 60–110 seconds wall-clock.

```bash
# Skill mode
cd /path/to/your/footage && claude
> edit these into a launch video

# Module mode
python -m video_forge.demo --project ~/projects/nightly-mvps/2026-04-25-something
```

## Multi-provider TTS

Three providers, fully config-driven via `~/config/household.json → skills.video_forge`:

| Provider | Voices | Notes |
|----------|--------|-------|
| OpenAI `gpt-4o-mini-tts` | 13 | Fast, supports per-call style instructions |
| Gemini 2.5 / 3.x TTS | 30 | Multi-speaker, persona-rich, supports style prompts |
| ElevenLabs | 2 (defaults) | Plus the full Scribe transcription pipeline |

Provider order, default voice, and instructions string are runtime-editable. Save voice profiles to a SQLite-backed catalog (`~/.local/share/video-forge/profiles.db`) and reference them by ID. The fallback chain walks providers on auth/quota errors and logs each attempt.

## Pipeline (module mode)

```
PREFLIGHT ──> PLAN ──> SCRIPT ──> TTS ──> LOUDNORM ──> WALKTHROUGH
   ──> ALIGN ──> ASSEMBLE ──> ASSERTIONS ──> OUTPUT
```

| Stage | What | Notes |
|---|---|---|
| preflight | HTTP 200 + Playwright load + identity check on `live_url` | aborts before any LLM/TTS spend if the URL is broken |
| plan | `scene_planner.plan_scenes` → `scenes.json` | gpt-5.4 + DOM probe + 1280×720 screenshot, strict-mode JSON; pacing floors enforced post-LLM (wait ≥4s, action_after ≥3.5s); `estimated_duration_s` is recomputed from scene-sums (LLM self-report ignored). 12–18 scenes, 50–75s walkthrough target. |
| script | product-story draft via gpt-5.4 | 130–200-word budget; problem-first opener pattern (hard-banned: "In Trollefsen…", "The Tollefsen household…", any family naming as subject); closing why-it-matters one-liner; demo-data anti-leakage retry (rejects screen-reader regressions where >35% of script tokens overlap with scene names/values, retries once with explicit forbidden-token list) |
| tts | OpenAI gpt-4o-mini-tts at 1.10× speed | nova default; speed configurable via `default_speed` in household.json |
| loudnorm | two-pass loudnorm to −14 LUFS / −1 dBTP | runs before align so Whisper sees normalized audio |
| walkthrough | Playwright records `walkthrough.mp4` | press_sequentially typing for visible form fills; raw .webm preserved in `edit/_raw_debug/` for truncation diagnosis; multi-webm concatenation if Playwright splits the recording on hard navigation |
| align | Whisper-1 word timestamps + Needleman-Wunsch script substitution | speech-onset anchored, falls back to uniform on API failure. (`gpt-4o-transcribe` is faster but rejects `verbose_json`, so word timestamps unavailable — keep whisper-1 until ElevenLabs Scribe is wired into align.py.) |
| assemble | filter-graph mux: walkthrough + voiceover + burned-in SRT | hold-tail strategy fills audio-vs-video gaps; subtitles applied last; phrase-length cues (~5 words/cue), FontSize 14, Outline 1.2 |
| assertions | quality gate: file size, duration, SRT integrity, lead silence, tail drift, **tail-gap (≤12s)**, integrated LUFS | failure aborts before publishing |
| output | versioned `demo-<run_id>.mp4` + `demo.mp4` "latest" copy in shared dir; `metadata.json.demo_video_url` points to versioned file for cache-busting | served at `https://clawdash.trollefsen.com/media/mvps/<slug>/demo-<run_id>.mp4` |

Each stage logs to `<project>/edit/pipeline.log.json` with timings, cache hits, retry count, fallback chain walked. Run with `--gantt` to render a Mermaid Gantt of the run.

## How it works (skill mode)

The LLM never sees pixels. It reads:

- **Audio transcript (always)** — ElevenLabs Scribe word-level timestamps + speaker diarization + audio events, packed into a single phrase-level markdown file (`takes_packed.md`).
- **Visual composite (on demand)** — `timeline_view.py` produces a filmstrip + waveform + word-label PNG for any time range. Called only at decision points.

> Naive approach: 30,000 frames × 1,500 tokens = **45M tokens**.
> video-forge: **~12KB text + a handful of PNGs.**

## Hard rules

The skill enforces 12 production-correctness rules (subtitles last in the filter chain, 30ms audio fades, word-boundary cuts, padded edges, parallel sub-agents for animations, strategy confirmation before execution, etc.). See [`SKILL.md`](./SKILL.md).

## Configuration

```json
"video_forge": {
  "default_provider": "openai",
  "default_voice": "nova",
  "default_speed": 1.10,
  "default_instructions": null,
  "fallback_chain": ["openai", "gemini", "elevenlabs"],
  "transcription_provider": "elevenlabs",
  "transcription_fallback_chain": ["elevenlabs", "openai", "gemini"],
  "tail_strategy": "hold"
}
```

`default_speed` is the TTS playback rate (only honored by OpenAI's adapter; gemini/elevenlabs ignore). 1.10 is the empirical sweet spot — 1.15 sounded mildly rushed in QA; 1.0 felt sluggish against typical walkthrough pacing.

Voice can be a raw `voice_id` or a saved `profile_id`. Resolved per call.

**Tail strategies** (used when the voiceover is longer than the recorded walkthrough):

| Strategy | Behavior |
|---|---|
| `hold` (default) | Extract walkthrough's final frame, generate a silent held-frame video for the gap, concat. Visually less obvious than a loop. |
| `loop` | Stream-loop the walkthrough until it covers the voiceover. Legacy behavior. |
| `trim_voice` | Trim the voiceover to the walkthrough length. Debug-only — usually wrong. |

Override at runtime with `--tail-strategy {hold,loop,trim_voice}`.

**Brand lexicon.** `references/brand-lexicon.yaml` is the single source of truth for product names, agent names, and pronunciation hints. It's loaded by `video_forge/references.py` and applied at:

1. **Script draft time** — `brand_voice_rules` are prepended to the LLM system prompt, and the drafted body is run through a quote-safe canonicalization pass (e.g., `Tollefsen product` → `Trollefsen product`, but `"Daniel Tollefsen"` is preserved).
2. **TTS synth time** — text-aware pronunciation hints are appended to the OpenAI `instructions` field and to the Gemini style prompt. ElevenLabs v1 has no equivalent; brand pronunciation there is best-effort.

Edit `brand-lexicon.yaml` to add new products, agents, or pronunciations.

**Scene planner.** Module mode uses `video_forge/demo/scene_planner.py` to produce an MVP-aware walkthrough plan. The planner does a 5s headless DOM probe + screenshot, sends the result to `gpt-5.4` with a strict JSON schema, and writes `<project>/edit/scenes.json`. The plan is reused on subsequent runs (hand-edit between runs to refine), or use `--regen-scenes` to force a fresh plan. Pass `--scene-plan <path>` to bypass the planner with a hand-authored file. If the planner fails or `gpt-5.4` is unavailable, the recorder falls back to a generic CTA-hunting plan.

**MVP visibility for recording.** `mvp.trollefsen.com` projects sit behind a basic-auth gate by default. The video-forge Playwright session does not authenticate. Workaround: flip `metadata.json.visibility` to `"public"`, run `bash /home/eve/workspaces/shared/scripts/nightly-builder/mvp-finalize.sh`, run the orchestrator, flip back to `"private"`, finalize again. There is no `--auth` flag yet; this is the next obvious wiring task when integrating with the nightly cron.

**System fonts.** Headless Chromium ships without emoji glyphs on Linux. Install `fonts-noto-color-emoji` system-wide (`sudo apt install fonts-noto-color-emoji && sudo fc-cache -fv`) so MVPs with emoji UI (cover pickers, status badges) render correctly in the recording.

## Install

```bash
git clone https://github.com/da-troll/video-forge /opt/apps/video-forge
cd /opt/apps/video-forge
uv sync
uv pip install -r household-requirements.txt
ln -sfn /opt/apps/video-forge ~/.claude/skills/video-forge
brew install ffmpeg            # macOS
# sudo apt-get install ffmpeg  # Linux
playwright install chromium    # for module mode
```

API keys live in `~/config/household.json → skills.apiKeys` (`openai_whisper`, `google_cloud_tts`, `elevenlabs`). Skill mode also reads `ELEVENLABS_API_KEY` from `.env` at the repo root for upstream-style use.

## Layout

```
SKILL.md                   ← skill-mode entry (12 hard rules + craft)
helpers/                   ← Python helpers driven by the skill
  transcribe.py            ← ElevenLabs Scribe single-file
  transcribe_batch.py      ← parallel
  pack_transcripts.py      ← phrase-level packing
  timeline_view.py         ← filmstrip + waveform PNG
  render.py                ← per-segment extract → concat → overlays → subtitles
  grade.py                 ← color-grade filter chains
video_forge/               ← module-mode package
  demo/                    ← orchestrator (script/tts/walkthrough/assemble)
  tts/                     ← multi-provider synth + voice catalog + profiles
  api.py                   ← JSON in/out seams (used by the planned ClawDash UI)
  observability.py         ← pipeline.log.json + Gantt rendering
  config.py                ← household.json reader
docs/PHASE2_UI.md          ← spec for the ClawDash drop-zone UI
```

## Inspired by

[browser-use/video-use](https://github.com/browser-use/video-use) — the text-first skill-mode pipeline, the 12 hard rules, and the 6 helpers in `helpers/` originate there. The household additions (multi-provider TTS, module-mode orchestrator, Playwright walkthroughs, voice profiles, observability, ClawDash UI seams) are ours.

## License

MIT — see [`LICENSE`](./LICENSE).
