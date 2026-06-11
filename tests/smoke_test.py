"""End-to-end smoke test for the ComfyUI MCP integration.

Phases:
  1. imports     — all server modules import cleanly.
  2. stdio       — the MCP server speaks clean JSON-RPC over stdio
                   (initialize + tools/list), i.e. no stdout pollution.
  3. live        — against a running ComfyUI: status, object_info, models,
                   upload a generated test image, run the passthrough
                   template (no GPU), verify the output file exists in
                   output/claude/, and confirm the queue was not cleared.

Usage:
  python tests/smoke_test.py            # all phases (live skipped if no server)
  python tests/smoke_test.py --offline  # phases 1-2 only
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG / "server"))

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def phase_imports() -> bool:
    try:
        import comfy_client  # noqa: F401
        import graph  # noqa: F401
        import templates  # noqa: F401
        import tracking  # noqa: F401
        report("imports", True)
        return True
    except Exception as exc:  # noqa: BLE001
        report("imports", False, repr(exc))
        return False


def phase_stdio() -> bool:
    """Spawn the MCP server and run a minimal JSON-RPC handshake."""
    server = PKG / "server" / "comfy_mcp_server.py"
    proc = subprocess.Popen(
        [sys.executable, str(server)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    def send(obj: dict) -> None:
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        proc.stdin.flush()

    def recv(timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            return json.loads(line)
        raise TimeoutError("no response from MCP server")

    try:
        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "0"},
            },
        })
        init = recv()
        ok = "result" in init and init["result"].get("serverInfo", {}).get("name") == "comfyui"
        report("stdio: initialize", ok, json.dumps(init)[:120] if not ok else "")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_resp = recv()
        tools = tools_resp.get("result", {}).get("tools", [])
        names = sorted(t["name"] for t in tools)
        ok2 = len(tools) >= 15 and "run_workflow" in names and "comfy_status" in names
        report("stdio: tools/list", ok2, f"{len(tools)} tools")

        # Real tools/call round-trip: plain-dict result path.
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "system_paths", "arguments": {}}})
        paths_resp = recv()
        ok3 = (not paths_resp.get("result", {}).get("isError")
               and "result" in paths_resp)
        report("stdio: tools/call system_paths", ok3,
               "" if ok3 else json.dumps(paths_resp)[:200])

        # Image-returning path (this is where structured-output bugs hide):
        # view_output on a locally generated PNG needs no ComfyUI server.
        from PIL import Image as PILImage
        test_png = PKG / "state" / "stdio_view_test.png"
        test_png.parent.mkdir(parents=True, exist_ok=True)
        PILImage.new("RGB", (32, 32), (200, 60, 60)).save(test_png)
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "view_output",
                         "arguments": {"path_or_filename": str(test_png)}}})
        view_resp = recv()
        content = view_resp.get("result", {}).get("content", [])
        has_image = any(c.get("type") == "image" for c in content)
        ok4 = not view_resp.get("result", {}).get("isError") and has_image
        report("stdio: tools/call view_output returns image", ok4,
               "" if ok4 else json.dumps(view_resp)[:300])
        test_png.unlink(missing_ok=True)

        return ok and ok2 and ok3 and ok4
    except Exception as exc:  # noqa: BLE001
        report("stdio handshake", False, repr(exc))
        return False
    finally:
        proc.kill()


async def phase_live() -> bool:
    from comfy_client import ComfyClient, ComfyUnavailable
    import templates as tpl
    from graph import namespace_outputs

    client = ComfyClient()
    try:
        base = await client.base_url()
    except ComfyUnavailable as exc:
        report("live: server detection", False, str(exc))
        print("    (start ComfyUI and re-run for the live phase)")
        return False
    report("live: server detection", True, base)

    stats = await client.system_stats()
    report("live: system_stats", "system" in stats,
           stats.get("system", {}).get("comfyui_version", "?"))

    info = await client.object_info()
    report("live: object_info", len(info) > 100, f"{len(info)} node types")

    folders = await client.model_folders()
    report("live: model folders", isinstance(folders, list) and len(folders) > 0,
           f"{len(folders)} folders")

    queue_before = await client.queue()
    pending_before = {i[1] for i in queue_before.get("queue_pending", []) if len(i) > 1}

    # Generate and upload a tiny test image.
    from PIL import Image as PILImage
    buf = io.BytesIO()
    PILImage.new("RGB", (64, 64), (40, 120, 200)).save(buf, format="PNG")
    up = await client.upload_image(buf.getvalue(), "smoke_test.png",
                                   subfolder="claude", overwrite=True)
    stored = f"{up.get('subfolder', 'claude')}/{up.get('name', 'smoke_test.png')}"
    report("live: upload_input", "name" in up, stored)

    # Run the passthrough template and wait.
    template = tpl.load_template("passthrough_test")
    wf = tpl.apply_template(template, {"image": stored,
                                       "filename_prefix": "smoke"})
    namespace_outputs(wf)
    sub = await client.submit_prompt(wf)
    pid = sub.get("prompt_id")
    report("live: submit prompt", bool(pid), str(sub.get("node_errors") or pid))
    if not pid:
        await client.close()
        return False
    entry = await client.wait_for_prompt(pid, timeout_s=120)
    outputs = entry.get("outputs", {})
    files = [
        item for node in outputs.values() if isinstance(node, dict)
        for items in node.values() if isinstance(items, list)
        for item in items if isinstance(item, dict) and "filename" in item
    ]
    report("live: execution finished", bool(files), f"{len(files)} file(s)")

    ok_path = False
    out_dir = PKG.parent / "output"
    for f in files:
        p = out_dir / f.get("subfolder", "") / f["filename"]
        if p.is_file() and f.get("subfolder", "").startswith("claude"):
            ok_path = True
            report("live: output namespaced under output/claude/", True, str(p))
            break
    if not ok_path:
        report("live: output namespaced under output/claude/", False,
               json.dumps(files)[:200])

    # A pending job may legitimately have started running or finished while
    # we waited — only an id that vanished from queue AND history was deleted.
    queue_after = await client.queue()
    pending_after = {i[1] for i in queue_after.get("queue_pending", []) if len(i) > 1}
    running_after = {i[1] for i in queue_after.get("queue_running", []) if len(i) > 1}
    history_after = set(await client.history(max_items=64))
    accounted = pending_after | running_after | history_after | {pid}
    report("live: user's queue untouched", pending_before <= accounted,
           f"{len(pending_before)} pending before, {len(pending_after)} after")

    await client.close()
    return all(r[0] == PASS for r in results if r[1].startswith("live"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true",
                        help="skip the live-server phase")
    args = parser.parse_args()

    ok = phase_imports()
    ok = phase_stdio() and ok
    if not args.offline:
        ok = asyncio.run(phase_live()) and ok

    print()
    failed = [r for r in results if r[0] == FAIL]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
