"""Contact sheet: one glance at every shot's current state.

Grid of each shot's best frame (video take's middle frame, else keyframe,
else grey slug) captioned with shot id + status. Output lands in edit/.

    python contact_sheet.py <production-name-or-dir> [--columns 4]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest  # noqa: E402
from build_animatic import ffmpeg  # noqa: E402

CELL = 384
CAPTION = 28


def best_frame(shot: dict, root: Path, tmp: Path) -> Image.Image | None:
    media = manifest.shot_media(shot, root)
    if not media:
        return None
    kind, path = media
    if kind == "still":
        return Image.open(path).convert("RGB")
    frame = tmp / f"{shot['id']}.png"
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-ss", "1.0",
                    "-i", str(path), "-frames:v", "1", "-update", "1",
                    str(frame)], check=False)
    if frame.is_file():
        return Image.open(frame).convert("RGB")
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("production")
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--out", default="edit/contact_sheet.png")
    args = parser.parse_args()

    m, root = manifest.load(args.production)
    shots = m.get("shots", [])
    if not shots:
        print("No shots in manifest.")
        return 1
    cols = max(1, args.columns)
    rows = (len(shots) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * CELL, rows * (CELL + CAPTION)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 16)
    except OSError:
        font = ImageFont.load_default()

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        for i, shot in enumerate(shots):
            x = (i % cols) * CELL
            y = (i // cols) * (CELL + CAPTION)
            img = best_frame(shot, root, tmp)
            if img:
                img.thumbnail((CELL, CELL))
                sheet.paste(img, (x + (CELL - img.width) // 2,
                                  y + (CELL - img.height) // 2))
            else:
                draw.rectangle([x + 4, y + 4, x + CELL - 4, y + CELL - 4],
                               outline=(90, 90, 90))
            take = manifest.best_take(shot)
            caption = f"{shot['id']}  {shot.get('status', '?')}" + (
                f"  t{take['n']:02d}" if take else "")
            draw.text((x + 8, y + CELL + 5), caption, fill=(230, 230, 230),
                      font=font)

    out_path = root / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    print(f"contact sheet: {out_path} ({len(shots)} shots)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
