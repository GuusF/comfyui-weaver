"""Async client for the ComfyUI HTTP/WebSocket API.

Attaches to an already-running ComfyUI instance (Desktop app or headless).
It never starts, stops, or restarts ComfyUI itself — that is part of the
non-disruption contract: the user's interactive session is left alone.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
import uuid
from typing import Any, Callable

import httpx

log = logging.getLogger("comfy_mcp.client")

# ComfyUI Desktop defaults to port 8000; standalone installs default to 8188.
DEFAULT_PORTS = (8000, 8188)
OBJECT_INFO_CACHE_TTL = 300  # seconds


class ComfyUnavailable(RuntimeError):
    """Raised when no running ComfyUI server can be reached."""


class ComfyApiError(RuntimeError):
    """Raised when ComfyUI returns an HTTP error; includes the response body."""


class ComfyClient:
    def __init__(self, base_url: str | None = None) -> None:
        configured = (base_url or os.environ.get("COMFYUI_URL") or "").strip()
        self._configured_url = configured.rstrip("/")
        self._base_url: str | None = self._configured_url or None
        self._verified = False
        # Stable client id so progress events for our prompts are routed to us.
        self.client_id = str(uuid.uuid4())
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))
        self._object_info_cache: dict[str, Any] | None = None
        self._object_info_fetched_at = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ url

    async def base_url(self) -> str:
        """Return the base URL of a live server, probing known ports if needed."""
        if self._base_url and self._verified:
            return self._base_url
        if self._configured_url:
            candidates = [self._configured_url]
        elif self._base_url:
            candidates = [self._base_url] + [
                f"http://127.0.0.1:{p}" for p in DEFAULT_PORTS
            ]
        else:
            candidates = [f"http://127.0.0.1:{p}" for p in DEFAULT_PORTS]
        for url in candidates:
            try:
                r = await self._http.get(f"{url}/system_stats", timeout=3.0)
                if r.status_code == 200:
                    self._base_url = url
                    self._verified = True
                    return url
            except httpx.HTTPError:
                continue
        self._verified = False
        if self._configured_url:
            raise ComfyUnavailable(
                f"No ComfyUI server reachable at {self._configured_url} "
                "(set by COMFYUI_URL). Start ComfyUI and retry."
            )
        raise ComfyUnavailable(
            "No running ComfyUI server found on http://127.0.0.1:8000 or :8188. "
            "Start the ComfyUI Desktop app (or a headless server) and retry. "
            "Set COMFYUI_URL if it listens elsewhere."
        )

    async def ws_url(self) -> str:
        base = await self.base_url()
        scheme = "wss" if base.startswith("https") else "ws"
        host = base.split("://", 1)[1]
        return f"{scheme}://{host}/ws?clientId={self.client_id}"

    # ------------------------------------------------------------- transport

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        base = await self.base_url()
        try:
            r = await self._http.request(method, f"{base}{path}", **kwargs)
        except httpx.HTTPError as exc:
            # The server may have been restarted on a different port; re-probe
            # on the next call.
            self._verified = False
            raise ComfyUnavailable(f"Request to ComfyUI failed: {exc}") from exc
        if r.status_code >= 400:
            body = r.text[:2000]
            raise ComfyApiError(f"{method} {path} -> HTTP {r.status_code}: {body}")
        return r

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        return (await self._request("GET", path, **kwargs)).json()

    async def post_json(self, path: str, payload: Any = None, **kwargs: Any) -> Any:
        r = await self._request("POST", path, json=payload, **kwargs)
        if not r.content:
            return {}
        try:
            return r.json()
        except json.JSONDecodeError:
            return {"raw": r.text[:2000]}

    # ------------------------------------------------------------- endpoints

    async def system_stats(self) -> dict:
        return await self.get_json("/system_stats")

    async def object_info(
        self, class_type: str | None = None, refresh: bool = False
    ) -> dict:
        """Node schemas. The full payload is large, so it is cached briefly."""
        if class_type:
            data = await self.get_json(
                f"/object_info/{urllib.parse.quote(class_type)}"
            )
            return data
        now = time.monotonic()
        if (
            refresh
            or self._object_info_cache is None
            or now - self._object_info_fetched_at > OBJECT_INFO_CACHE_TTL
        ):
            self._object_info_cache = await self.get_json("/object_info")
            self._object_info_fetched_at = now
        return self._object_info_cache

    async def model_folders(self) -> list:
        return await self.get_json("/models")

    async def models_in_folder(self, folder: str) -> list:
        return await self.get_json(f"/models/{urllib.parse.quote(folder)}")

    async def embeddings(self) -> list:
        return await self.get_json("/embeddings")

    async def queue(self) -> dict:
        return await self.get_json("/queue")

    async def history(self, prompt_id: str | None = None, max_items: int = 64) -> dict:
        if prompt_id:
            return await self.get_json(f"/history/{prompt_id}")
        return await self.get_json("/history", params={"max_items": max_items})

    async def submit_prompt(
        self, prompt: dict, extra_data: dict | None = None, prompt_id: str | None = None
    ) -> dict:
        """POST /prompt. Always queues at the back — never uses `front`.

        Validation failures (HTTP 400 with {"error", "node_errors"}) are
        returned as-is so callers get per-node detail instead of a truncated
        error string.
        """
        payload: dict[str, Any] = {"prompt": prompt, "client_id": self.client_id}
        if extra_data:
            payload["extra_data"] = extra_data
        if prompt_id:
            payload["prompt_id"] = prompt_id
        base = await self.base_url()
        try:
            r = await self._http.post(f"{base}/prompt", json=payload)
        except httpx.HTTPError as exc:
            self._verified = False
            raise ComfyUnavailable(f"Request to ComfyUI failed: {exc}") from exc
        if r.status_code == 400:
            try:
                body = r.json()
            except json.JSONDecodeError:
                body = None
            if isinstance(body, dict) and ("error" in body or "node_errors" in body):
                return body
        if r.status_code >= 400:
            raise ComfyApiError(f"POST /prompt -> HTTP {r.status_code}: {r.text[:2000]}")
        return r.json()

    async def interrupt(self, prompt_id: str | None = None) -> dict:
        payload = {"prompt_id": prompt_id} if prompt_id else None
        return await self.post_json("/interrupt", payload)

    async def delete_queued(self, prompt_ids: list[str]) -> dict:
        # Deletes specific pending items only. Never sends {"clear": true}.
        return await self.post_json("/queue", {"delete": prompt_ids})

    async def free(self, unload_models: bool = False) -> dict:
        return await self.post_json(
            "/free", {"free_memory": True, "unload_models": unload_models}
        )

    async def upload_image(
        self,
        data: bytes,
        filename: str,
        subfolder: str = "claude",
        overwrite: bool = False,
        image_type: str = "input",
    ) -> dict:
        base = await self.base_url()
        files = {"image": (filename, data, "application/octet-stream")}
        form = {
            "subfolder": subfolder,
            "type": image_type,
            "overwrite": "true" if overwrite else "false",
        }
        try:
            r = await self._http.post(f"{base}/upload/image", files=files, data=form)
        except httpx.HTTPError as exc:
            self._verified = False
            raise ComfyUnavailable(f"Upload to ComfyUI failed: {exc}") from exc
        if r.status_code >= 400:
            raise ComfyApiError(f"POST /upload/image -> {r.status_code}: {r.text[:500]}")
        return r.json()

    async def view(
        self, filename: str, file_type: str = "output", subfolder: str = ""
    ) -> bytes:
        r = await self._request(
            "GET",
            "/view",
            params={"filename": filename, "type": file_type, "subfolder": subfolder},
        )
        return r.content

    async def internal_files(self, directory_type: str = "output") -> Any:
        return await self.get_json(f"/internal/files/{directory_type}")

    async def internal_logs_raw(self) -> dict:
        return await self.get_json("/internal/logs/raw")

    # ------------------------------------------------------------- userdata

    async def userdata_list(self, directory: str = "workflows") -> list:
        return await self.get_json(
            "/userdata",
            params={"dir": directory, "recurse": "true", "split": "false"},
        )

    async def userdata_get(self, relative_path: str) -> Any:
        encoded = urllib.parse.quote(relative_path, safe="")
        r = await self._request("GET", f"/userdata/{encoded}")
        try:
            return r.json()
        except json.JSONDecodeError:
            return r.text

    async def userdata_put(
        self, relative_path: str, content: Any, overwrite: bool = False
    ) -> dict:
        encoded = urllib.parse.quote(relative_path, safe="")
        body = content if isinstance(content, (str, bytes)) else json.dumps(content)
        if isinstance(body, str):
            body = body.encode("utf-8")
        r = await self._request(
            "POST",
            f"/userdata/{encoded}",
            params={"overwrite": "true" if overwrite else "false"},
            content=body,
        )
        try:
            return r.json()
        except json.JSONDecodeError:
            return {"path": relative_path}

    # ------------------------------------------------------------ wait logic

    @staticmethod
    def sorted_pending(queue_payload: dict) -> list:
        """queue_pending in actual execution order.

        /queue serves PromptQueue's raw heapq array, which is only partially
        ordered — sort by (number, prompt_id) to match pop order.
        """
        pending = [i for i in queue_payload.get("queue_pending", []) if len(i) > 1]
        pending.sort(key=lambda i: (i[0], str(i[1])))
        return pending

    async def queue_position(self, prompt_id: str) -> dict:
        """Where a prompt sits: running, position N in queue, or absent."""
        q = await self.queue()
        for item in q.get("queue_running", []):
            if len(item) > 1 and item[1] == prompt_id:
                return {"state": "running"}
        for idx, item in enumerate(self.sorted_pending(q)):
            if item[1] == prompt_id:
                return {"state": "pending", "position": idx + 1}
        return {"state": "absent"}

    async def wait_for_prompt(
        self,
        prompt_id: str,
        timeout_s: float = 300.0,
        poll_interval: float = 1.5,
        progress_cb: Callable[[dict], None] | None = None,
    ) -> dict:
        """Wait for a prompt to finish; returns its history entry.

        Completion is detected by polling /history (authoritative). A
        best-effort websocket listener adds live progress detail when
        available; any websocket failure silently degrades to polling.
        """
        deadline = time.monotonic() + timeout_s
        progress: dict[str, Any] = {}
        ws_task = asyncio.create_task(
            self._ws_progress_listener(prompt_id, progress, deadline)
        )

        def is_done(entry: dict | None) -> bool:
            return bool(entry) and (
                entry.get("status", {}).get("completed") is not None
                or entry.get("outputs")
            )

        try:
            while time.monotonic() < deadline:
                hist = await self.history(prompt_id)
                entry = hist.get(prompt_id)
                if is_done(entry):
                    entry["_progress"] = dict(progress)
                    return entry
                if progress_cb and progress:
                    progress_cb(dict(progress))
                await asyncio.sleep(poll_interval)
            # The job may have finished during the last sleep: check the queue
            # first, then history, so a completion between the two calls is
            # still caught by the final history read.
            pos = await self.queue_position(prompt_id)
            hist = await self.history(prompt_id)
            entry = hist.get(prompt_id)
            if is_done(entry):
                entry["_progress"] = dict(progress)
                return entry
            if pos["state"] in ("running", "pending"):
                detail = f"It keeps running (state: {pos}) — check later with job_status."
            else:
                detail = ("It is no longer queued (finished, cancelled, or "
                          "unknown) — use job_status to fetch any results.")
            raise TimeoutError(
                f"Prompt {prompt_id} not finished after {timeout_s:.0f}s. {detail}"
            )
        finally:
            ws_task.cancel()
            try:
                await ws_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _ws_progress_listener(
        self, prompt_id: str, progress: dict, deadline: float
    ) -> None:
        """Best-effort: record progress events for prompt_id into `progress`."""
        try:
            import websockets
        except ImportError:
            return
        try:
            url = await self.ws_url()
            async with websockets.connect(url, max_size=32 * 1024 * 1024) as ws:
                while time.monotonic() < deadline:
                    remaining = max(0.5, deadline - time.monotonic())
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=min(remaining, 10.0)
                        )
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(raw, bytes):
                        continue  # binary preview frames — ignore
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    data = msg.get("data") or {}
                    if data.get("prompt_id") not in (None, prompt_id):
                        continue
                    mtype = msg.get("type")
                    if mtype == "progress":
                        progress["value"] = data.get("value")
                        progress["max"] = data.get("max")
                    elif mtype == "progress_text":
                        progress["text"] = data.get("text")
                    elif mtype == "executing" and data.get("node") is not None:
                        progress["node"] = data.get("node")
                    elif mtype == "execution_error":
                        progress["error"] = data.get("exception_message")
        except Exception as exc:  # noqa: BLE001 — progress is purely optional
            log.debug("websocket progress listener stopped: %s", exc)
