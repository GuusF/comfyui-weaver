"""Production manifest: the single source of truth for a film project.

A production lives in output/claude/productions/<name>/ :

    production.json     this manifest
    script/             treatment, screenplay, beat sheet (markdown/fountain)
    boards/             keyframe stills per shot (sc010_sh010_k1_t01.png)
    takes/              generated video takes (sc010_sh010_t01.mp4)
    edit/               animatic.mp4, EDL, contact sheets
    audio/              temp audio, generated audio stems

Manifest shape (production.json):

    {
      "production": "robot_painter",
      "fps": 24,
      "resolution": "1024x1024",
      "style_bible": {
        "look": "soft window light, shallow depth of field, 85mm",
        "negative": "blur, warping, text, watermark",
        "palette": "warm naturals",
        "seed_bank": {"hero_robot": 42}
      },
      "shots": [
        {
          "id": "sc010_sh010",
          "duration_s": 5,
          "action": "Robot dips brush, begins painting",
          "camera": "slow push-in",
          "dialogue": "",
          "prompt": "",                  # final still/video prompt
          "keyframes": ["boards/sc010_sh010_k1_t01.png"],
          "takes": [
            {"n": 1, "engine": "kling", "model": "kling-v2-master",
             "seed": 42, "path": "takes/sc010_sh010_t01.mp4",
             "cost_eur": 1.0, "approved": false, "notes": ""}
          ],
          "status": "scripted|boarded|animated|approved"
        }
      ]
    }

Every take records prompt/seed/model so any shot can be re-rendered or
varied reproducibly. Paths are relative to the production directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DATA_DIR = Path(
    os.environ.get("COMFY_DATA_DIR") or Path(__file__).resolve().parents[2]
)
PRODUCTIONS_ROOT = _DATA_DIR / "output" / "claude" / "productions"

SUBDIRS = ("script", "boards", "takes", "edit", "audio")
STATUSES = ("scripted", "boarded", "animated", "approved")


def production_dir(name: str) -> Path:
    return PRODUCTIONS_ROOT / name


def load(name_or_dir: str | Path) -> tuple[dict, Path]:
    root = Path(name_or_dir)
    if not root.is_absolute():
        root = production_dir(str(name_or_dir))
    manifest_path = root / "production.json"
    with open(manifest_path, encoding="utf-8") as fh:
        return json.load(fh), root


def save(manifest: dict, root: Path) -> None:
    with open(root / "production.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, ensure_ascii=False)


def scaffold(name: str, fps: int = 24, resolution: str = "1024x1024") -> Path:
    root = production_dir(name)
    for sub in SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    manifest_path = root / "production.json"
    if not manifest_path.exists():
        save({"production": name, "fps": fps, "resolution": resolution,
              "style_bible": {"look": "", "negative": "", "palette": "",
                              "seed_bank": {}},
              "shots": []}, root)
    return root


def best_take(shot: dict) -> dict | None:
    takes = shot.get("takes") or []
    approved = [t for t in takes if t.get("approved")]
    if approved:
        return approved[-1]
    return takes[-1] if takes else None


def shot_media(shot: dict, root: Path) -> tuple[str, Path] | None:
    """(kind, absolute path) of the best media for a shot: video > keyframe."""
    take = best_take(shot)
    if take and (root / take["path"]).is_file():
        return "video", root / take["path"]
    for key in reversed(shot.get("keyframes") or []):
        if (root / key).is_file():
            return "still", root / key
    return None
