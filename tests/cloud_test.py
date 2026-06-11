"""Comfy Cloud connectivity test.

    python tests/cloud_test.py            # auth + catalog checks (free)
    python tests/cloud_test.py --render   # ALSO runs a tiny render (credits!)

Requires the API key in state/cloud_api_key.txt or
COMFY_CLOUD_API_KEY.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from cloud_client import CloudApiError, CloudComfyClient, CloudKeyMissing  # noqa: E402


async def main(render: bool) -> int:
    client = CloudComfyClient()
    try:
        user = await client.user()
        print(f"[PASS] auth — account: {user}")
    except (CloudKeyMissing, CloudApiError) as exc:
        print(f"[FAIL] auth — {exc}")
        await client.close()
        return 1

    info = await client.object_info()
    print(f"[PASS] object_info — {len(info)} node types on cloud")

    ckpt_schema = info.get("CheckpointLoaderSimple", {})
    options = (ckpt_schema.get("input", {}).get("required", {})
               .get("ckpt_name", [[]]))[0]
    flux = [o for o in options if "flux" in o.lower()]
    print(f"[INFO] cloud checkpoints: {len(options)} total, flux-like: {flux[:5]}")

    q = await client.queue()
    print(f"[PASS] queue — running={len(q.get('queue_running', []))} "
          f"pending={len(q.get('queue_pending', []))}")

    if render:
        if not flux:
            print("[SKIP] render — no flux checkpoint found on cloud")
        else:
            wf = {
                "1": {"class_type": "CheckpointLoaderSimple",
                      "inputs": {"ckpt_name": flux[0]}},
                "2": {"class_type": "CLIPTextEncode",
                      "inputs": {"text": "a tiny robot painting a sunflower",
                                 "clip": ["1", 1]}},
                "3": {"class_type": "ConditioningZeroOut",
                      "inputs": {"conditioning": ["2", 0]}},
                "4": {"class_type": "EmptySD3LatentImage",
                      "inputs": {"width": 768, "height": 768, "batch_size": 1}},
                "5": {"class_type": "KSampler",
                      "inputs": {"model": ["1", 0], "positive": ["2", 0],
                                 "negative": ["3", 0], "latent_image": ["4", 0],
                                 "seed": 42, "steps": 4, "cfg": 1.0,
                                 "sampler_name": "euler", "scheduler": "simple",
                                 "denoise": 1.0}},
                "6": {"class_type": "VAEDecode",
                      "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
                "7": {"class_type": "SaveImage",
                      "inputs": {"images": ["6", 0],
                                 "filename_prefix": "claude/cloud_test"}},
            }
            sub = await client.submit_prompt(wf)
            pid = sub.get("prompt_id")
            if not pid:
                print(f"[FAIL] submit — {sub}")
                await client.close()
                return 1
            print(f"[PASS] submitted {pid} — waiting...")
            details = await client.wait_for_job(pid, timeout_s=600)
            from cloud_client import SUCCESS_STATES
            print(f"[{'PASS' if details.get('_status') in SUCCESS_STATES else 'FAIL'}] "
                  f"render — status: {details.get('_status')}")

    await client.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--render", action="store_true",
                        help="run a small test render (consumes credits)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.render)))
