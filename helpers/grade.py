"""Apply a color grade to a video via ffmpeg filter chain.

Ships with a proven `warm_cinematic` preset from HEURISTICS — the exact
chain that shipped a real launch video. For custom grades, pass
`--filter '<raw ffmpeg filter string>'`.

Usage:
    python helpers/grade.py <input> -o <output>
    python helpers/grade.py <input> -o <output> --preset warm_cinematic
    python helpers/grade.py <input> -o <output> --filter 'eq=contrast=1.1,curves=preset=vintage'
    python helpers/grade.py --print-preset warm_cinematic    # print filter string only

Can also be imported by render.py which embeds the filter string into
per-segment extract commands. See `get_preset(name)`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PRESETS: dict[str, str] = {
    # From HEURISTICS §6. Shipped in skills_launch_v3.mp4.
    # +12% contrast, crushed blacks, -12% sat (retro/terminal feel)
    # warm shadows+mids, cool highs (subtle teal/orange split)
    # master S-curve for filmic snap
    "warm_cinematic": (
        "eq=contrast=1.12:brightness=-0.02:saturation=0.88,"
        "colorbalance="
        "rs=0.02:gs=0.0:bs=-0.03:"
        "rm=0.04:gm=0.01:bm=-0.02:"
        "rh=0.08:gh=0.02:bh=-0.05,"
        "curves=master='0/0 0.25/0.22 0.75/0.78 1/1'"
    ),
    # Minimal corrective grade: contrast bump + subtle S-curve, no color shifts.
    "neutral_punch": (
        "eq=contrast=1.08:brightness=0.0:saturation=1.0,"
        "curves=master='0/0 0.25/0.22 0.75/0.78 1/1'"
    ),
    # Flat — no grade. Useful as a sentinel for "skip grading this source".
    "none": "",
}


def get_preset(name: str) -> str:
    """Return the ffmpeg filter string for a preset name. Empty string for 'none'."""
    if name not in PRESETS:
        raise KeyError(
            f"unknown preset '{name}'. Available: {', '.join(sorted(PRESETS))}"
        )
    return PRESETS[name]


def apply_grade(input_path: Path, output_path: Path, filter_string: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not filter_string:
        # No grade — straight re-encode for consistency, or just copy.
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-c", "copy", str(output_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-vf", filter_string,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply a color grade via ffmpeg filter chain")
    ap.add_argument("input", type=Path, nargs="?", help="Input video")
    ap.add_argument("-o", "--output", type=Path, help="Output video")
    ap.add_argument(
        "--preset",
        type=str,
        default="warm_cinematic",
        choices=list(PRESETS.keys()),
        help="Grade preset (default: warm_cinematic)",
    )
    ap.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Raw ffmpeg filter string. Overrides --preset.",
    )
    ap.add_argument(
        "--print-preset",
        type=str,
        default=None,
        help="Print the filter string for a preset and exit. No input/output needed.",
    )
    ap.add_argument(
        "--list-presets",
        action="store_true",
        help="List available presets and exit.",
    )
    args = ap.parse_args()

    if args.list_presets:
        for name, f in PRESETS.items():
            print(f"{name}:")
            print(f"  {f}" if f else "  (no filter)")
            print()
        return

    if args.print_preset is not None:
        print(get_preset(args.print_preset))
        return

    if not args.input or not args.output:
        ap.error("input and -o/--output are required unless using --print-preset or --list-presets")

    if not args.input.exists():
        sys.exit(f"input not found: {args.input}")

    filter_string = args.filter if args.filter is not None else get_preset(args.preset)

    print(f"grading {args.input.name} → {args.output.name}")
    if filter_string:
        print(f"  filter: {filter_string[:100]}{'...' if len(filter_string) > 100 else ''}")
    else:
        print("  filter: (none — copy)")

    apply_grade(args.input, args.output, filter_string)
    print(f"done: {args.output}")


if __name__ == "__main__":
    main()
