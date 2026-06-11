"""Dailies QC strips: first/mid/last frame of each shot's latest take, hstacked.

    python make_dailies.py <production>

Writes edit/dailies/<shot>_tNN_strip.png for every shot with a take.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest  # noqa: E402
from build_animatic import ffmpeg  # noqa: E402
from export_edl import probe_duration  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("production")
    args = parser.parse_args()

    m, root = manifest.load(args.production)
    out_dir = root / "edit" / "dailies"
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for shot in m.get("shots", []):
        take = manifest.best_take(shot)
        if not take:
            continue
        src = root / take["path"]
        if not src.is_file():
            continue
        seconds = probe_duration(src) or float(shot.get("duration_s", 5))
        stamps = [0.15, seconds / 2, max(0.2, seconds - 0.3)]
        strip = out_dir / f"{shot['id']}_t{take['n']:02d}_strip.png"
        inputs, filters = [], []
        for i, t in enumerate(stamps):
            inputs += ["-ss", f"{t:.2f}", "-i", str(src)]
            filters.append(f"[{i}:v]select=eq(n\\,0),scale=400:-1[f{i}]")
        filtergraph = ";".join(filters) + \
            f";[f0][f1][f2]hstack=inputs=3[out]"
        subprocess.run([ffmpeg(), "-y", "-loglevel", "error", *inputs,
                        "-filter_complex", filtergraph, "-map", "[out]",
                        "-frames:v", "1", str(strip)], check=True)
        made.append(strip.name)
        print(f"  {strip.name}")
    print(f"dailies strips: {len(made)} in {out_dir}")
    return 0 if made else 1


if __name__ == "__main__":
    sys.exit(main())
