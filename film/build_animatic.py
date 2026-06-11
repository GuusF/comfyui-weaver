"""Build an animatic from the production manifest.

Works at every stage of completeness: shots with an approved/latest video
take use the video; shots with only a board keyframe hold the still for the
shot's duration; shots with nothing get a black slug. Each clip gets a
burned-in slate (shot id + take/status) so dailies are self-identifying.

    python build_animatic.py <production-name-or-dir> [--out edit/animatic.mp4]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest  # noqa: E402

FFMPEG = (Path(__file__).resolve().parents[2] / ".venv" / "Lib" / "site-packages"
          / "imageio_ffmpeg" / "binaries" / "ffmpeg-win-x86_64-v7.1.exe")
FONT = "C\\:/Windows/Fonts/consola.ttf"


def ffmpeg() -> str:
    if FFMPEG.is_file():
        return str(FFMPEG)
    return "ffmpeg"  # PATH fallback


def drawtext(label: str) -> str:
    safe = label.replace(":", "\\:").replace("'", "")
    return (f"drawtext=fontfile='{FONT}':text='{safe}':x=16:y=h-th-12:"
            f"fontsize=24:fontcolor=white:box=1:boxcolor=black@0.45:boxborderw=8")


def normalize_clip(src: Path, dst: Path, width: int, height: int, fps: int,
                   label: str, hold_seconds: float | None = None) -> None:
    """Re-encode any source (video or still) to uniform size/fps with a slate."""
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
          f"fps={fps},{drawtext(label)}")
    cmd = [ffmpeg(), "-y", "-loglevel", "error"]
    if hold_seconds is not None:
        cmd += ["-loop", "1", "-t", f"{hold_seconds:.3f}"]
    cmd += ["-i", str(src), "-vf", vf, "-an",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            str(dst)]
    subprocess.run(cmd, check=True)


def black_slug(dst: Path, width: int, height: int, fps: int,
               seconds: float, label: str) -> None:
    cmd = [ffmpeg(), "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}",
           "-t", f"{seconds:.3f}", "-vf", drawtext(label), "-an",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dst)]
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("production")
    parser.add_argument("--out", default="edit/animatic.mp4")
    args = parser.parse_args()

    m, root = manifest.load(args.production)
    fps = int(m.get("fps", 24))
    res = m.get("resolution", "1024x1024")
    width, height = (int(x) for x in res.split("x"))
    shots = m.get("shots", [])
    if not shots:
        print("No shots in manifest.")
        return 1

    out_path = root / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=root / "edit") as tmp:
        tmp_dir = Path(tmp)
        parts = []
        for i, shot in enumerate(shots):
            media = manifest.shot_media(shot, root)
            take = manifest.best_take(shot)
            label = f"{shot['id']}  " + (
                f"t{take['n']:02d} {shot.get('status', '')}" if take
                else shot.get("status", "boarded"))
            part = tmp_dir / f"{i:03d}.mp4"
            duration = float(shot.get("duration_s", 4))
            if media and media[0] == "video":
                normalize_clip(media[1], part, width, height, fps, label)
            elif media:
                normalize_clip(media[1], part, width, height, fps, label,
                               hold_seconds=duration)
            else:
                black_slug(part, width, height, fps, duration,
                           label + "  MISSING")
            parts.append(part)
            print(f"  [{i + 1}/{len(shots)}] {shot['id']} <- "
                  f"{media[0] if media else 'slug'}")

        concat_list = tmp_dir / "concat.txt"
        concat_list.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="ascii")
        subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-f", "concat",
                        "-safe", "0", "-i", str(concat_list), "-c", "copy",
                        str(out_path)], check=True)

    print(f"animatic: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
