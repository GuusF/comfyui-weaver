"""ComfyUI MCP server for Claude Code (stdio transport).

Attaches to the running ComfyUI Desktop backend (auto-detects port 8000/8188,
override with COMFYUI_URL) and exposes tools for discovering nodes/models,
running workflows, tracking jobs, and inspecting outputs.

Non-disruption contract (the user keeps working while Claude does):
- never starts/stops/restarts ComfyUI;
- jobs always queue at the back, never the front;
- never clears the queue or history;
- outputs are namespaced under output/claude/, uploads under input/claude/;
- cancel only touches jobs this server submitted, unless force=true;
- VRAM is freed only on an explicit free_vram call.

stdio rule: stdout belongs to the MCP protocol. All logging goes to stderr
and a log file — never print().
"""
from __future__ import annotations

import base64
import io
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

import graph
import templates as tpl
import tracking
import cloud_client as cloud_client_module
from cloud_client import CloudApiError, CloudComfyClient, CloudKeyMissing
from comfy_client import ComfyApiError, ComfyClient, ComfyUnavailable

# --------------------------------------------------------------------- setup

# ComfyUI data directory (models/, input/, output/, user/). Set COMFY_DATA_DIR
# when this package lives outside it; the default assumes it sits inside.
COMFY_DATA_DIR = Path(
    os.environ.get("COMFY_DATA_DIR") or Path(__file__).resolve().parents[2]
)
OUTPUT_DIR = COMFY_DATA_DIR / "output"
INPUT_DIR = COMFY_DATA_DIR / "input"
WORKFLOWS_DIR = COMFY_DATA_DIR / "user" / "default" / "workflows"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"

STATE_DIR.mkdir(parents=True, exist_ok=True)
_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
try:
    _handlers.append(
        logging.handlers.RotatingFileHandler(
            STATE_DIR / "server.log", maxBytes=1_000_000, backupCount=2,
            encoding="utf-8",
        )
    )
except OSError:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("comfy_mcp")

mcp = FastMCP("comfyui")
client = ComfyClient()
cloud = CloudComfyClient()

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}


def _err(message: str) -> dict:
    return {"error": message}


def _file_abs_path(filename: str, subfolder: str, file_type: str) -> str:
    base = {"output": OUTPUT_DIR, "input": INPUT_DIR,
            "temp": COMFY_DATA_DIR / "temp"}.get(file_type, OUTPUT_DIR)
    return str(base / subfolder / filename) if subfolder else str(base / filename)


def _collect_outputs(history_entry: dict) -> list[dict]:
    """Flatten a history entry's outputs into [{filename, subfolder, ...}]."""
    files: list[dict] = []
    for node_id, node_out in (history_entry.get("outputs") or {}).items():
        if not isinstance(node_out, dict):
            continue
        for kind, items in node_out.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and "filename" in item:
                    files.append({
                        "node": node_id,
                        "kind": kind,
                        "filename": item["filename"],
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                        "path": _file_abs_path(
                            item["filename"], item.get("subfolder", ""),
                            item.get("type", "output"),
                        ),
                    })
    return files


def _job_summary(prompt_id: str, history_entry: dict | None) -> dict:
    summary: dict[str, Any] = {"prompt_id": prompt_id,
                               "mine": tracking.is_mine(prompt_id)}
    if history_entry:
        status = history_entry.get("status") or {}
        summary["status"] = status.get("status_str") or (
            "success" if history_entry.get("outputs") else "unknown")
        summary["completed"] = status.get("completed")
        summary["outputs"] = _collect_outputs(history_entry)
        errors = [
            m[1] for m in status.get("messages", [])
            if isinstance(m, list) and len(m) > 1 and m[0] == "execution_error"
        ]
        if errors:
            summary["errors"] = errors
    return summary


def _convert_ui(parsed: dict, info: dict) -> dict:
    """ui_to_api with converter bugs surfaced as readable errors."""
    try:
        return graph.ui_to_api(parsed, info)
    except graph.ConversionError:
        raise
    except Exception as exc:  # noqa: BLE001 — converter must not leak tracebacks
        raise graph.ConversionError(
            f"UI->API conversion failed ({type(exc).__name__}: {exc}). "
            "Export the workflow as API format from the ComfyUI editor instead."
        ) from exc


async def _resolve_workflow(workflow: dict | str | None, template: str | None,
                            params: dict | None,
                            api: Any = None) -> tuple[dict, list[str]]:
    """Tool arguments -> (validated namespaced API graph, warnings).

    `api` supplies object_info() — the local client by default, the cloud
    client for cloud runs (the two have different node/model catalogs).
    """
    api = api or client
    if (workflow is None) == (template is None):
        raise ValueError("Provide exactly one of `workflow` or `template`.")
    if template is not None:
        api_wf = tpl.apply_template(tpl.load_template(template), params)
    else:
        parsed = graph.parse_workflow(workflow)
        if graph.is_api_format(parsed):
            api_wf = graph.deep_copy(parsed)
        else:
            info = await api.object_info()
            api_wf = _convert_ui(parsed, info)
        for key, value in (params or {}).items():
            node_id, _, input_name = key.partition(".")
            if not input_name:
                raise ValueError(
                    "When running a raw workflow, params keys must be "
                    "'node_id.input_name' (e.g. '12.text').")
            graph.set_input(api_wf, node_id, input_name, value)
    info = await api.object_info()
    problems = graph.validate_api_workflow(api_wf, info)
    if problems:
        raise ValueError("Workflow validation failed: " + "; ".join(problems[:10]))
    _, namespace_problems = graph.namespace_outputs(api_wf)
    if namespace_problems:
        raise ValueError(
            "Refusing to run — outputs could escape output/claude/: "
            + "; ".join(namespace_problems[:10])
        )
    return api_wf, graph.combo_warnings(api_wf, info)


# --------------------------------------------------------------------- tools


@mcp.tool()
async def comfy_status() -> dict:
    """Check ComfyUI connectivity, version, GPU/VRAM, and queue state.

    Call this first in a session to confirm the server is reachable.
    """
    try:
        base = await client.base_url()
        stats = await client.system_stats()
        q = await client.queue()
    except (ComfyUnavailable, ComfyApiError) as exc:
        return _err(str(exc))
    devices = [
        {
            "name": d.get("name"),
            "vram_total_gb": round((d.get("vram_total") or 0) / 1e9, 1),
            "vram_free_gb": round((d.get("vram_free") or 0) / 1e9, 1),
        }
        for d in stats.get("devices", [])
    ]
    running = [item[1] for item in q.get("queue_running", []) if len(item) > 1]
    return {
        "url": base,
        "comfyui_version": stats.get("system", {}).get("comfyui_version"),
        "devices": devices,
        "queue_running": running,
        "queue_pending_count": len(q.get("queue_pending", [])),
        "running_job_is_mine": any(tracking.is_mine(p) for p in running),
        "templates_available": [t["name"] for t in tpl.list_templates()],
        "data_dir": str(COMFY_DATA_DIR),
    }


@mcp.tool()
async def list_models(folder: str = "") -> dict:
    """List model folders, or the models inside one folder.

    Args:
        folder: empty for the folder list; or e.g. "checkpoints", "loras",
            "vae", "controlnet", "upscale_models".
    """
    try:
        if not folder:
            return {"folders": await client.model_folders()}
        files = await client.models_in_folder(folder)
    except (ComfyUnavailable, ComfyApiError) as exc:
        return _err(str(exc))
    local_dir = COMFY_DATA_DIR / "models" / folder
    detailed = []
    for name in files:
        entry: dict[str, Any] = {"name": name}
        candidate = local_dir / name
        if candidate.is_file():
            entry["size_gb"] = round(candidate.stat().st_size / 1e9, 2)
        detailed.append(entry)
    return {"folder": folder, "models": detailed}


@mcp.tool()
async def search_nodes(query: str, limit: int = 40) -> dict:
    """Search installed node types by name/category (from /object_info).

    Args:
        query: case-insensitive substring, e.g. "ltx", "save video", "upscale".
        limit: max results.
    """
    try:
        info = await client.object_info()
    except (ComfyUnavailable, ComfyApiError) as exc:
        return _err(str(exc))
    terms = [t for t in query.lower().split() if t]
    results = []
    for class_type, schema in info.items():
        haystack = " ".join([
            class_type, schema.get("display_name", "") or "",
            schema.get("category", "") or "",
        ]).lower()
        if all(t in haystack for t in terms):
            results.append({
                "class_type": class_type,
                "display_name": schema.get("display_name"),
                "category": schema.get("category"),
                "output_node": schema.get("output_node", False),
            })
            if len(results) >= limit:
                break
    return {"query": query, "count": len(results), "nodes": results}


@mcp.tool()
async def node_info(class_type: str) -> dict:
    """Full input/output schema for one node type (exact class_type)."""
    try:
        info = await client.object_info(class_type)
    except ComfyApiError:
        # /object_info/{type} 500s on unknown types in some versions
        info = {}
    except ComfyUnavailable as exc:
        return _err(str(exc))
    if not info:
        try:
            all_info = await client.object_info()
        except (ComfyUnavailable, ComfyApiError) as exc:
            return _err(str(exc))
        if class_type in all_info:
            return {class_type: all_info[class_type]}
        close = [c for c in all_info if class_type.lower() in c.lower()][:10]
        return _err(
            f"Unknown node type {class_type!r}."
            + (f" Did you mean one of: {close}?" if close else "")
        )
    return info


@mcp.tool()
async def list_templates() -> list[dict]:
    """List available parameterized workflow templates and their params."""
    return tpl.list_templates()


@mcp.tool()
async def run_workflow(
    workflow: dict | str | None = None,
    template: str | None = None,
    params: dict | None = None,
    wait: bool = False,
    timeout_s: int = 300,
) -> dict:
    """Queue a workflow on ComfyUI (always at the back of the queue).

    Provide either `workflow` (API-format prompt JSON; UI-format is converted
    best-effort) or `template` (a template name from list_templates).

    Args:
        workflow: API-format graph (dict or JSON string), or UI-format export.
        template: template name; use `params` for its parameters.
        params: for templates: {param: value}. For raw workflows:
            {"<node_id>.<input>": value} overrides, e.g. {"7.seed": 42}.
        wait: block until done and return outputs (good for fast jobs; for
            long video renders prefer wait=false + wait_for_job/job_status).
        timeout_s: max seconds to wait when wait=true.
    """
    try:
        api_wf, warnings = await _resolve_workflow(workflow, template, params)
        result = await client.submit_prompt(api_wf)
    except (ComfyUnavailable, ComfyApiError, ValueError, FileNotFoundError,
            KeyError, graph.ConversionError) as exc:
        return _err(str(exc))
    if result.get("error"):
        # Validation failure: pass ComfyUI's structured error through intact.
        return {"error": result["error"], "node_errors": result.get("node_errors")}
    prompt_id = result.get("prompt_id")
    tracking.record_job(prompt_id, template=template,
                        node_count=len(api_wf), status="queued")
    response = {
        "prompt_id": prompt_id,
        "queue_number": result.get("number"),
        "note": "Queued behind any existing jobs; the user's queue is untouched.",
    }
    if result.get("node_errors"):
        # 200 partial success: some output nodes failed validation but the
        # job IS queued for the rest.
        response["node_errors"] = result["node_errors"]
    if warnings:
        response["warnings"] = warnings
    if wait and prompt_id:
        final = await wait_for_job(prompt_id, timeout_s)
        if warnings and isinstance(final, dict):
            final.setdefault("warnings", warnings)
        return final
    return response


@mcp.tool()
async def job_status(prompt_id: str) -> dict:
    """Check one job: queue position, running, or finished (with outputs)."""
    try:
        hist = await client.history(prompt_id)
        entry = hist.get(prompt_id)
        if entry:
            summary = _job_summary(prompt_id, entry)
            tracking.update_job(prompt_id, status=summary.get("status"))
            return summary
        pos = await client.queue_position(prompt_id)
    except (ComfyUnavailable, ComfyApiError) as exc:
        return _err(str(exc))
    return {"prompt_id": prompt_id, "mine": tracking.is_mine(prompt_id), **pos}


@mcp.tool()
async def wait_for_job(prompt_id: str, timeout_s: int = 300) -> dict:
    """Wait for a job to finish; returns status, outputs, and any errors.

    On timeout the job keeps running — poll later with job_status.
    """
    try:
        entry = await client.wait_for_prompt(prompt_id, timeout_s=float(timeout_s))
    except TimeoutError as exc:
        return {"prompt_id": prompt_id, "timeout": True, "detail": str(exc)}
    except (ComfyUnavailable, ComfyApiError) as exc:
        return _err(str(exc))
    summary = _job_summary(prompt_id, entry)
    if entry.get("_progress"):
        summary["last_progress"] = entry["_progress"]
    tracking.update_job(prompt_id, status=summary.get("status"))
    return summary


@mcp.tool()
async def cancel_job(prompt_id: str, force: bool = False) -> dict:
    """Cancel a job submitted by this server (pending or running).

    Refuses to cancel the user's own jobs unless force=true.
    """
    if not tracking.is_mine(prompt_id) and not force:
        return _err(
            f"Prompt {prompt_id} was not submitted by Claude; refusing to "
            "cancel the user's job. Pass force=true only if the user asked."
        )
    try:
        pos = await client.queue_position(prompt_id)
        if pos["state"] == "running":
            await client.interrupt(prompt_id)
            action = "interrupted (was running)"
        elif pos["state"] == "pending":
            await client.delete_queued([prompt_id])
            action = "removed from queue"
        else:
            return {"prompt_id": prompt_id,
                    "result": "not in queue (already finished?)"}
    except (ComfyUnavailable, ComfyApiError) as exc:
        return _err(str(exc))
    tracking.update_job(prompt_id, status="cancelled")
    return {"prompt_id": prompt_id, "result": action}


@mcp.tool()
async def queue_info() -> dict:
    """Show running and pending jobs, marking which were submitted by Claude."""
    try:
        q = await client.queue()
    except (ComfyUnavailable, ComfyApiError) as exc:
        return _err(str(exc))

    def brief(item: list) -> dict:
        pid = item[1] if len(item) > 1 else "?"
        nodes = len(item[2]) if len(item) > 2 and isinstance(item[2], dict) else None
        return {"prompt_id": pid, "nodes": nodes, "mine": tracking.is_mine(pid)}

    return {
        "running": [brief(i) for i in q.get("queue_running", [])],
        "pending": [brief(i) for i in client.sorted_pending(q)],
    }


@mcp.tool()
async def get_history(limit: int = 10, only_mine: bool = False) -> list[dict]:
    """Recent executions with status and output files (newest first).

    Args:
        limit: max entries to return.
        only_mine: only jobs submitted by this server.
    """
    try:
        hist = await client.history(max_items=200 if only_mine else max(limit, 20))
    except (ComfyUnavailable, ComfyApiError) as exc:
        return [_err(str(exc))]
    entries = []
    for pid, entry in hist.items():
        if only_mine and not tracking.is_mine(pid):
            continue
        number = entry.get("prompt", [0])[0] if entry.get("prompt") else 0
        entries.append((number, _job_summary(pid, entry)))
    entries.sort(key=lambda x: x[0], reverse=True)
    return [e[1] for e in entries[:limit]]


@mcp.tool()
async def history_to_template(prompt_id: str, name: str,
                              description: str = "") -> dict:
    """Capture a past run (from history) as a reusable parameterized template.

    Great for turning a workflow the user already ran (e.g. their LTX-2 video
    setup) into something Claude can re-run with different prompts/seeds.
    """
    try:
        hist = await client.history(prompt_id)
    except (ComfyUnavailable, ComfyApiError) as exc:
        return _err(str(exc))
    entry = hist.get(prompt_id)
    if not entry:
        return _err(f"No history entry for {prompt_id!r}. Use get_history first.")
    prompt_graph = entry.get("prompt", [None, None, None])[2]
    if not isinstance(prompt_graph, dict):
        return _err("History entry has no readable prompt graph.")
    path = tpl.template_from_history(prompt_graph, name, description)
    saved = tpl.load_template(name)
    return {
        "saved_to": str(path),
        "params": {k: v.get("default") for k, v in (saved.get("params") or {}).items()},
        "note": "Edit the template file to trim/rename params if needed.",
    }


@mcp.tool()
async def list_outputs(subfolder: str = "claude", limit: int = 25) -> dict:
    """List files in the output directory (newest first).

    Args:
        subfolder: "" for the output root, default "claude" (Claude's own
            renders live in output/claude/).
        limit: max files.
    """
    target = (OUTPUT_DIR / subfolder) if subfolder else OUTPUT_DIR
    if not target.is_dir():
        return {"directory": str(target), "files": [],
                "note": "Directory does not exist yet."}
    files = [p for p in target.rglob("*") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "directory": str(target),
        "files": [
            {
                "path": str(p),
                "size_mb": round(p.stat().st_size / 1e6, 2),
                "modified": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)),
            }
            for p in files[:limit]
        ],
    }


@mcp.tool(structured_output=False)
async def view_output(path_or_filename: str, subfolder: str = "",
                      max_dim: int = 768) -> Any:
    """View a generated image inline (downscaled). Videos return metadata.

    Args:
        path_or_filename: absolute path, or a filename relative to output/
            (combine with subfolder).
        subfolder: subfolder under output/ when passing a bare filename.
        max_dim: longest image side after downscaling.
    """
    p = Path(path_or_filename)
    if not p.is_absolute():
        p = (OUTPUT_DIR / subfolder / path_or_filename) if subfolder \
            else (OUTPUT_DIR / path_or_filename)
    if not p.is_file():
        return _err(f"File not found: {p}")
    info = {"path": str(p), "size_mb": round(p.stat().st_size / 1e6, 2)}
    ext = p.suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return {**info, "kind": "video",
                "note": "Video files cannot be embedded; open the path directly."}
    if ext not in IMAGE_EXTENSIONS:
        return {**info, "kind": "file"}
    try:
        from PIL import Image as PILImage
        with PILImage.open(p) as im:
            im = im.convert("RGB")
            info["dimensions"] = f"{im.width}x{im.height}"
            if max(im.size) > max_dim:
                im.thumbnail((max_dim, max_dim))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
        return [json.dumps(info), Image(data=buf.getvalue(), format="jpeg")]
    except Exception as exc:  # noqa: BLE001
        return _err(f"Could not read image {p}: {exc}")


@mcp.tool()
async def upload_input(source_path: str | None = None,
                       image_b64: str | None = None,
                       filename: str | None = None,
                       overwrite: bool = False) -> dict:
    """Upload an image into ComfyUI's input/claude/ for use in LoadImage nodes.

    Args:
        source_path: absolute path of a local file to upload.
        image_b64: alternatively, base64-encoded image bytes.
        filename: name to store as (required with image_b64).
        overwrite: replace an existing file of the same name.
    """
    if (source_path is None) == (image_b64 is None):
        return _err("Provide exactly one of source_path or image_b64.")
    if source_path:
        src = Path(source_path)
        if not src.is_file():
            return _err(f"File not found: {src}")
        data = src.read_bytes()
        filename = filename or src.name
    else:
        if not filename:
            return _err("filename is required with image_b64.")
        try:
            data = base64.b64decode(image_b64)
        except Exception as exc:  # noqa: BLE001
            return _err(f"Invalid base64 data: {exc}")
    try:
        result = await client.upload_image(data, filename, subfolder="claude",
                                           overwrite=overwrite)
    except (ComfyUnavailable, ComfyApiError) as exc:
        return _err(str(exc))
    stored = result.get("name", filename)
    sub = result.get("subfolder", "claude")
    return {
        "stored_as": f"{sub}/{stored}" if sub else stored,
        "note": f'Use "{sub}/{stored}" as the `image` input of a LoadImage node.',
    }


@mcp.tool()
async def list_user_workflows() -> dict:
    """List workflows saved in the ComfyUI editor (user/default/workflows)."""
    names: list[str] = []
    try:
        listed = await client.userdata_list("workflows")
        names = [n if isinstance(n, str) else n.get("path", str(n)) for n in listed]
    except (ComfyUnavailable, ComfyApiError):
        if WORKFLOWS_DIR.is_dir():
            names = [str(p.relative_to(WORKFLOWS_DIR)) for p in
                     WORKFLOWS_DIR.rglob("*.json")]
    return {"workflows": sorted(names), "directory": str(WORKFLOWS_DIR)}


@mcp.tool()
async def get_user_workflow(name: str) -> dict:
    """Read a saved editor workflow (UI format). Convert before running."""
    rel = name if name.endswith(".json") else f"{name}.json"
    try:
        data = await client.userdata_get(f"workflows/{rel}")
        if isinstance(data, dict):
            return {"name": rel, "format": "ui", "workflow": data}
    except (ComfyUnavailable, ComfyApiError):
        pass
    local = WORKFLOWS_DIR / rel
    if local.is_file():
        with open(local, encoding="utf-8") as fh:
            return {"name": rel, "format": "ui", "workflow": json.load(fh)}
    return _err(f"No saved workflow {rel!r}; see list_user_workflows.")


@mcp.tool()
async def save_user_workflow(name: str, workflow: dict | str,
                             overwrite: bool = False) -> dict:
    """Save a workflow into the user's editor library (UI format expected).

    It will appear under Workflows in the ComfyUI app. Refuses to overwrite
    unless overwrite=true.
    """
    rel = name if name.endswith(".json") else f"{name}.json"
    try:
        parsed = graph.parse_workflow(workflow)
    except graph.ConversionError as exc:
        return _err(str(exc))
    note = None
    if graph.is_api_format(parsed):
        note = ("This is API-format JSON; it executes fine but will not lay "
                "out visually in the editor. Prefer saving UI-format exports.")
    try:
        await client.userdata_put(f"workflows/{rel}", parsed, overwrite=overwrite)
    except ComfyApiError as exc:
        if "409" in str(exc) or "exists" in str(exc).lower():
            return _err(f"{rel!r} already exists; pass overwrite=true to replace.")
        return _err(str(exc))
    except ComfyUnavailable as exc:
        return _err(str(exc))
    result = {"saved": rel, "location": str(WORKFLOWS_DIR / rel)}
    if note:
        result["note"] = note
    return result


@mcp.tool()
async def convert_ui_to_api(workflow: dict | str) -> dict:
    """Convert an editor (UI-format) workflow to API format without running it.

    Best-effort: subgraph/group-node workflows must be exported as API format
    from the editor instead.
    """
    try:
        parsed = graph.parse_workflow(workflow)
        if graph.is_api_format(parsed):
            return {"already_api_format": True, "workflow": parsed}
        info = await client.object_info()
        api_wf = _convert_ui(parsed, info)
        problems = graph.validate_api_workflow(api_wf, info)
        warnings = graph.combo_warnings(api_wf, info)
    except (graph.ConversionError, ComfyUnavailable, ComfyApiError) as exc:
        return _err(str(exc))
    return {"workflow": api_wf, "validation_problems": problems,
            "warnings": warnings}


@mcp.tool()
async def get_logs(lines: int = 100) -> dict:
    """Tail the ComfyUI server log (great for debugging failed runs)."""
    try:
        raw = await client.internal_logs_raw()
    except (ComfyUnavailable, ComfyApiError) as exc:
        return _err(str(exc))
    entries = raw.get("entries", [])
    text = "".join(e.get("m", "") for e in entries)
    tail = text.splitlines()[-lines:]
    return {"lines": len(tail), "log": "\n".join(tail)}


@mcp.tool()
async def free_vram(unload_models: bool = False) -> dict:
    """Free VRAM caches (and optionally unload models). Explicit use only.

    Warning: unload_models=true makes the user's next interactive run reload
    its models — only do this when they ask or a job hit out-of-memory.
    """
    try:
        await client.free(unload_models=unload_models)
    except (ComfyUnavailable, ComfyApiError) as exc:
        return _err(str(exc))
    return {"freed": True, "unloaded_models": unload_models}


@mcp.tool()
async def system_paths() -> dict:
    """Key directories of this ComfyUI installation (absolute paths)."""
    return {
        "data_dir": str(COMFY_DATA_DIR),
        "output": str(OUTPUT_DIR),
        "claude_output": str(OUTPUT_DIR / "claude"),
        "input": str(INPUT_DIR),
        "claude_input": str(INPUT_DIR / "claude"),
        "models": str(COMFY_DATA_DIR / "models"),
        "custom_nodes": str(COMFY_DATA_DIR / "custom_nodes"),
        "user_workflows": str(WORKFLOWS_DIR),
        "templates": str(tpl.TEMPLATE_DIR),
        "state": str(STATE_DIR),
    }


# ----------------------------------------------------------- Comfy Cloud

CLOUD_OUTPUT_DIR = OUTPUT_DIR / "claude" / "cloud"


def _cloud_outputs(details: dict) -> list[dict]:
    """Flatten cloud job details into [{node, kind, filename, ...}]."""
    outputs = details.get("outputs")
    if not isinstance(outputs, dict):
        # some responses inline node-id keys at the top level
        outputs = {k: v for k, v in details.items()
                   if isinstance(v, dict) and any(
                       isinstance(i, list) for i in v.values())}
    files = []
    for node_id, node_out in (outputs or {}).items():
        if not isinstance(node_out, dict):
            continue
        for kind, items in node_out.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and "filename" in item:
                    files.append({"node": node_id, "kind": kind,
                                  "filename": item["filename"],
                                  "subfolder": item.get("subfolder", ""),
                                  "type": item.get("type", "output")})
    return files


async def _cloud_download(prompt_id: str, files: list[dict]) -> list[str]:
    target = CLOUD_OUTPUT_DIR / prompt_id[:8]
    target.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        try:
            data = await cloud.download_view(f["filename"], f["subfolder"],
                                             f["type"])
            path = target / Path(f["filename"]).name
            path.write_bytes(data)
            saved.append(str(path))
        except CloudApiError as exc:
            log.warning("cloud download failed for %s: %s", f["filename"], exc)
    return saved


@mcp.tool()
async def cloud_status() -> dict:
    """Check Comfy Cloud access: API key validity, account, queue state.

    Cloud runs execute on Comfy's GPUs under the user's account and consume
    their credits — only submit cloud jobs the user asked for.
    """
    try:
        user = await cloud.user()
        q = await cloud.queue()
    except (CloudKeyMissing, CloudApiError) as exc:
        return _err(str(exc))
    return {
        "url": "https://cloud.comfy.org",
        "account": user,
        "queue_running": len(q.get("queue_running", [])),
        "queue_pending": len(q.get("queue_pending", [])),
        "note": "Every cloud run consumes the user's Comfy Cloud credits.",
    }


@mcp.tool()
async def cloud_models(loader: str = "CheckpointLoaderSimple") -> dict:
    """List model files available on Comfy Cloud (via a loader's options).

    Args:
        loader: loader node whose first COMBO holds the catalog, e.g.
            CheckpointLoaderSimple, LoraLoader, VAELoader, UNETLoader.
    """
    try:
        info = await cloud.object_info()
    except (CloudKeyMissing, CloudApiError) as exc:
        return _err(str(exc))
    schema = info.get(loader)
    if not schema:
        close = [c for c in info if loader.lower() in c.lower()][:10]
        return _err(f"No node {loader!r} on Comfy Cloud."
                    + (f" Similar: {close}" if close else ""))
    options: dict[str, list] = {}
    for section in ("required", "optional"):
        for name, spec in (schema.get("input", {}).get(section) or {}).items():
            if isinstance(spec, (list, tuple)) and spec and isinstance(spec[0], list):
                options[name] = spec[0]
    return {"loader": loader, "options": options}


@mcp.tool()
async def run_cloud_workflow(
    workflow: dict | str | None = None,
    template: str | None = None,
    params: dict | None = None,
    wait: bool = False,
    timeout_s: int = 600,
) -> dict:
    """Run a workflow on Comfy Cloud (the user's account — consumes credits).

    Only call this when the user explicitly asked for a cloud run. The cloud
    has its own node/model catalog: validate model names with cloud_models
    first — local files (e.g. GGUF quants) do not exist there.

    Args: same as run_workflow.
    """
    try:
        api_wf, warnings = await _resolve_workflow(workflow, template, params,
                                                   api=cloud)
        result = await cloud.submit_prompt(api_wf)
    except (CloudKeyMissing, CloudApiError, ValueError, FileNotFoundError,
            KeyError, graph.ConversionError) as exc:
        return _err(str(exc))
    if result.get("error"):
        return {"error": result["error"], "node_errors": result.get("node_errors")}
    prompt_id = result.get("prompt_id")
    tracking.record_job(prompt_id, template=template, cloud=True,
                        node_count=len(api_wf), status="queued")
    response: dict[str, Any] = {
        "prompt_id": prompt_id,
        "cloud": True,
        "note": "Running on Comfy Cloud under the user's account (credits).",
    }
    if warnings:
        response["warnings"] = warnings
    if wait and prompt_id:
        return await cloud_wait_for_job(prompt_id, timeout_s)
    return response


@mcp.tool()
async def cloud_job_status(prompt_id: str) -> dict:
    """Status of a Comfy Cloud job; includes outputs once completed."""
    try:
        status = (await cloud.job_status(prompt_id)).get("status", "unknown")
        result: dict[str, Any] = {"prompt_id": prompt_id, "status": status,
                                  "mine": tracking.is_mine(prompt_id)}
        if status in cloud_client_module.SUCCESS_STATES:
            details = await cloud.job_details(prompt_id)
            result["outputs"] = _cloud_outputs(details)
        tracking.update_job(prompt_id, status=status)
        return result
    except (CloudKeyMissing, CloudApiError) as exc:
        return _err(str(exc))


@mcp.tool()
async def cloud_wait_for_job(prompt_id: str, timeout_s: int = 600) -> dict:
    """Wait for a cloud job; on completion downloads outputs locally.

    Files land in output/claude/cloud/<job>/ so view_output works on them.
    """
    try:
        details = await cloud.wait_for_job(prompt_id, timeout_s=float(timeout_s))
    except TimeoutError as exc:
        return {"prompt_id": prompt_id, "timeout": True, "detail": str(exc)}
    except (CloudKeyMissing, CloudApiError) as exc:
        return _err(str(exc))
    status = details.get("_status", "unknown")
    tracking.update_job(prompt_id, status=status)
    files = _cloud_outputs(details)
    result: dict[str, Any] = {"prompt_id": prompt_id, "status": status,
                              "outputs": files}
    if status in ("failed", "error"):
        result["detail"] = details.get("error") or details.get("status_detail") \
            or "Job failed — see the cloud workspace for the error."
    if files:
        result["local_paths"] = await _cloud_download(prompt_id, files)
    return result


@mcp.tool()
async def cloud_upload_input(source_path: str, filename: str | None = None,
                             overwrite: bool = False) -> dict:
    """Upload a local image to Comfy Cloud for use in LoadImage nodes there."""
    src = Path(source_path)
    if not src.is_file():
        return _err(f"File not found: {src}")
    try:
        result = await cloud.upload_image(src.read_bytes(),
                                          filename or src.name, overwrite)
    except (CloudKeyMissing, CloudApiError) as exc:
        return _err(str(exc))
    name = result.get("name", filename or src.name)
    return {"stored_as": name,
            "note": f'Use "{name}" as the image input of a cloud LoadImage.'}


@mcp.tool()
async def cloud_cancel_job(prompt_id: str, force: bool = False) -> dict:
    """Cancel a Comfy Cloud job this server submitted (force for others)."""
    if not tracking.is_mine(prompt_id) and not force:
        return _err(f"Cloud job {prompt_id} was not submitted by Claude; "
                    "refusing without force=true.")
    try:
        status = (await cloud.job_status(prompt_id)).get("status")
        if status == "pending":
            await cloud.delete_queued([prompt_id])
            action = "removed from cloud queue"
        elif status == "in_progress":
            await cloud.interrupt()
            action = "interrupt sent (stops the currently executing job)"
        else:
            return {"prompt_id": prompt_id, "status": status,
                    "result": "not cancellable in this state"}
    except (CloudKeyMissing, CloudApiError) as exc:
        return _err(str(exc))
    tracking.update_job(prompt_id, status="cancelled")
    return {"prompt_id": prompt_id, "result": action}


if __name__ == "__main__":
    log.info("starting ComfyUI MCP server (data dir: %s)", COMFY_DATA_DIR)
    mcp.run()
