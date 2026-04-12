# video-editor

A Claude Code skill that edits video by conversation. Drop footage in a folder, launch Claude Code, have a conversation, iterate until it's right. Works for any content — talking heads, montages, tutorials, travel, interviews — without presets or menus.

The skill handles transcription (ElevenLabs Scribe), cut selection (LLM reasons over a packed phrase-level transcript), color grading (proven ffmpeg filter chain), subtitle burn-in (2-word UPPERCASE chunks), and overlay animations (PIL / Manim / Remotion via parallel subagents). Session state persists to a `project.md` memory file so next week's session picks up where you left off.

## Install

```bash
# 1. Symlink into ~/.claude/skills so any Claude Code session can use it
ln -s "$(pwd)" ~/.claude/skills/video-editor

# 2. Install Python deps
pip install -e .

# 3. Set your ElevenLabs API key
cp .env.example .env
$EDITOR .env                  # paste ELEVENLABS_API_KEY=...

# 4. System tools
brew install ffmpeg           # required
brew install yt-dlp           # optional, for downloading online sources
```

Python 3.10+ recommended (helpers also work on 3.9 via `from __future__ import annotations`).

## Use

```bash
cd /path/to/your/videos       # wherever your source files live
claude                         # start a Claude Code session
```

Then in the session:

> edit these videos into a launch video

The skill will inventory the sources, ask about what you're making, propose a strategy, wait for your confirmation, then produce `edit/final.mp4` next to the sources. All outputs go into `<videos_dir>/edit/` — the skill directory stays clean.

## What's in this repo

```
video-editor/
├── SKILL.md               the product — instructions for Claude Code
├── pyproject.toml         Python deps
├── .env.example
├── helpers/               CLI tools invoked by the skill
│   ├── transcribe.py        ElevenLabs Scribe (single file)
│   ├── transcribe_batch.py  parallel (4 workers) for multi-take
│   ├── pack_transcripts.py  raw JSON → phrase-level markdown (the LLM's reading view)
│   ├── timeline_view.py     filmstrip + waveform PNG for visual drill-down
│   ├── render.py            per-segment extract → concat → overlays → subtitles LAST
│   └── grade.py             ffmpeg color grade presets (warm_cinematic proven)
└── skills/
    └── manim-video/       vendored from hermes-agent for deep Manim expertise
```

## Design principles

1. The LLM reasons from a raw transcript + on-demand visual composites. Preprocessing beyond the packed-phrase view is the antipattern.
2. Audio is primary; visuals follow. Cuts happen on word boundaries and silence gaps.
3. Ask → confirm → execute → iterate → persist. Never edit before the user has approved the strategy.
4. Generalize. The skill contains zero assumptions about what kind of video it's editing.

Production rules (subtitle placement, ffmpeg filter chains, animation pacing, subagent brief structure) are derived from `cc-video-editor/HEURISTICS.md` — the real-world notes from shipping a launch video end-to-end.
