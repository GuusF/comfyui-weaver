# ComfyUI data directory — Claude guide (EXAMPLE — edit for your machine)

This folder is the **data directory of ComfyUI** (models, custom_nodes,
input/output, user settings).
<!-- If you run ComfyUI Desktop: the app manages the actual source; note its
     location here and tell Claude not to edit it. -->

## Driving ComfyUI

Use the **`comfyui` MCP server** (registered in `.mcp.json`, code in
`claude-integration/`). Start every ComfyUI task with `comfy_status`. If no
server is running, ask the user to start ComfyUI — never launch or kill it
yourself.

The `/comfyui` skill (`.claude/skills/comfyui/SKILL.md`) documents the
workflow JSON format and tool recipes.

## Hard rules (the user keeps working while you do)

- **Never** modify ComfyUI's own Python environment or install packages into
  it. The bridge has its own venv.
- **Never** start, stop, or restart the ComfyUI app/backend.
- **Never** clear the queue or history; never submit with `front=true`
  (the MCP server enforces both).
- Generated files belong under `output/claude/` (enforced) — do not write
  into `output/` root or delete anything outside `output/claude/` and
  `input/claude/`.
- Don't install/remove custom nodes or models without being asked.

## This machine (EDIT ME)

- GPU: <model, VRAM — and which other apps usually occupy it>
- Main models/workloads: <e.g. SDXL, Flux, video models, upscalers>
- Custom node packs worth knowing: <list the important ones>
- <Anything else Claude should know: naming conventions, port overrides,
  where your real workflows live>

## Verification

`<bridge venv python> <bridge path>\tests\smoke_test.py` runs an end-to-end
check (use `--offline` when ComfyUI isn't running). The `passthrough_test`
template verifies execution without touching the GPU.
