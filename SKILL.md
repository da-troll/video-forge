---
name: video-editor
description: Edit any video by conversation. Transcribe, cut, color grade, generate overlay animations, and burn subtitles — for talking heads, montages, tutorials, travel, interviews. No presets, no menus. Ask questions, confirm the plan, execute, iterate, persist. Production rules are proven — they shipped a real launch video.
---

# Video Editor

## Principle

1. You reason from the raw transcript plus on-demand visual composites. **Do not over-preprocess.** The one derived artifact that earns its keep is a packed phrase-level transcript (`takes_packed.md`) — one call, one file, the primary reading view for cut selection. Everything else (filler tagging, retake detection, shot classification, emphasis scoring) you derive from transcript at decision time.
2. **Audio is primary, visuals follow.** Cut candidates come from speech boundaries and silence gaps. Drill into visuals only at actual decision points.
3. **Ask → confirm → execute → iterate → persist.** Never touch the cut until the user has confirmed the strategy in plain English.
4. **Generalize.** Do not assume what kind of video this is. Look at the material, ask the user about it, then edit.

## Directory layout

The skill lives in `video-editor/`. The user's source files live wherever they put them. **All session outputs go next to the sources in an `edit/` subfolder** — nothing is written inside `video-editor/`.

```
<videos_dir>/                    ← wherever the user's footage lives
├── <source files, untouched>
└── edit/
    ├── project.md               ← memory; appended every session
    ├── takes_packed.md          ← phrase-level transcripts, the LLM's primary reading view
    ├── edl.json                 ← cut decisions
    ├── transcripts/<name>.json  ← cached per-source raw Scribe JSON
    ├── animations/slot_<id>/    ← per-animation source + render + reasoning
    ├── clips_graded/            ← per-segment extracts with color grade + audio fades
    ├── master.srt               ← output-timeline subtitles (merged per-segment)
    ├── downloads/               ← yt-dlp outputs (if any)
    ├── verify/                  ← debug frames extracted at cut points / overlay windows
    ├── preview.mp4
    └── final.mp4
```

Every helper takes a video path and infers `edit/` as `<video_parent>/edit/` by default, or accepts an explicit `--edit-dir` override.

## Setup

- `ELEVENLABS_API_KEY` in `.env` at the project root or process environment. Ask the user and write `.env` if missing.
- `ffmpeg` + `ffprobe` on PATH.
- Python deps via `pip install -e .` (reads `pyproject.toml`).
- `yt-dlp`, `manim`, Remotion installed **only on first use**.
- This skill vendors `skills/manim-video/` for Manim expertise. Read its SKILL.md when building a Manim animation slot.

## Helpers (CLI tools in `helpers/`)

- **`transcribe.py <video>`** — single-file transcription. Optional `--num-speakers N` when known improves diarization. Cached per output path.
- **`transcribe_batch.py <videos_dir>`** — 4-worker parallel transcription of every `.MP4/.mov` in a directory. ~4× speedup over sequential for 10+ clips. **Use this for multi-take projects.**
- **`pack_transcripts.py --edit-dir <dir>`** — reads all `transcripts/<name>.json`, groups words into phrase-level chunks (break on any silence ≥ 0.5s), writes `takes_packed.md`. This is the primary artifact the editor LLM reads to pick cuts.
- **`timeline_view.py <video> <start> <end>`** — filmstrip + waveform composite PNG. Range mode or `--edl` mode. The only visual drill-down tool. Use at decision points, not constantly.
- **`render.py <edl.json> -o <out>`** — per-segment extract (grade + audio fades baked in) → lossless concat → optional overlay chain with PTS shift → subtitles applied LAST. `--preview` for 720p fast.
- **`grade.py <in> -o <out>`** — ffmpeg filter chain color grade. Ships with a proven `warm_cinematic` preset; accepts `--filter '<raw>'` for custom.

**For animations,** create `<edit>/animations/slot_<id>/` directly with `Bash`, write `spec.md`, and spawn a subagent via the `Agent` tool. No `animate.py` helper.

## The process

1. **Inventory.** `ffprobe` every source. `transcribe_batch.py` on the whole directory. `pack_transcripts.py` to produce `takes_packed.md`. Sample one `timeline_view` per source (middle of the clip) for a visual first impression.
2. **Pre-scan verbal slips.** One LLM pass over `takes_packed.md` to spot obvious mis-speaks, wrong words, or regrettable phrasings. Produce a plain list: `[C0103@38.46: "CSS writers" (meant CSS selectors)]`. Feed this list into the editor brief so the editor knows what to avoid or stop before.
3. **Converse.** Describe what you see in plain English. Ask questions *shaped by the material*, not from a fixed checklist. A drone reel needs different questions than an interview.
4. **Propose strategy.** 4–8 sentences: shape, take choices, cuts, animations, grade, length. Wait for confirmation. **This is the most important checkpoint.**
5. **Execute.** Produce `edl.json` via the editor sub-agent brief below. Drill into `timeline_view` at ambiguous moments. Propose and build animations in parallel sub-agents. Compose via `render.py`.
6. **Preview.** `render.py --preview`. Spot-check 3–5 cut points via `timeline_view`.
7. **Iterate + persist.** Natural-language feedback, re-plan, re-render. Never re-transcribe. Final render on confirmation. Append to `project.md`.

## Cut rules (non-negotiable, learned from shipping)

- **Never cut inside a word.** Snap every edge to a word boundary from the transcript.
- **Pad every cut edge.** 50ms before the first kept word, 80ms after the last. Word timestamps drift 50–100ms in Scribe output — padding absorbs the drift.
- **Prefer silences ≥ 400ms for cuts.** 150–400ms needs a visual check. <150ms is unsafe.
- **Preserve peaks.** Laughs, punchlines, emphasis. Extend clips past punchlines to include the reaction — the laugh IS the beat.
- **Speaker handoffs** get 400–600ms of air between utterances.
- **Audio events are cut signals.** `(laughs)`, `(sighs)`, `(applause)` mark beats. Extend past them, don't cut before them.
- **Never reason about audio and video independently.** Every cut must work on both tracks.

## The editor sub-agent brief (for multi-take selection)

When the task is "pick the best take of each beat across many clips," spawn a dedicated editor sub-agent with exactly this brief structure. This is the format that shipped a real launch video.

```
You are editing a <type> video. Pick the best take of each beat and 
assemble them chronologically by beat, not by source clip order.

INPUTS:
  - takes_packed.md (time-annotated phrase-level transcripts of all takes)
  - Product context: <2 sentences from the user>
  - Speaker(s): <name, role, delivery style note>
  - Expected pitch structure: <e.g., HOOK → PROBLEM → SOLUTION → BENEFIT → EXAMPLE → CTA>
  - Verbal slips to avoid: <list from the pre-scan pass>
  - Target runtime: <seconds>

RULES:
  - Start/end times must fall on word boundaries from the transcript.
  - Pad cut boundaries by 50–150ms.
  - Prefer silences ≥ 400ms as cut targets.
  - Unavoidable slips are kept if no better take exists. Note them in "reason".
  - If total runtime is over budget, revise: drop a redundant beat or trim tails.
    Report total and self-correct.

OUTPUT (JSON array, no prose):
  [
    {
      "source": "C0103",
      "start": 2.42,
      "end": 6.85,
      "beat": "HOOK",
      "quote": "Ninety percent of what a web agent does is completely wasted. We fixed this.",
      "reason": "Cleanest delivery, stops before the 'CSS writers' slip at 38.46."
    },
    ...
  ]

Return the final EDL and a one-line total runtime check.
```

## The render pipeline (the rules that matter)

This is where the skill's theory meets ffmpeg's reality. These rules are non-negotiable because each one addresses a specific silent-failure mode.

### Rule 1 — Subtitles are applied LAST, after all overlays

If you burn subtitles into the base video and then overlay animations, **the animations hide the captions**. The correct order is strict:

1. Per-segment extract with color grade + audio fades, **no subtitles**
2. Lossless `-c copy` concat into `base.mp4`
3. Overlay animations on the concat'd base via filter graph
4. Apply `subtitles` filter as the FINAL step in the same filter graph

`render.py` enforces this automatically. Never override it.

### Rule 2 — Per-segment extract → lossless concat, not single-pass filtergraph

Extract each cut range as its own MP4 with color grade + audio fade baked in, then concat with `-c copy`. This gives: (a) lossless concat with no second re-encode, (b) trivially parallelizable extraction, (c) single source of truth for per-segment intermediates you can sanity-check in QuickTime.

Per-segment extract command (the workhorse pattern):
```
ffmpeg -y \
  -ss <seg_start> -i <source> -t <duration> \
  -vf "scale=1920:-2,<grade_filter>" \
  -af "afade=t=in:st=0:d=0.03,afade=t=out:st=<dur-0.03>:d=0.03" \
  -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p -r 24 \
  -c:a aac -b:a 192k -ar 48000 -movflags +faststart \
  clips_graded/seg_NN.mp4
```

`-ss` **before** `-i` for fast accurate seeking. `scale=1920:-2` normalizes everything to 1080p from 4K sources.

### Rule 3 — 30ms audio fades at every segment boundary

Hard audio cuts produce audible pops. `afade=t=in:st=0:d=0.03` + `afade=t=out:st={dur-0.03}:d=0.03` on every segment eliminates them without shifting sync or eating into audible content. Non-negotiable.

### Rule 4 — PTS shift overlays so frame 0 lands at the overlay window start

When you overlay an animation at time T, you need `setpts=PTS-STARTPTS+T/TB` on the overlay stream. Without it, the overlay plays from its natural time 0 during the `between(t,T,T+dur)` window — you only see the middle of the animation.

The single-pass overlay + subtitles filter graph:
```
[1:v]setpts=PTS-STARTPTS+T1/TB[a1];
[2:v]setpts=PTS-STARTPTS+T2/TB[a2];
[0:v][a1]overlay=enable='between(t,T1,T1+dur1)'[v1];
[v1][a2]overlay=enable='between(t,T2,T2+dur2)'[v2];
[v2]subtitles='master.srt':force_style='<style>'[outv]
```

Map `-map "[outv]" -map 0:a` and `-c:a copy` — don't re-encode audio that was finalized during extract.

### Rule 5 — Master SRT uses output-timeline offsets, not per-segment relative times

When the base is a concat of N segments, each segment has its own local timestamps. The master SRT needs output-timeline times. Compute:
```
output_time = word.start - segment_start + segment_offset_in_output
```
where `segment_offset_in_output` is the sum of durations of all earlier kept segments. Merge per-segment entries, sort by start time, write as one SRT file. `render.py` handles this.

### Rule 6 — Social-ready loudness normalization is MANDATORY, final audio stage

Every video destined for social media must exit at `I=-14 LUFS integrated, TP=-1 dBTP, LRA=11 LU`. This matches YouTube, Instagram, TikTok, X, LinkedIn auto-normalization targets. Delivering quieter just means the viewer's volume knob fights the video; delivering louder or clipping means the platforms crunch-compress it.

Apply via ffmpeg `loudnorm` filter in **two passes** on the final composite:
1. **Measurement pass** — `loudnorm=I=-14:TP=-1:LRA=11:print_format=json -vn -f null -` — parse the JSON block from stderr for `input_i`, `input_tp`, `input_lra`, `input_thresh`, `target_offset`.
2. **Normalization pass** — `loudnorm=I=-14:TP=-1:LRA=11:measured_I=<>:measured_TP=<>:measured_LRA=<>:measured_thresh=<>:offset=<>:linear=true` on the composite, re-encode audio to AAC 192k 48kHz.

Apply ONCE on the final concatenated + composited file — never per-segment (loudnorm needs the full track to measure correctly). `render.py` enforces this by default; use `--no-loudnorm` only if you know why.

### Rule 7 — Preview mode must be evaluable

`--preview` = 1080p libx264 medium CRF 22 (slightly faster than final but quality is judgeable). `--draft` = 720p ultrafast CRF 28 for cut-point verification only. The old "720p ultrafast CRF 28" preview was unwatchable and forced a second full render for QC — net slower. A preview the user can't judge has failed its purpose.

## Color grade

Apply **per-segment during extraction**, not post-concat — avoids double re-encode.

**Default: `auto` mode.** The `grade.py` helper samples ~10 frames from each clip range via `signalstats`, reads the mean luma / range / saturation, and emits a bounded per-segment correction (contrast ±8%, gamma ±10%, saturation ±6%). No creative color shifts. The goal is *"make it look clean without looking graded"* — the viewer should not notice the grade at all.

Set `"grade": "auto"` in your EDL. `render.py` resolves this per-segment by calling `grade.auto_grade_for_clip(source, start, duration)`.

**Opt-in presets** (use explicitly, never as default):
- `subtle` — `eq=contrast=1.03:saturation=0.98`. Floor when auto-analysis isn't available.
- `neutral_punch` — light contrast + subtle S-curve, no color shifts.
- `warm_cinematic` — the original retro/cinematic look (+12% contrast, crushed blacks, warm-shadow/cool-highlight colorbalance, filmic curve). **TOO AGGRESSIVE for launch content** — it shipped in an earlier launch video but user feedback after a second run: "TOO MUCH — do not do that much color". Use only when the brief explicitly asks for nostalgic/retro/cinematic.
- `none` — no grade.

Why auto over fixed presets: talking-head footage varies by clip depending on exposure, ambient temperature, camera ISO. A fixed preset that flatters one take crushes another. Per-clip analysis adapts. All adjustments bounded to avoid over-correction; if a clip is already balanced, auto emits near-identity.

**Never** apply a creative LUT to launch/social content without explicit user approval. **Never** go aggressive without testing via `timeline_view`.

## Subtitles (when requested)

Generated from the transcript. Burned in via ffmpeg's `subtitles` filter applied LAST (Rule 1).

**Chunking rules:**
- **2-word chunks.** 1 is choppy, 3 is too wide. Always 2.
- **Break on any punctuation** — comma, period, question mark, exclamation.
- **UPPERCASE everything.** Launch-video convention, also more legible at small sizes.

**Exact force_style (proven at 1920×1080):**
```
FontName=Helvetica,FontSize=18,Bold=1,
PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,
BorderStyle=1,Outline=2,Shadow=0,
Alignment=2,MarginV=35
```

`MarginV=35` puts captions near the bottom edge. `MarginV=90` is too high — captions visually dominate the frame. `FontSize=18` at 1920px — 22 is too wide.

## Animations (when requested)

Only build animations the user explicitly asks for.

### Tool choice per slot

- **PIL + PNG sequence + ffmpeg encode** — default for simple overlay cards: counters, typewriter text, single bar reveal, one-line reveals. Faster to iterate on, looks clean, no Manim weight. Never matplotlib — axis chrome looks scientific, wrong aesthetic.
- **Manim** — for formal diagrams, graph morphs, state machines, equation derivations. Read `skills/manim-video/SKILL.md` and its 14 references. Do not write Manim guidance in this file.
- **Remotion** — for typography-heavy brand content, comparisons, number-reveal sequences with complex layouts. Spring animations, `Easing.out(Easing.cubic)` never linear, heavy fonts ≥ 64px.

Default to **PIL** for simple overlays. Only reach for Manim/Remotion when the content demands it.

### The hard rule: readable at 1× on first watch

An animation a viewer can't parse without pausing is broken regardless of how pretty it is.

**Minimum durations:**
| Type | Minimum | Target |
|---|---|---|
| Single stat reveal | 3s | **5–7s** |
| Multi-stat staggered | 5s | 6–8s |
| Simple diagram | 6s | 8–10s |
| Complex diagram | 8s | 10–14s |

5–7s per simple animation is the sweet spot (HEURISTICS v3). 3s was too fast, didn't land. Hold the final frame ≥ 1s before cutting.

### Visual rules for PIL overlay cards

- **Pure black background** — `(10, 10, 10)` RGB, not true black
- **Single accent color** — one color for the thing that matters. HEURISTICS used orange `#FF5A00` / `(255, 90, 0)`. Match whatever brand the user gave in the conversation.
- **White for primary text, dim gray `(110, 110, 110)` for labels.** Nothing else.
- **Monospace everywhere** — Menlo Bold for launch content (`/System/Library/Fonts/Menlo.ttc` index 1). Cohesive terminal feel.
- **Minimal chrome** — no SYSTEM tags, package numbers, footers, blinking indicators. Every removed element makes the remaining elements look more intentional.

### Animation payoff timing (critical)

**Time the animation so the payoff frame lands on the spoken payoff word.** Get the word's timestamp from the transcript. Compute overlay start time = `spoken_word_time - reveal_duration`. If the animation reveals its key visual at frame 84 (3.5s in), start the overlay 3.5s before the spoken word.

Without this sync, the animation feels disconnected from the voiceover.

### Easing

```python
def ease_out_cubic(t):    return 1 - (1 - t) ** 3
def ease_in_out_cubic(t):
    if t < 0.5: return 4 * t ** 3
    return 1 - (-2 * t + 2) ** 3 / 2
```

`ease_out_cubic` for single reveals (slow landing). `ease_in_out_cubic` for continuous draws. **Never linear.** Linear reveals look robotic.

### Progressive reveal pattern (every animation is a variation of this)

```python
progress = (frame_idx - start_frame) / duration_frames
progress = max(0, min(1, progress))
eased = ease_in_out_cubic(progress)
# use `eased` to drive anything: bar height, line length, counter value, alpha
```

### Typing text — anchor at fixed x

The typewriter effect has one subtle bug: if you center the partial string, the text slides left as it grows. Fix: precompute `start_x` from the **full** string's centered width, then draw `text[:chars_shown]` at `(start_x, y)`.

```python
full_bbox = draw.textbbox((0, 0), full_text, font=f)
start_x = (W - (full_bbox[2] - full_bbox[0])) // 2
chars_shown = min(len(full_text), (frame_idx - start_frame) // 3)
draw.text((start_x, y), full_text[:chars_shown], fill=WHITE, font=f)
```

### Parallel sub-agents — N animations in parallel, never sequential

**Never build multiple animations in one agent.** Spawn N sub-agents in parallel via the `Agent` tool — each finishes in ~80-180s, total wall time ≈ slowest one, not the sum.

Each sub-agent prompt is **completely self-contained** (sub-agents have no parent context). A good brief has:

1. **One-sentence goal:** *"Build ONE animation: [concept]. Nothing else."*
2. **MANDATORY read-first section** — for Manim, point to `skills/manim-video/SKILL.md` + `references/visual-design.md` + `references/animation-design-thinking.md`. For PIL/Remotion, point to whatever the sub-skill ships.
3. **NARRATIVE CONCEPT, not layout.** Describe the story the animation tells — the "aha moment" — in prose. Do NOT write "circle A at position (-3, 0), label B at position (4, 2)". Let the agent design the composition. Over-prescribing x/y coordinates produces literal labeled diagrams that look like AI slide decks (real failure mode, burned by this once).
4. **Technical spec:** 1920×1080, 24fps, H.264 yuv420p, CRF 18, exact duration in seconds
5. **Style palette as exact colors** (hex or RGB tuples — `#FF5A00`, not "orange"), STRICT — no other colors allowed. Font paths or names.
6. **Opacity layering requirement** — primary 1.0, context 0.4, structural 0.15 — and require explicit `.set_opacity()` calls in the code. Everything-at-1.0 is a failure.
7. **Transform requirement** — at least one `Transform`, `ReplacementTransform`, `TransformMatchingShapes`, or `ValueTracker`-driven update. FadeIn-only animation is lazy.
8. **MANDATORY bounds check:** require an `ensure_in_frame(mobj)` helper that asserts every mobject lies inside the safe area (`x ∈ [-6.5, 6.5]`, `y ∈ [-3.5, 3.5]`). Text at large font sizes overflows anchor positions — this is the hidden failure mode that clips content off the frame edge.
9. **MANDATORY still-render gate:** before running full video, the agent must render a PNG via `manim -ql --format=png -s script.py Scene`, load it with PIL, and verify the outer rows/columns are all background color. If any edge pixel is non-background, something is clipping — fix before full render. This gate has saved a full re-render cycle in practice.
10. **Breathing room:** `self.wait(0.8)` minimum after any major reveal. Rushed animations don't land.
11. **Anti-list** — enumerate what NOT to put on screen. "NO giant labels. NO axes. NO bars. NO SYSTEM tags." Be specific. The anti-list prevents the agent from padding with filler.
12. **Deliverable checklist + duration verification via ffprobe.**
13. **"Do not ask questions. If ambiguous, pick the most sophisticated interpretation that fits the safe area."**

**One sub-agent = one file.** Use unique filenames (`script.py` inside `slot_N/` dirs) so parallel agents don't overwrite each other.

**Failure mode to avoid:** over-prescribed layouts. If your brief says "EXPLORATION label at x=-4.5, EXPLOITATION label at x=+4.5" you'll get a pitch deck. Describe what the scene is TRYING to communicate and trust the sub-agent's composition skills. Enforce discipline via the anti-list + bounds check, not by dictating coordinates.

## EDL format

```json
{
  "version": 1,
  "sources": {
    "C0103": "/abs/path/C0103.MP4",
    "C0108": "/abs/path/C0108.MP4"
  },
  "ranges": [
    {
      "source": "C0103", "start": 2.42, "end": 6.85,
      "beat": "HOOK",
      "quote": "Ninety percent of what a web agent does is completely wasted. We fixed this.",
      "reason": "Cleanest delivery, stops before CSS writers slip at 38.46."
    },
    {
      "source": "C0108", "start": 14.30, "end": 28.90,
      "beat": "SOLUTION",
      "quote": "...",
      "reason": "Only take without the false start."
    }
  ],
  "grade": "warm_cinematic",
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0},
    {"file": "edit/animations/slot_2/render.mp4", "start_in_output": 12.5, "duration": 6.0}
  ],
  "subtitles": "edit/master.srt",
  "total_duration_s": 87.4
}
```

`sources` maps short names (usually the filename stem) to absolute paths. `ranges` is the ordered list of keeps. `grade` is either a preset name or a raw ffmpeg filter string. `overlays` are the animation clips to composite on top of the base. `subtitles` is optional and applied LAST (see Rule 1).

## Memory — `project.md`

At session end, append a section to `<edit>/project.md`:

```markdown
## Session N — YYYY-MM-DD

**Strategy:** one paragraph

**Decisions:**
- take choices, cuts, grades, animations + WHY

**Reasoning log:**
- <timestamp> <source>: rationale for non-obvious decisions

**Outstanding:**
- things deferred for a later session
```

On every startup, read `project.md` if it exists, summarize the last session in one sentence, and ask whether to continue or start fresh.

## Anti-patterns (things we tried and rejected)

- **Hierarchical pre-computed codec formats** with `USABILITY` / `NOTABLE_BEATS` / shot/turn/word layers. Over-engineering. The LLM infers from raw transcripts fine.
- **Hand-tuned moment-scoring functions.** The LLM is better at reading transcripts and picking moments than any heuristic you'll write.
- **Whisper SRT / phrase-level output.** Loses sub-second gap data you need for cuts. Always word-level, always verbatim.
- **Running Whisper locally on CPU.** Slow, and it normalizes fillers out of the transcript. Use hosted Scribe.
- **Burning subtitles into the base before overlay compositing.** Overlays hide them. Subtitles LAST. Always.
- **Single-pass `filtergraph` across all cut ranges.** Re-encodes twice when you add overlays. Use per-segment extract → concat instead.
- **Dense animation cards with SYSTEM tags, package numbers, footers.** Looked busy and cheap. Strip chrome.
- **Animations at 2.5–4.0s.** Too fast to land. 5–7s minimum for simple overlays.
- **matplotlib for animations.** Scientific chrome. Use PIL primitives.
- **Linear animation easing.** Robotic. Always cubic.
- **Typing text centered on the partial string.** Text slides left as it grows. Anchor at the full-string's centered `start_x`.
- **Instant overlay hard cuts.** Looks like PowerPoint. Use 200ms alpha fades at each overlay edge (known gap — worth adding to v1 renders).
- **Sequential sub-agents for multiple animations.** 337s vs 81s parallel. Always parallel.
- **Hard audio cuts at segment boundaries.** Audible pops. 30ms fades on every segment.
- **Starting to edit before confirming the strategy.** Never. Ask → confirm → execute.
- **Re-transcribing cached sources.** Transcripts are immutable outputs of immutable inputs. Cache religiously.
- **Assuming what kind of video it is.** Look first, ask second, edit last.
