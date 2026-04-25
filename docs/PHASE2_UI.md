# Phase-2 UI Surface — ClawDash drop-zone

**Status:** spec only. No UI built tonight. The seams in `video_forge/api.py` are the contract this UI wires against.

## Concept

A drop-zone in ClawDash where Daniel pastes a nightly-MVP slug (or path) and gets:

1. A **script preview** he can edit (approval gate)
2. A **voice picker** with live preview
3. A **render button** that streams progress over WebSocket
4. The finished `demo.mp4` linked back at `https://clawdash.trollefsen.com/media/mvps/<slug>/demo.mp4`

## HTTP endpoints

All return JSON unless noted. All wrap `video_forge.api` functions one-to-one, so the HTTP layer is a thin shell over already-tested Python.

```
POST   /api/video-forge/drop                  → { project_dir, metadata, script_preview }
       body: { slug | project_dir }
       Reads metadata.json + drafts initial script.md (cached if exists).

POST   /api/video-forge/generate-demo         → { run_id }
       body: { project_dir, options: {voice, instructions, fallback_chain} }
       Kicks off run() in a worker; client subscribes to WS for progress.

GET    /api/video-forge/list-voices           → { providers: {openai|gemini|elevenlabs: [{id, name, ...}]} }
       Wraps api.list_voices().

POST   /api/video-forge/preview-voice         → audio/mpeg | audio/wav (binary)
       body: { provider, voice_id, instructions, sample_text }
       Wraps api.preview_voice(); cached by content hash.

GET    /api/video-forge/profiles              → [{id, name, provider, voice_id, ...}]
GET    /api/video-forge/profiles/{id}         → {...}
POST   /api/video-forge/profiles              → {...}     body: {name, provider, voice_id, instructions}
PUT    /api/video-forge/profiles/{id}         → {...}     body: any subset
DELETE /api/video-forge/profiles/{id}         → {ok: true}
```

## WebSocket event schema

Subscribe to `/ws/video-forge/runs/<run_id>`. Server emits:

```json
{ "type": "stage_started",    "stage": "script", "ts": 1714080123.456 }
{ "type": "stage_progress",   "stage": "tts",    "message": "synthesising 187 words", "ts": ... }
{ "type": "fallback_used",    "stage": "tts",    "from": "elevenlabs", "to": "openai", "reason": "402 Payment Required" }
{ "type": "stage_done",       "stage": "tts",    "duration_s": 3.4, "output_size_bytes": 75264, "extra": {...} }
{ "type": "render_complete",  "demo_video_url": "https://...", "log_path": "..." }
{ "type": "error",            "stage": "...",    "message": "..." }
```

The orchestrator's `Pipeline` already records all of this in `pipeline.log.json`; the WS layer just tails the log + augments with live messages.

## Approval-gate handshake

The flow blocks at the script-preview stage until the user approves. The drop-zone:

1. Calls `POST /drop` → gets `script_preview` (markdown text).
2. Renders the script in an editable textarea with a "Use this voice" picker below.
3. On user "Render demo", calls `POST /generate-demo` with the resolved `{provider, voice_id, instructions}`.
4. Subscribes to the WS for progress.

If the user edits the script before kicking off render, the orchestrator skips the SCRIPT stage (cache_hit=true) and uses their edits directly. `script.md` on disk is the source of truth.

## Voice Picker UX

```
┌────────────────────────────────────────────────────┐
│ Voice                                              │
│  ▼ [Saved profiles] / OpenAI / Gemini / ElevenLabs │
│                                                    │
│  ▶ alloy · The Narrator                  [🔊]      │
│    ash · The Anchor                       [🔊]      │
│    ...                                             │
│                                                    │
│ Style/instructions (optional)                      │
│  ┌──────────────────────────────────────────────┐ │
│  │ "Confident product launch tone, slightly     │ │
│  │  faster pace than baseline."                 │ │
│  └──────────────────────────────────────────────┘ │
│                                                    │
│  [💾 Save as profile…]                             │
└────────────────────────────────────────────────────┘
```

- Dropdown groups: **[Saved profiles]** first (most recent at top), then **OpenAI** (13), **Gemini** (30), **ElevenLabs** (1–2 tonight, full catalog phase 2+).
- Each row: persona/name/tagline + 🔊 button that streams a 6-second preview from `/preview-voice` over the WS.
- Free-form **Instructions** field — sent to providers that support it (OpenAI `gpt-4o-mini-tts`, Gemini inline-prompt prefix). ElevenLabs ignores it gracefully (parenthetical hint, see `tts/elevenlabs.py`).
- **Save as profile** captures `{provider, voice_id, instructions}` and stores under a user-supplied name (unique). Selecting a saved profile resolves to that triple at render time — `config.default_voice` may be either a raw voice_id OR a profile_id.

## Browser concerns

- WS messages may include large `extra` blobs (scene lists, fallback walks). Compress with `permessage-deflate`, cap stage `extra` to ~8 KB.
- Preview audio: stream as `audio/mpeg` (OpenAI/ElevenLabs) or `audio/wav` (Gemini). The frontend sniffs from `Content-Type`.
- Render-complete event includes the public URL — the drop-zone can `<video src>` it directly.

## What's deferred to phase 3+

- Multi-take editing (B-roll cuts, retakes detection, filler-word stripping)
- Manim animation overlays (the `skills/manim-video` upstream sub-skill)
- Custom EDL specifications via UI
- Approval-gate per stage (vs. the single script-preview gate above)
- Full ElevenLabs voice catalog UI
