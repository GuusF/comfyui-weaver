"""Workflow graph utilities.

- Detects UI-format vs API-format workflow JSON.
- Best-effort conversion of UI format (what the ComfyUI editor saves) to API
  format (what POST /prompt expects), using live /object_info schemas to map
  positional widgets_values to named inputs.
- Local validation against /object_info.
- Output namespacing: every filename_prefix is forced under `claude/` so
  Claude's renders never mix with the user's own outputs.

Conversion notes: the UI format is a moving target (subgraphs, group nodes,
converted widgets). Where a workflow uses features this converter does not
understand it raises with a clear message; the reliable path is exporting
"API format" from the ComfyUI editor or using history_to_template on a past
run.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any

log = logging.getLogger("comfy_mcp.graph")

OUTPUT_NAMESPACE = "claude"
# Widget value types; anything else (MODEL, IMAGE, LATENT, ...) is a connection.
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
# These node types exist only in the editor and never execute.
EDITOR_ONLY_TYPES = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode"}
MODE_MUTED = 2
MODE_BYPASSED = 4


class ConversionError(ValueError):
    pass


def parse_workflow(workflow: Any) -> dict:
    if isinstance(workflow, str):
        try:
            workflow = json.loads(workflow)
        except json.JSONDecodeError as exc:
            raise ConversionError(f"Workflow is not valid JSON: {exc}") from exc
    if not isinstance(workflow, dict):
        raise ConversionError("Workflow must be a JSON object.")
    return workflow


def is_api_format(workflow: dict) -> bool:
    if not workflow or "nodes" in workflow:
        return False
    return all(
        isinstance(v, dict) and "class_type" in v
        for k, v in workflow.items()
        if not k.startswith("_")
    )


# --------------------------------------------------------------- conversion


def _schema_inputs_in_order(schema: dict) -> list[tuple[str, Any, dict]]:
    """Yield (name, type_spec, config) for required then optional inputs."""
    out: list[tuple[str, Any, dict]] = []
    inputs = schema.get("input", {}) or {}
    for section in ("required", "optional"):
        for name, spec in (inputs.get(section) or {}).items():
            if not isinstance(spec, (list, tuple)) or not spec:
                continue
            type_spec = spec[0]
            config = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            out.append((name, type_spec, config))
    return out


def _is_widget_input(type_spec: Any, config: dict) -> bool:
    if config.get("forceInput"):
        return False
    if isinstance(type_spec, list):  # COMBO options
        return True
    return isinstance(type_spec, str) and type_spec.upper() in WIDGET_TYPES


def _has_seed_control(name: str, type_spec: Any, config: dict) -> bool:
    """The editor adds a hidden control_after_generate widget after these."""
    if config.get("control_after_generate"):
        return True
    return name in ("seed", "noise_seed") and type_spec == "INT"


def ui_to_api(ui_workflow: dict, object_info: dict) -> dict:
    """Convert editor-format workflow JSON to API (prompt) format."""
    if "definitions" in ui_workflow or any(
        isinstance(n.get("type"), str) and n["type"].startswith("workflow/")
        for n in ui_workflow.get("nodes", [])
    ):
        raise ConversionError(
            "This workflow uses subgraphs/group nodes, which this converter "
            "does not support. In ComfyUI use Workflow > Export (API) and run "
            "that file instead."
        )
    nodes = {int(n["id"]): n for n in ui_workflow.get("nodes", [])}
    links = {}
    for link in ui_workflow.get("links", []) or []:
        # [link_id, from_node, from_slot, to_node, to_slot, type]
        if isinstance(link, dict):
            links[link["id"]] = (
                link["origin_id"], link["origin_slot"],
                link["target_id"], link["target_slot"],
            )
        else:
            links[link[0]] = (link[1], link[2], link[3], link[4])

    def resolve_source(node_id: int, slot: int, depth: int = 0) -> tuple[int, int]:
        """Follow Reroute / bypassed nodes upstream to a real producer."""
        if depth > 64:
            raise ConversionError("Link resolution loop detected.")
        node = nodes.get(node_id)
        if node is None:
            raise ConversionError(f"Link references missing node {node_id}.")
        ntype = node.get("type")
        mode = node.get("mode", 0)
        if ntype == "Reroute" or mode == MODE_BYPASSED:
            # Pass through: find the input link that feeds the same data type.
            wanted_type = None
            outputs = node.get("outputs") or []
            if slot < len(outputs):
                wanted_type = outputs[slot].get("type")
            for inp in node.get("inputs") or []:
                if inp.get("link") is None:
                    continue
                if ntype == "Reroute" or wanted_type in (None, "*") or \
                        inp.get("type") in (wanted_type, "*"):
                    src = links.get(inp["link"])
                    if src:
                        return resolve_source(src[0], src[1], depth + 1)
            raise ConversionError(
                f"Cannot resolve a path through {'bypassed ' if mode == MODE_BYPASSED else ''}"
                f"node {node_id} ({ntype}); connect it or remove the bypass, "
                "or export API format from the editor."
            )
        return node_id, slot

    api: dict[str, dict] = {}
    for node_id, node in nodes.items():
        ntype = node.get("type")
        mode = node.get("mode", 0)
        if ntype in EDITOR_ONLY_TYPES or mode in (MODE_MUTED, MODE_BYPASSED):
            continue
        schema = object_info.get(ntype)
        if schema is None:
            raise ConversionError(
                f"Node {node_id} has unknown type {ntype!r} — is the custom "
                "node pack installed and the server restarted?"
            )

        def trace_through_reroutes(start_id: int) -> int:
            """Walk pure Reroute chains upstream; returns the origin node id."""
            current, hops = start_id, 0
            while hops < 64:
                origin = nodes.get(current)
                if origin is None or origin.get("type") != "Reroute":
                    return current
                hops += 1
                next_id = None
                for rin in origin.get("inputs") or []:
                    if rin.get("link") is not None and rin["link"] in links:
                        next_id = links[rin["link"]][0]
                        break
                if next_id is None:
                    return current
                current = next_id
            return current

        inputs: dict[str, Any] = {}
        link_by_input: dict[str, int] = {}
        primitive_values: dict[str, Any] = {}
        for inp in node.get("inputs") or []:
            if inp.get("link") is None:
                continue
            src = links.get(inp["link"])
            if not src:
                continue
            origin = nodes.get(trace_through_reroutes(src[0]))
            if origin and origin.get("type") == "PrimitiveNode":
                values = origin.get("widgets_values") or []
                if values:
                    primitive_values[inp["name"]] = values[0]
                continue
            link_by_input[inp["name"]] = inp["link"]

        raw_widgets = node.get("widgets_values")
        # Some nodes (e.g. VideoHelperSuite) store widgets_values as a dict
        # keyed by input name instead of a positional list.
        widgets_by_name = raw_widgets if isinstance(raw_widgets, dict) else None
        widgets = list(raw_widgets) if isinstance(raw_widgets, list) else []
        cursor = 0
        for name, type_spec, config in _schema_inputs_in_order(schema):
            if _is_widget_input(type_spec, config):
                value = None
                have_value = False
                if widgets_by_name is not None:
                    if name in widgets_by_name:
                        value = widgets_by_name[name]
                        have_value = True
                elif cursor < len(widgets):
                    value = widgets[cursor]
                    have_value = True
                    cursor += 1
                    if _has_seed_control(name, type_spec, config) and cursor < len(widgets):
                        nxt = widgets[cursor]
                        if isinstance(nxt, str) and nxt in (
                            "fixed", "increment", "decrement", "randomize"
                        ):
                            cursor += 1  # skip control_after_generate
                    # Upload buttons (image_upload etc.) serialize an extra
                    # value like "image" after the file combo — skip it so
                    # later widgets don't desync.
                    if cursor < len(widgets) and any(
                        config.get(k) for k in
                        ("image_upload", "video_upload", "audio_upload")
                    ):
                        nxt = widgets[cursor]
                        if isinstance(nxt, str) and nxt in (
                            "image", "video", "audio", "file"
                        ):
                            cursor += 1
                if name in primitive_values:
                    inputs[name] = primitive_values[name]
                elif name in link_by_input:
                    src = links[link_by_input[name]]
                    sid, sslot = resolve_source(src[0], src[1])
                    inputs[name] = [str(sid), sslot]
                elif have_value:
                    inputs[name] = value
            else:
                if name in primitive_values:
                    # PrimitiveNode feeding a forceInput/connection-typed input
                    inputs[name] = primitive_values[name]
                elif name in link_by_input:
                    src = links[link_by_input[name]]
                    sid, sslot = resolve_source(src[0], src[1])
                    inputs[name] = [str(sid), sslot]

        api[str(node_id)] = {"class_type": ntype, "inputs": inputs}
    if not api:
        raise ConversionError("Workflow contains no executable nodes.")
    return api


# --------------------------------------------------------------- validation


def validate_api_workflow(api_workflow: dict, object_info: dict) -> list[str]:
    """Local sanity check; returns a list of problems (empty = looks fine)."""
    problems: list[str] = []
    for node_id, node in api_workflow.items():
        if not isinstance(node, dict) or "class_type" not in node:
            problems.append(f"Node {node_id}: not an API-format node object.")
            continue
        ctype = node["class_type"]
        schema = object_info.get(ctype)
        if schema is None:
            problems.append(f"Node {node_id}: unknown class_type {ctype!r}.")
            continue
        inputs = node.get("inputs", {})
        required = (schema.get("input", {}) or {}).get("required") or {}
        for name in required:
            if name not in inputs:
                problems.append(f"Node {node_id} ({ctype}): missing required input {name!r}.")
        for name, value in inputs.items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                if str(value[0]) not in api_workflow:
                    problems.append(
                        f"Node {node_id} ({ctype}): input {name!r} references "
                        f"missing node {value[0]!r}."
                    )
    return problems


# -------------------------------------------------------------- namespacing


def namespace_outputs(
    api_workflow: dict, prefix: str = OUTPUT_NAMESPACE
) -> tuple[list[str], list[str]]:
    """Force save paths under `<prefix>/`; returns (changed_ids, problems).

    This keeps all Claude-generated files in their own output subfolder so the
    user's own renders are never mixed with or overwritten by ours. Handles
    `filename_prefix` (core + VHS save nodes) and `output_path` on Save-style
    nodes (WAS suite convention). Anything it cannot namespace — link-fed
    values, absolute paths escaping output/<prefix>/ — is reported as a
    problem so the caller can refuse the workflow instead of leaking files.
    """
    import os.path

    changed: list[str] = []
    problems: list[str] = []
    for node_id, node in api_workflow.items():
        if not isinstance(node, dict):
            continue
        ctype = node.get("class_type", "")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue

        value = inputs.get("filename_prefix")
        if isinstance(value, list):
            problems.append(
                f"Node {node_id} ({ctype}): filename_prefix is link-fed and "
                f"cannot be namespaced under {prefix}/ — set a literal string."
            )
        elif isinstance(value, str) and not value.startswith(f"{prefix}/"):
            inputs["filename_prefix"] = f"{prefix}/{value.lstrip('/')}"
            changed.append(node_id)

        if "save" in ctype.lower() and "output_path" in inputs:
            path_value = inputs["output_path"]
            if isinstance(path_value, list):
                problems.append(
                    f"Node {node_id} ({ctype}): output_path is link-fed and "
                    f"cannot be namespaced — set a literal path under {prefix}/."
                )
            elif isinstance(path_value, str):
                normalized = path_value.replace("\\", "/")
                if normalized.strip() in ("", ".", "./"):
                    inputs["output_path"] = prefix
                    changed.append(node_id)
                elif os.path.isabs(path_value):
                    if f"/output/{prefix}" not in normalized.lower():
                        problems.append(
                            f"Node {node_id} ({ctype}): absolute output_path "
                            f"{path_value!r} escapes output/{prefix}/ — use a "
                            "relative path."
                        )
                elif ".." in normalized.split("/"):
                    problems.append(
                        f"Node {node_id} ({ctype}): output_path {path_value!r} "
                        "contains '..' — not allowed."
                    )
                elif normalized != prefix and not normalized.startswith(f"{prefix}/"):
                    inputs["output_path"] = f"{prefix}/{normalized.lstrip('/')}"
                    changed.append(node_id)
    return changed, problems


def combo_warnings(api_workflow: dict, object_info: dict) -> list[str]:
    """Non-blocking check: literal COMBO values that aren't valid options.

    Catches widget desyncs from UI->API conversion loudly. Warnings only —
    combo option lists can be stale (e.g. a file uploaded after /object_info
    was cached), so the server's own validation stays authoritative.
    """
    warnings: list[str] = []
    for node_id, node in api_workflow.items():
        if not isinstance(node, dict):
            continue
        schema = object_info.get(node.get("class_type", ""))
        if not schema:
            continue
        spec_by_name = {n: t for n, t, _ in _schema_inputs_in_order(schema)}
        for name, value in (node.get("inputs") or {}).items():
            options = spec_by_name.get(name)
            if (isinstance(options, list) and options
                    and all(isinstance(o, str) for o in options)
                    and isinstance(value, str) and value not in options):
                preview = ", ".join(options[:5])
                warnings.append(
                    f"Node {node_id} ({node.get('class_type')}): {name}="
                    f"{value!r} is not among the known options ({preview}, "
                    f"... {len(options)} total). Possible conversion desync "
                    "or stale file list."
                )
    return warnings


def set_input(api_workflow: dict, node_id: str, input_name: str, value: Any) -> None:
    node = api_workflow.get(str(node_id))
    if node is None:
        raise KeyError(f"No node {node_id!r} in workflow.")
    node.setdefault("inputs", {})[input_name] = value


def deep_copy(api_workflow: dict) -> dict:
    return copy.deepcopy(api_workflow)
