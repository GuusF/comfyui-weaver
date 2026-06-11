"""Async client for the Comfy Cloud API (cloud.comfy.org).

Runs workflows on Comfy's cloud GPUs under the user's own account — the same
account the Desktop app's cloud workspace uses, so jobs and outputs appear
there too. Authentication is an X-API-Key header.

Differences from the local API that matter here:
- job status:   GET /api/job/{id}/status -> {"status": "pending|in_progress|
                completed|failed|cancelled"}
- job details:  GET /api/jobs/{id} -> includes outputs per node id
- downloads:    GET /api/view -> 302 redirect to a signed URL
- uploads:      subfolder is ignored (cloud storage is content-addressed)
- NOTE: every execution consumes the user's Comfy Cloud credits.

The API is officially experimental and may change without notice.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("comfy_mcp.cloud")

CLOUD_URL = "https://cloud.comfy.org"
KEY_FILE = Path(__file__).resolve().parent.parent / "state" / "cloud_api_key.txt"
OBJECT_INFO_CACHE_TTL = 300

# Docs say completed|failed|cancelled, but the live API also returns
# "success" / "error" — accept both vocabularies (API is experimental).
TERMINAL_STATES = {"completed", "success", "failed", "error", "cancelled"}
SUCCESS_STATES = {"completed", "success"}


class CloudKeyMissing(RuntimeError):
    pass


class CloudApiError(RuntimeError):
    pass


def resolve_api_key() -> str:
    """Env var first, then the key file (editable without a restart)."""
    key = (os.environ.get("COMFY_CLOUD_API_KEY") or "").strip()
    if not key:
        try:
            key = KEY_FILE.read_text(encoding="utf-8").strip().splitlines()[0]
        except (OSError, IndexError):
            key = ""
    if not key:
        raise CloudKeyMissing(
            "No Comfy Cloud API key configured. Create one at "
            "platform.comfy.org (the full key is shown only once, at "
            f"creation) and save it as the only line of {KEY_FILE}, or set "
            "COMFY_CLOUD_API_KEY in .mcp.json."
        )
    return key


class CloudComfyClient:
    def __init__(self, api_key: str | None = None) -> None:
        self._explicit_key = api_key
        self._http = httpx.AsyncClient(
            base_url=CLOUD_URL,
            timeout=httpx.Timeout(120.0, connect=10.0),
            follow_redirects=True,  # /api/view 302s to a signed URL
        )
        self._object_info_cache: dict | None = None
        self._object_info_at = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._explicit_key or resolve_api_key()}

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            r = await self._http.request(method, path, headers=self._headers(),
                                          **kwargs)
        except httpx.HTTPError as exc:
            raise CloudApiError(f"Comfy Cloud request failed: {exc}") from exc
        if r.status_code == 401:
            raise CloudApiError(
                "Comfy Cloud rejected the API key (HTTP 401). The key may be "
                "masked/expired — create a new one at platform.comfy.org and "
                f"update {KEY_FILE}."
            )
        if r.status_code >= 400:
            raise CloudApiError(
                f"{method} {path} -> HTTP {r.status_code}: {r.text[:1000]}")
        return r

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        return (await self._request("GET", path, **kwargs)).json()

    # ------------------------------------------------------------ endpoints

    async def user(self) -> dict:
        return await self.get_json("/api/user")

    async def object_info(self, refresh: bool = False) -> dict:
        now = time.monotonic()
        if (refresh or self._object_info_cache is None
                or now - self._object_info_at > OBJECT_INFO_CACHE_TTL):
            self._object_info_cache = await self.get_json("/api/object_info")
            self._object_info_at = now
        return self._object_info_cache

    async def queue(self) -> dict:
        return await self.get_json("/api/queue")

    async def submit_prompt(self, prompt: dict,
                            extra_data: dict | None = None) -> dict:
        payload: dict[str, Any] = {"prompt": prompt}
        # Partner/API nodes (Kling, etc.) additionally require the comfy.org
        # key inside extra_data; harmless for ordinary nodes.
        merged = {"api_key_comfy_org": self._explicit_key or resolve_api_key()}
        merged.update(extra_data or {})
        payload["extra_data"] = merged
        try:
            r = await self._http.post("/api/prompt", json=payload,
                                      headers=self._headers())
        except httpx.HTTPError as exc:
            raise CloudApiError(f"Comfy Cloud request failed: {exc}") from exc
        if r.status_code == 400:
            try:
                body = r.json()
            except json.JSONDecodeError:
                body = None
            if isinstance(body, dict) and ("error" in body or "node_errors" in body):
                return body
        if r.status_code == 401:
            raise CloudApiError("Comfy Cloud rejected the API key (HTTP 401).")
        if r.status_code >= 400:
            raise CloudApiError(
                f"POST /api/prompt -> HTTP {r.status_code}: {r.text[:1000]}")
        return r.json()

    async def job_status(self, prompt_id: str) -> dict:
        return await self.get_json(f"/api/job/{prompt_id}/status")

    async def job_details(self, prompt_id: str) -> dict:
        return await self.get_json(f"/api/jobs/{prompt_id}")

    async def interrupt(self) -> None:
        await self._request("POST", "/api/interrupt")

    async def delete_queued(self, prompt_ids: list[str]) -> None:
        await self._request("POST", "/api/queue", json={"delete": prompt_ids})

    async def upload_image(self, data: bytes, filename: str,
                           overwrite: bool = False) -> dict:
        files = {"image": (filename, data, "application/octet-stream")}
        form = {"type": "input", "overwrite": "true" if overwrite else "false"}
        r = await self._request("POST", "/api/upload/image", files=files,
                                data=form)
        return r.json()

    async def download_view(self, filename: str, subfolder: str = "",
                            file_type: str = "output") -> bytes:
        r = await self._request(
            "GET", "/api/view",
            params={"filename": filename, "subfolder": subfolder,
                    "type": file_type},
        )
        return r.content

    # ------------------------------------------------------------- waiting

    async def wait_for_job(self, prompt_id: str, timeout_s: float = 600.0,
                           poll_interval: float = 3.0) -> dict:
        """Poll until the job reaches a terminal state; returns job details."""
        deadline = time.monotonic() + timeout_s
        last_status = "unknown"
        while time.monotonic() < deadline:
            status = (await self.job_status(prompt_id)).get("status", "unknown")
            last_status = status
            if status in TERMINAL_STATES:
                details = await self.job_details(prompt_id)
                details["_status"] = status
                return details
            await asyncio.sleep(poll_interval)
        raise TimeoutError(
            f"Cloud job {prompt_id} still {last_status!r} after "
            f"{timeout_s:.0f}s. It keeps running on the cloud — check later "
            "with cloud_job_status (credits are consumed either way)."
        )
