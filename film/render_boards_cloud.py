"""Render missing storyboard keyframes on Comfy Cloud flux1-dev (paid: cents/board).

For each manifest shot without keyframes: build a flux-dev graph from the
shot's assembled prompt + seed, submit, wait, download to boards/<id>_k1_tNN.png,
and record provenance in the shot's board_meta. Run only after the user
approved cloud boards.

    python render_boards_cloud.py <production> [--only sc010_sh010,...] [--retake sc010_sh010,...]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "server"))

import manifest  # noqa: E402
from cloud_client import SUCCESS_STATES, CloudComfyClient  # noqa: E402

MODEL = "flux1-dev-fp8.safetensors"
GUIDANCE = 3.5
STEPS = 20


def board_workflow(prompt: str, seed: int, width: int, height: int,
                   prefix: str) -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": MODEL}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "FluxGuidance",
              "inputs": {"conditioning": ["2", 0], "guidance": GUIDANCE}},
        "4": {"class_type": "ConditioningZeroOut",
              "inputs": {"conditioning": ["2", 0]}},
        "5": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["3", 0],
                         "negative": ["4", 0], "latent_image": ["5", 0],
                         "seed": seed, "steps": STEPS, "cfg": 1.0,
                         "sampler_name": "euler", "scheduler": "simple",
                         "denoise": 1.0}},
        "7": {"class_type": "VAEDecode",
              "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveImage",
              "inputs": {"images": ["7", 0], "filename_prefix": prefix}},
    }


def next_take_number(shot: dict) -> int:
    best = 0
    for key in shot.get("keyframes") or []:
        match = re.search(r"_t(\d+)\.png$", key)
        if match:
            best = max(best, int(match.group(1)))
    return best + 1


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("production")
    parser.add_argument("--only", default="",
                        help="comma-separated shot ids to render")
    parser.add_argument("--retake", default="",
                        help="comma-separated shot ids to re-render even if boarded")
    args = parser.parse_args()

    m, root = manifest.load(args.production)
    width, height = (int(x) for x in m.get("resolution", "1024x576").split("x"))
    only = {s for s in args.only.split(",") if s}
    retake = {s for s in args.retake.split(",") if s}

    client = CloudComfyClient()
    rendered = 0
    try:
        for shot in m.get("shots", []):
            sid = shot["id"]
            if only and sid not in only:
                continue
            if shot.get("keyframes") and sid not in retake and sid not in only:
                continue
            prompt = shot.get("prompt", "")
            if not prompt:
                print(f"SKIP {sid}: no prompt")
                continue
            take_n = next_take_number(shot)
            seed = int(shot.get("seed", 0)) + (take_n - 1) * 1000
            prefix = f"claude/{m['production']}/boards/{sid}"
            print(f"-> {sid} (t{take_n:02d}, seed {seed})")

            sub = await client.submit_prompt(
                board_workflow(prompt, seed, width, height, prefix))
            if sub.get("error"):
                print(f"   SUBMIT FAILED: {str(sub)[:400]}")
                continue
            pid = sub["prompt_id"]
            details = await client.wait_for_job(pid, timeout_s=420,
                                                poll_interval=3.0)
            if details.get("_status") not in SUCCESS_STATES:
                print(f"   FAILED: {details.get('_status')}")
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
                            rel = f"boards/{sid}_k1_t{take_n:02d}.png"
                            (root / rel).parent.mkdir(parents=True, exist_ok=True)
                            (root / rel).write_bytes(data)
                            saved_rel = rel
            if not saved_rel:
                print("   no output file found")
                continue
            shot.setdefault("keyframes", []).append(saved_rel)
            shot["status"] = "boarded"
            shot.setdefault("board_meta", []).append({
                "take": take_n, "engine": "cloud-flux-dev", "model": MODEL,
                "seed": seed, "steps": STEPS, "guidance": GUIDANCE,
                "prompt_id": pid,
            })
            manifest.save(m, root)  # save after every shot — crash-safe
            rendered += 1
            print(f"   saved {saved_rel}")
    finally:
        await client.close()
    print(f"done: {rendered} board(s) rendered")
    return 0 if rendered else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
