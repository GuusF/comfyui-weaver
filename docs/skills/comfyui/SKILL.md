---
name: comfyui
description: Drive the local ComfyUI instance via the comfyui MCP server — build and run workflows, manage jobs and outputs, debug failed runs. Use whenever the user asks to generate images/video with ComfyUI, run or modify a workflow, inspect models/nodes, or troubleshoot ComfyUI.
---

# Driving ComfyUI from Claude Code

## Session start

1. `comfy_status` — confirms the backend is up (port 8000/8188 auto-detected)
   and shows queue + VRAM. If unreachable, ask the user to start ComfyUI;
   never launch it yourself.
2. The queue is shared with the user: your jobs always go to the **back**.
   For anything longer than ~30 s submit `wait=false`, then `job_status` /
   `wait_for_job` with a generous timeout.

## Workflow JSON: two formats

**API format** (what actually executes — POST /prompt):
```json
{
  "1": {"class_type": "LoadImage", "inputs": {"image": "claude/in.png"}},
  "2": {"class_type": "SaveImage",
        "inputs": {"images": ["1", 0], "filename_prefix": "render"}}
}
```
- Keys are node ids (strings). Connections are `["<source_node_id>", <output_slot>]`.
- Literal values fill widget inputs by **name** (get names from `node_info`).

**UI format** (what the editor saves): has `nodes`/`links` arrays plus layout.
`run_workflow` and `convert_ui_to_api` convert it best-effort; workflows with
subgraphs/group nodes must be exported via *Workflow → Export (API)* in the
app instead.

## Building a workflow from scratch

1. `search_nodes("<keyword>")` → find candidate class types.
2. `node_info("<ClassType>")` → exact input names, types, and COMBO options
   (COMBO options are the *valid values* — e.g. available model filenames).
3. Wire outputs to inputs by slot index; output types are listed in order in
   `node_info` under `output`.
4. `run_workflow(workflow=..., wait=false)` — validation errors come back
   with node ids; fix and resubmit. `get_logs` shows the backend traceback
   for runtime failures.

## The fastest path to a working pipeline

The user's proven workflows often live in their **run history**, not files:
- `get_history(limit=10)` → find a successful run,
- `history_to_template(prompt_id, "<name>")` → reusable template with
  seeds/prompts/sizes exposed as params,
- `run_workflow(template="<name>", params={...})`.

## Comfy Cloud (the user's paid account — credits!)

`cloud_*` tools run workflows on cloud.comfy.org GPUs. Rules: only submit
cloud jobs the user explicitly asked for (each run costs credits); validate
model names against `cloud_models` first (the cloud catalog ≠ local files);
completed outputs auto-download to `output/claude/cloud/`. Key lives in
`state/cloud_api_key.txt`. If `cloud_status` returns 401, the key is
missing/stale — the user must mint a new one at platform.comfy.org.

## Etiquette (enforced by the server, respected by you)

- Outputs land in `output/claude/` (filename_prefix is rewritten); inputs you
  upload land in `input/claude/`. Don't write elsewhere.
- Never clear queue/history; `cancel_job` only works on your own jobs.
- `free_vram(unload_models=true)` forces the user's next interactive run to
  reload models — only on explicit request or after a confirmed OOM.
- Long renders: report the `prompt_id` so the user can also watch it in the
  app's queue panel.

## Quick checks

- End-to-end sanity, no GPU: `run_workflow(template="passthrough_test",
  params={"image": "<subfolder/name from upload_input>"}, wait=true)`.
- View results inline: `view_output(path)` (images; videos return the path —
  use `list_outputs("claude")` to find them).
