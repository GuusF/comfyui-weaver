"""Focused tests for graph.ui_to_api edge cases found in review.

Requires a running ComfyUI (core nodes) for /object_info.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from comfy_client import ComfyClient  # noqa: E402
from graph import ui_to_api  # noqa: E402

# LoadImage serializes list-form with extra upload-widget entries
# (["name.png", "image", ""]) — seen in real workflow files on this machine.
# SaveImage here uses dict-form widgets_values like VideoHelperSuite does.
UI_WORKFLOW = {
    "nodes": [
        {
            "id": 1, "type": "LoadImage", "mode": 0,
            "inputs": [], "outputs": [{"name": "IMAGE", "type": "IMAGE"}],
            "widgets_values": ["claude/smoke_test.png", "image", ""],
        },
        {
            "id": 2, "type": "SaveImage", "mode": 0,
            "inputs": [{"name": "images", "type": "IMAGE", "link": 10}],
            "outputs": [],
            "widgets_values": {"filename_prefix": "converter_test"},
        },
    ],
    "links": [[10, 1, 0, 2, 0, "IMAGE"]],
}


async def main() -> int:
    client = ComfyClient()
    info = await client.object_info()
    api = ui_to_api(UI_WORKFLOW, info)
    await client.close()

    checks = {
        "LoadImage image value survives extra upload entries":
            api.get("1", {}).get("inputs", {}).get("image")
            == "claude/smoke_test.png",
        "dict widgets_values mapped by name":
            api.get("2", {}).get("inputs", {}).get("filename_prefix")
            == "converter_test",
        "connection wired":
            api.get("2", {}).get("inputs", {}).get("images") == ["1", 0],
    }
    failed = [name for name, ok in checks.items() if not ok]
    for name, ok in checks.items():
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if failed:
        print(f"converted graph: {api}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
