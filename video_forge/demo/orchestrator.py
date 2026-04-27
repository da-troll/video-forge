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

    # ── 2b. LOUDNORM (two-pass) ────────────────────────────────────────────
    # Normalize voiceover to broadcast standard (-14 LUFS / -1 dBTP / 11 LU).
    # Runs before align so Whisper sees the normalized audio + downstream
    # assemble's filter chain doesn't need to add yet another loudnorm step.
    with pipe.stage("loudnorm") as st:
        from .loudnorm import apply_loudnorm_two_pass_audio
        normalized_path = voice_path.with_name(voice_path.stem + ".normalized" + voice_path.suffix)
        result = apply_loudnorm_two_pass_audio(voice_path, normalized_path)
        st.extra.update({
            "ok": result["ok"],
            "fallback_used": result.get("fallback_used", False),
            "target_lufs": result["target"]["I"],
            "measured_in_i": result["measured_in"]["input_i"] if result.get("measured_in") else None,
            "measured_in_tp": result["measured_in"]["input_tp"] if result.get("measured_in") else None,
            "measured_out_lufs": result.get("measured_out_lufs"),
        })
        if result["ok"] and normalized_path.exists():
            voice_path = normalized_path
            st.output_size_bytes = normalized_path.stat().st_size
        else:
            # Continue with un-normalized audio rather than failing the run.
            st.extra["error"] = result.get("error", "loudnorm produced no output")

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

    # ── 4. ALIGN (script-as-truth; Whisper-1 only for word boundaries) ────
    # Script-substitution forced alignment: Whisper provides word-level
    # timings; we substitute the actual script tokens. The emitted JSON
    # mirrors Scribe's shape so build_master_srt is unchanged.
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(exist_ok=True)
    transcript_path = transcripts_dir / "voiceover.json"
    with pipe.stage("align") as st:
        from .align import align_script_to_audio
        align_meta = align_script_to_audio(
            script_text=body,
            audio_path=voice_path,
            out_json_path=transcript_path,
        )
        st.output_size_bytes = transcript_path.stat().st_size
        st.extra.update(align_meta)

    # ── 5. SRT + ASSEMBLE (honors upstream Hard Rules where they apply) ────
    with pipe.stage("assemble") as st:
        srt_path = edit_dir / "demo.srt"
        srt_meta = build_master_srt(transcript_path, srt_path)
        # Spread all srt_meta fields so any new keys (overlap_clips_applied,
        # min_duration_stretches, etc.) land in pipeline.log.json automatically.
        st.extra["srt_cues"] = srt_meta.get("cue_count")
        st.extra["srt_canonicalizations"] = srt_meta.get("canonicalizations_applied", 0)
        st.extra["srt_overlap_clips"] = srt_meta.get("overlap_clips_applied", 0)
        st.extra["srt_min_duration_stretches"] = srt_meta.get("min_duration_stretches", 0)

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

    # ── 5b. ASSERTIONS — quality gate before publishing ─────────────────────
    # Cheap, no-LLM checks: file size, duration bounds, SRT integrity, lead
    # silence, tail drift, integrated LUFS. Failure raises and aborts the run
    # BEFORE the demo is copied to the public path or metadata.json is updated.
    with pipe.stage("assertions") as st:
        from .assertions import AssertionFailed, assert_demo_quality
        try:
            measurements = assert_demo_quality(
                demo_path=demo_path,
                srt_path=srt_path,
                tail_gap_s=(assemble_meta or {}).get("tail_gap_s"),
                voiceover_path=voice_path,
            )
            st.extra.update(measurements)
            st.extra["passed"] = True
        except AssertionFailed as e:
            st.extra["passed"] = False
            st.extra["failures"] = str(e)
            raise

    # ── 6. OUTPUT ──────────────────────────────────────────────────────────
    # Versioned filenames defeat browser/CDN cache ambiguity. Each run
    # writes demo-<run_id>.mp4 (run_id is the orchestrator's start
    # timestamp); metadata.demo_video_url points to the versioned file.
    # We also maintain a demo.mp4 copy as a "latest" pointer for tooling
    # that expects a stable name (gallery thumbnailing, etc.).
    with pipe.stage("output") as st:
        slug = _slug(project_dir)
        shared_dir = SHARED_MEDIA_ROOT / slug
        shared_dir.mkdir(parents=True, exist_ok=True)
        versioned_name = f"demo-{pipe.run_id}.mp4"
        shared_versioned = shared_dir / versioned_name
        shared_latest = shared_dir / "demo.mp4"
        shutil.copy2(demo_path, shared_versioned)
        shutil.copy2(demo_path, shared_latest)

        # demo_video_url points to the VERSIONED file so cache-busting is
        # automatic across runs.
        meta_path = project_dir / "metadata.json"
        meta = _read_metadata(project_dir)
        versioned_url = f"https://clawdash.trollefsen.com/media/mvps/{slug}/{versioned_name}"
        meta["demo_video_url"] = versioned_url
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        st.extra["shared_versioned_path"] = str(shared_versioned)
        st.extra["shared_latest_path"] = str(shared_latest)
        st.extra["demo_video_url"] = versioned_url
        st.extra["run_id"] = pipe.run_id

    pipe.flush()
    return {
        "project": project_dir.name,
        "demo_path": str(demo_path),
        "shared_path": str(shared_versioned),
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
