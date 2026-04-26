"""Orchestrator: SCRIPT → TTS → WALKTHROUGH → TRANSCRIBE → ASSEMBLE → OUTPUT."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from ..config import default_voice, tail_strategy as cfg_tail_strategy
from ..observability import Pipeline
from ..tts.synth import synthesize_with_fallback
from . import script as script_stage
from .assemble import assemble, build_master_srt
from .preflight import PreflightError, preflight
from .scene_planner import plan_scenes as run_scene_planner
from .walkthrough import record


SHARED_MEDIA_ROOT = Path("/home/eve/workspaces/shared/images/mvps")


def _import_helper(name: str):
    """Import an upstream helpers/<name>.py module by file path (it's not on sys.path)."""
    helpers_dir = Path(__file__).resolve().parent.parent.parent / "helpers"
    spec = importlib.util.spec_from_file_location(f"helpers_{name}", helpers_dir / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _read_metadata(project_dir: Path) -> dict:
    meta_path = project_dir / "metadata.json"
    if not meta_path.exists():
        return {}
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _slug(project_dir: Path) -> str:
    return project_dir.name


def run(project_dir: Path, options: dict[str, Any] | None = None) -> dict:
    options = options or {}
    project_dir = project_dir.resolve()
    if not project_dir.exists():
        raise FileNotFoundError(f"project not found: {project_dir}")

    edit_dir = project_dir / "edit"
    edit_dir.mkdir(parents=True, exist_ok=True)
    pipe = Pipeline(project_dir)
    metadata = _read_metadata(project_dir)
    live_url = options.get("live_url") or metadata.get("live_url")
    if not live_url:
        raise RuntimeError(f"no live_url in metadata.json or options for {project_dir.name}")

    # ── 0. PREFLIGHT ────────────────────────────────────────────────────────
    # Runs BEFORE any LLM/TTS spend. Aborts cleanly on broken live URLs.
    if not options.get("skip_preflight"):
        with pipe.stage("preflight") as st:
            try:
                project_hint = metadata.get("project_name") or project_dir.name
                pf = preflight(live_url, project_hint=project_hint)
                st.extra.update(pf.as_dict())
            except PreflightError as e:
                st.extra["error"] = str(e)
                raise

    # ── 1a. PLAN ────────────────────────────────────────────────────────────
    # Scene plan runs BEFORE the script so narration can pace to visual beats.
    # If --scene-plan was passed or scenes.json already exists (and --regen-scenes
    # not set), the existing plan is reused — supports hand-edits between runs.
    scenes_for_script: list[dict] | None = None
    with pipe.stage("plan") as st:
        scenes_path = edit_dir / "scenes.json"
        override_path: Path | None = options.get("scene_plan_override")
        regen = bool(options.get("regen_scenes"))

        if override_path and override_path.exists():
            try:
                doc = json.loads(override_path.read_text(encoding="utf-8"))
                scenes_for_script = doc.get("scenes") or doc
                st.extra["plan_source"] = f"override:{override_path.name}"
            except Exception as e:
                st.extra["override_error"] = str(e)

        if scenes_for_script is None and scenes_path.exists() and not regen:
            try:
                doc = json.loads(scenes_path.read_text(encoding="utf-8"))
                scenes_for_script = doc.get("scenes") or doc
                st.extra["plan_source"] = "cached:edit/scenes.json"
            except Exception as e:
                st.extra["cache_error"] = str(e)

        if scenes_for_script is None:
            try:
                plan = run_scene_planner(project_dir, live_url)
                if plan and plan.get("scenes"):
                    scenes_for_script = plan["scenes"]
                    st.extra["plan_source"] = "scene_planner:llm"
                    st.extra["estimated_duration_s"] = plan.get("estimated_duration_s")
            except Exception as e:
                st.extra["planner_error"] = str(e)

        if scenes_for_script is None:
            st.extra["plan_source"] = "default"
            st.extra["scene_count"] = 0
        else:
            st.extra["scene_count"] = len(scenes_for_script)

    # ── 1b. SCRIPT ──────────────────────────────────────────────────────────
    with pipe.stage("script") as st:
        body, frontmatter = script_stage.draft_script(project_dir, scenes=scenes_for_script)
        script_path = script_stage.write_script(project_dir, body, frontmatter)
        st.output_size_bytes = script_path.stat().st_size
        word_count = len(body.split())
        st.extra["word_count"] = word_count
        st.extra["scene_aware"] = scenes_for_script is not None

    # ── 2. TTS ──────────────────────────────────────────────────────────────
    with pipe.stage("tts") as st:
        voice = (frontmatter.get("voice") if frontmatter else None) or options.get("voice") or default_voice()
        instructions = (frontmatter.get("instructions") if frontmatter else None) or options.get("instructions")
        audio_bytes, mime, used_provider, fallback_log = synthesize_with_fallback(
            body, voice=voice, instructions=instructions,
        )
        ext = ".mp3" if "mpeg" in mime else ".wav"
        voice_path = edit_dir / f"voiceover{ext}"
        voice_path.write_bytes(audio_bytes)
        st.output_size_bytes = len(audio_bytes)
        st.fallback_chain_walked = fallback_log
        st.extra["provider_used"] = used_provider
        st.extra["voice"] = voice
        st.extra["mime"] = mime

    # ── 3. WALKTHROUGH ──────────────────────────────────────────────────────
    # NOTE: project_dir intentionally NOT passed — the plan stage already ran
    # the LLM. The recorder will only consume the existing scenes.json (or
    # fall back to its DOM-hunt default plan if neither exists). This keeps
    # us at exactly one scene-planner LLM call per pipeline run.
    with pipe.stage("walkthrough") as st:
        scene_meta = record(
            live_url,
            edit_dir,
            max_seconds=options.get("max_walkthrough_s", 60),
            project_dir=None,
            scene_plan_override=options.get("scene_plan_override"),
            regen_scenes=False,
        )
        st.extra["scenes"] = scene_meta["scenes"]
        st.extra["plan_source"] = scene_meta.get("plan_source", "unknown")
        st.extra["walkthrough_duration_s"] = scene_meta["duration_s"]
        walkthrough_mp4 = edit_dir / "walkthrough.mp4"
        st.output_size_bytes = walkthrough_mp4.stat().st_size

    # ── 4. TRANSCRIBE (upstream helper, unchanged) ─────────────────────────
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(exist_ok=True)
    with pipe.stage("transcribe") as st:
        transcribe = _import_helper("transcribe")
        # transcribe.transcribe_one expects a video; voice_path is audio. Scribe
        # accepts audio uploads — call call_scribe directly with the audio file.
        # Falls back to running upstream's CLI which extracts via ffmpeg first.
        api_key = transcribe.load_api_key()
        # Extract a 16k mono wav from the voiceover so Scribe gets the same
        # input shape as upstream's CLI path.
        audio_for_scribe = edit_dir / "voiceover_16k.wav"
        import subprocess as sp
        sp.run([
            "ffmpeg", "-y", "-i", str(voice_path),
            "-ac", "1", "-ar", "16000", str(audio_for_scribe),
        ], check=True, stdout=sp.DEVNULL, stderr=sp.DEVNULL)

        transcript = transcribe.call_scribe(audio_for_scribe, api_key)
        transcript_path = transcripts_dir / "voiceover.json"
        transcript_path.write_text(json.dumps(transcript, indent=2))
        st.output_size_bytes = transcript_path.stat().st_size
        st.extra["word_count"] = sum(1 for w in transcript.get("words", []) if w.get("type") == "word")

    # ── 5. SRT + ASSEMBLE (honors upstream Hard Rules where they apply) ────
    with pipe.stage("assemble") as st:
        srt_path = edit_dir / "demo.srt"
        cue_count = build_master_srt(transcript_path, srt_path)
        st.extra["srt_cues"] = cue_count

        demo_path = edit_dir / "demo.mp4"
        chosen_strategy = options.get("tail_strategy") or cfg_tail_strategy()
        assemble_meta = assemble(
            edit_dir,
            walkthrough=walkthrough_mp4,
            voiceover=voice_path,
            srt=srt_path,
            out=demo_path,
            tail_strategy=chosen_strategy,
        )
        st.extra.update(assemble_meta or {})
        st.output_size_bytes = demo_path.stat().st_size

    # ── 6. OUTPUT ──────────────────────────────────────────────────────────
    with pipe.stage("output") as st:
        slug = _slug(project_dir)
        shared_dir = SHARED_MEDIA_ROOT / slug
        shared_dir.mkdir(parents=True, exist_ok=True)
        shared_demo = shared_dir / "demo.mp4"
        shutil.copy2(demo_path, shared_demo)

        # Extend metadata.json with demo_video_url
        meta_path = project_dir / "metadata.json"
        meta = _read_metadata(project_dir)
        meta["demo_video_url"] = f"https://clawdash.trollefsen.com/media/mvps/{slug}/demo.mp4"
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        st.extra["shared_path"] = str(shared_demo)
        st.extra["demo_video_url"] = meta["demo_video_url"]

    pipe.flush()
    return {
        "project": project_dir.name,
        "demo_path": str(demo_path),
        "shared_path": str(shared_dir / "demo.mp4"),
        "demo_video_url": meta["demo_video_url"],
        "stages": [s.stage_id for s in pipe.stages],
        "log_path": str(pipe.log_path),
    }


def _cli() -> None:
    p = argparse.ArgumentParser(description="Generate a demo video for a nightly-MVP project")
    p.add_argument("--project", required=True, type=Path, help="Path to ~/projects/nightly-mvps/<slug>/")
    p.add_argument("--voice", help="Override voice / profile id")
    p.add_argument("--instructions", help="Style/tone hint for providers that support it")
    p.add_argument("--max-walkthrough-s", type=int, default=60)
    p.add_argument("--scene-plan", type=Path, help="Hand-authored scenes.json to bypass the agent")
    p.add_argument("--regen-scenes", action="store_true", help="Force re-run scene_planner even if scenes.json exists")
    p.add_argument("--skip-preflight", action="store_true", help="Skip preflight check (debug only)")
    p.add_argument("--tail-strategy", choices=["hold", "loop", "trim_voice"], help="Override tail strategy from household.json")
    p.add_argument("--gantt", action="store_true", help="Print a Mermaid Gantt at end")
    args = p.parse_args()
    options = {k: v for k, v in {
        "voice": args.voice,
        "instructions": args.instructions,
        "max_walkthrough_s": args.max_walkthrough_s,
        "scene_plan_override": args.scene_plan,
        "regen_scenes": args.regen_scenes,
        "tail_strategy": args.tail_strategy,
        "skip_preflight": args.skip_preflight or None,
    }.items() if v is not None}
    out = run(args.project, options)
    print(json.dumps(out, indent=2))
    if args.gantt:
        # Re-load the just-written log to render the gantt.
        log = json.loads((args.project / "edit" / "pipeline.log.json").read_text())
        print()
        from ..observability import Pipeline
        # Cheap render — reconstruct just enough to call render_gantt.
        pipe = Pipeline(args.project)
        pipe.run_id = log.get("run_id", pipe.run_id)
        pipe.start_ts = log["start_ts"]
        for s in log["stages"]:
            from ..observability import StageRecord
            pipe.stages.append(StageRecord(**{k: s[k] for k in StageRecord.__dataclass_fields__ if k in s}))
        print(pipe.render_gantt())


if __name__ == "__main__":
    _cli()
