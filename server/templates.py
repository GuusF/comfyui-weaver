"""Parameterized workflow templates.

A template is a JSON file in claude-integration/templates/:

    {
      "name": "...",
      "description": "...",
      "params": {
        "<param>": {"node": "<id>", "input": "<name>",
                     "default": <value-or-null>, "description": "..."}
      },
      "workflow": { ... API-format prompt graph ... }
    }

A param with default null is required. The best source of new templates is a
run the user already did: history_to_template() turns a /history entry into a
template skeleton with the obviously-tunable inputs exposed.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from graph import deep_copy, set_input

log = logging.getLogger("comfy_mcp.templates")

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

# Input names worth exposing when building a template from history.
TUNABLE_INPUTS = {
    "seed", "noise_seed", "steps", "cfg", "denoise", "text", "prompt",
    "positive_prompt", "negative_prompt", "filename_prefix", "width", "height",
    "length", "num_frames", "frames", "fps", "frame_rate", "batch_size",
    "ckpt_name", "lora_name", "strength", "strength_model", "strength_clip",
    "image", "video", "crf", "guidance",
}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", name.lower()).strip("_")


def list_templates() -> list[dict]:
    out = []
    if not TEMPLATE_DIR.is_dir():
        return out
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                t = json.load(fh)
            out.append({
                "name": t.get("name", path.stem),
                "description": t.get("description", ""),
                "params": {
                    k: {
                        "description": v.get("description", ""),
                        "default": v.get("default"),
                        "required": v.get("default") is None,
                    }
                    for k, v in (t.get("params") or {}).items()
                },
                "file": str(path),
            })
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("skipping unreadable template %s: %s", path, exc)
    return out


def load_template(name: str) -> dict:
    path = TEMPLATE_DIR / f"{_slug(name)}.json"
    if not path.is_file():
        # allow exact filename matches too
        alt = TEMPLATE_DIR / f"{name}.json"
        path = alt if alt.is_file() else path
    if not path.is_file():
        known = ", ".join(t["name"] for t in list_templates()) or "(none)"
        raise FileNotFoundError(f"No template {name!r}. Available: {known}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def apply_template(template: dict, params: dict[str, Any] | None) -> dict:
    """Return an API-format workflow with params substituted."""
    params = params or {}
    workflow = deep_copy(template["workflow"])
    spec: dict[str, dict] = template.get("params") or {}
    unknown = set(params) - set(spec)
    if unknown:
        raise ValueError(
            f"Unknown params {sorted(unknown)}; this template accepts {sorted(spec)}."
        )
    missing = [
        k for k, v in spec.items()
        if v.get("default") is None and k not in params
    ]
    if missing:
        raise ValueError(f"Missing required params: {sorted(missing)}")
    for key, entry in spec.items():
        value = params.get(key, entry.get("default"))
        set_input(workflow, entry["node"], entry["input"], value)
    return workflow


def save_template(name: str, description: str, workflow: dict,
                  params: dict[str, dict]) -> Path:
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    path = TEMPLATE_DIR / f"{_slug(name)}.json"
    payload = {
        "name": _slug(name),
        "description": description,
        "params": params,
        "workflow": workflow,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    return path


def template_from_history(prompt_graph: dict, name: str,
                          description: str = "") -> Path:
    """Build a template from an executed prompt graph (history entry)."""
    params: dict[str, dict] = {}
    for node_id, node in prompt_graph.items():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for input_name, value in inputs.items():
            if isinstance(value, list):
                continue  # connection, not a literal
            if input_name not in TUNABLE_INPUTS:
                continue
            key = input_name if input_name not in params else f"{input_name}_{node_id}"
            params[key] = {
                "node": str(node_id),
                "input": input_name,
                "default": value,
                "description": f"{node.get('class_type', '?')} node {node_id}",
            }
    return save_template(name, description or f"Captured from history ({name})",
                         prompt_graph, params)
