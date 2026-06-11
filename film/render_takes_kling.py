"""Render video takes for boarded shots via Kling on Comfy Cloud (PAID, ~EUR 1/5s).

Run only against a signed render order. For each shot with a keyframe and no
take (or listed in --retake): upload the newest board as the start frame,
run KlingImage2VideoNode with the shot's motion_prompt, download the result
as takes/<id>_tNN.mp4, and append a take record to the manifest (saved after
every shot — crash-safe).

    python render_takes_kling.py <production> [--only ids] [--retake ids] [--mode std|pro]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "server"))

import manifest  # noqa: E402
from cloud_client import SUCCESS_STATES, CloudComfyClient  # noqa: E402

MODEL = "kling-v2-master"


def kling_workflow(image_name: str, motion_prompt: str, negative: str,
                   duration_s: int, mode: str, prefix: str) -> dict:
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "2": {"class_type": "KlingImage2VideoNode",
              "inputs": {"start_frame": ["1", 0],
                         "prompt": motion_prompt,
                         "negative_prompt": negative,
                         "model_name": MODEL,
                         "cfg_scale": 0.8,
                         "mode": mode,
                         "aspect_ratio": "16:9",
                         "duration": str(10 if duration_s > 5 else 5)}},
        "3": {"class_type": "SaveVideo",
              "inputs": {"video": ["2", 0], "filename_prefix": prefix,
                         "format": "auto", "codec": "auto"}},
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("production")
    parser.add_argument("--only", default="")
    parser.add_argument("--retake", default="")
    parser.add_argument("--mode", default="std", choices=["std", "pro"])
    args = parser.parse_args()

    m, root = manifest.load(args.production)
    negative = m.get("style_bible", {}).get("negative", "")
    only = {s for s in args.only.split(",") if s}
    retake = {s for s in args.retake.split(",") if s}

    client = CloudComfyClient()
    rendered, est_cost = 0, 0.0
    try:
        for shot in m.get("shots", []):
            sid = shot["id"]
            if only and sid not in only:
                continue
            if shot.get("takes") and sid not in retake and sid not in only:
                continue
            keys = shot.get("keyframes") or []
            if not keys:
                print(f"SKIP {sid}: no keyframe", flush=True)
                continue
            motion = shot.get("motion_prompt") or shot.get("action", "")
            board = root / keys[-1]
            take_n = (shot["takes"][-1]["n"] + 1) if shot.get("takes") else 1
            duration = int(shot.get("duration_s", 5))
            cost = 2.0 if duration > 5 else 1.0
            print(f"-> {sid} t{take_n:02d} ({duration}s, ~EUR {cost:.0f}) "
                  f"from {keys[-1]}", flush=True)

            up = await client.upload_image(board.read_bytes(), board.name,
                                           overwrite=True)
            wf = kling_workflow(up.get("name", board.name), motion, negative,
                                duration, args.mode,
                                f"claude/{m['production']}/takes/{sid}")
            sub = await client.submit_prompt(wf)
            if sub.get("error"):
                print(f"   SUBMIT FAILED: {str(sub)[:400]}", flush=True)
                continue
            pid = sub["prompt_id"]
            print(f"   submitted {pid}, waiting...", flush=True)
            try:
                details = await client.wait_for_job(pid, timeout_s=1200,
                                                    poll_interval=6.0)
            except TimeoutError as exc:
                print(f"   TIMEOUT: {exc}", flush=True)
                continue
            if details.get("_status") not in SUCCESS_STATES:
                print(f"   FAILED: {details.get('_status')}", flush=True)
                continue

            outputs = details.get("outputs")
            if not isinstance(outputs, dict):
                outputs = {k: v for k, v in details.items() if isinstance(v, dict)}
            saved_rel = None
            for node_out in (outputs or {}).values():
                if not isinstance(node_out, dict):
                    continue
                for items in node_out.values():
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if isinstance(item, dict) and "filename" in item:
                            data = await client.download_view(
                                item["filename"], item.get("subfolder", ""),
                                item.get("type", "output"))
                            rel = f"takes/{sid}_t{take_n:02d}.mp4"
                            (root / rel).parent.mkdir(parents=True, exist_ok=True)
                            (root / rel).write_bytes(data)
                            saved_rel = rel
            if not saved_rel:
                print("   no output file found", flush=True)
                continue
            shot.setdefault("takes", []).append({
                "n": take_n, "engine": "kling", "model": MODEL,
                "mode": args.mode, "duration_s": duration,
                "start_frame": keys[-1], "prompt": motion,
                "path": saved_rel, "prompt_id": pid,
                "cost_eur": cost, "approved": False, "notes": "",
            })
            shot["status"] = "animated"
            manifest.save(m, root)
            rendered += 1
            est_cost += cost
            print(f"   saved {saved_rel}", flush=True)
    finally:
        await client.close()
    print(f"done: {rendered} take(s), estimated EUR {est_cost:.0f}", flush=True)
    return 0 if rendered else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
