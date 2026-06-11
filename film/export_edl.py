"""Export a CMX3600 EDL of the current cut for DaVinci Resolve.

Each shot becomes one event; source clips are the shots' best takes. Import
in Resolve via File > Import > Timeline, then relink to the production's
takes/ folder. Shots without video yet are skipped (listed at the end).

    python export_edl.py <production-name-or-dir> [--out edit/cut.edl]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest  # noqa: E402
from build_animatic import ffmpeg  # noqa: E402


def probe_duration(path: Path) -> float | None:
    """Clip duration in seconds via ffmpeg (no ffprobe in imageio bundle)."""
    proc = subprocess.run([ffmpeg(), "-i", str(path)], capture_output=True,
                          text=True)
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            stamp = line.split("Duration:", 1)[1].split(",")[0].strip()
            try:
                h, m, s = stamp.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            except ValueError:
                return None
    return None


def tc(frames: int, fps: int) -> str:
    f = frames % fps
    s = (frames // fps) % 60
    m = (frames // (fps * 60)) % 60
    h = frames // (fps * 3600)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("production")
    parser.add_argument("--out", default="edit/cut.edl")
    args = parser.parse_args()

    m, root = manifest.load(args.production)
    fps = int(m.get("fps", 24))
    out_path = root / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"TITLE: {m['production'].upper()}_CUT", "FCM: NON-DROP FRAME", ""]
    record = fps * 3600  # start record TC at 01:00:00:00
    event = 0
    skipped = []
    for shot in m.get("shots", []):
        take = manifest.best_take(shot)
        src = root / take["path"] if take else None
        if not (src and src.is_file()):
            skipped.append(shot["id"])
            continue
        seconds = probe_duration(src) or float(shot.get("duration_s", 4))
        frames = max(1, round(seconds * fps))
        event += 1
        lines.append(
            f"{event:03d}  AX       V     C        "
            f"{tc(0, fps)} {tc(frames, fps)} "
            f"{tc(record, fps)} {tc(record + frames, fps)}")
        lines.append(f"* FROM CLIP NAME: {src.name}")
        lines.append(f"* COMMENT: {shot['id']} take {take['n']}")
        lines.append("")
        record += frames

    out_path.write_text("\n".join(lines), encoding="ascii", errors="replace")
    print(f"EDL: {out_path} ({event} events @ {fps}fps)")
    if skipped:
        print(f"skipped (no video yet): {', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
