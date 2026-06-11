"""Registry of prompts submitted by this MCP server.

The non-disruption contract relies on knowing which jobs are "ours": cancel
operations refuse to touch the user's own queued work unless forced, and
history can be filtered to Claude-submitted jobs. State lives in a small JSON
sidecar so it survives server restarts.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("comfy_mcp.tracking")

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
JOBS_FILE = STATE_DIR / "jobs.json"
MAX_JOBS = 300


def _load() -> dict[str, Any]:
    try:
        with open(JOBS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read %s: %s", JOBS_FILE, exc)
    return {}


def _save(jobs: dict[str, Any]) -> None:
    """Atomic-ish write via a unique temp file.

    Tracking is best-effort bookkeeping: concurrent servers can lose an
    update (last-writer-wins on the whole dict), which at worst makes one
    job temporarily report mine=false. The unique temp name plus a single
    retry covers the common Windows failure (file briefly held open by
    another reader/AV scan).
    """
    if len(jobs) > MAX_JOBS:
        oldest = sorted(jobs, key=lambda k: jobs[k].get("submitted_at", 0))
        for key in oldest[: len(jobs) - MAX_JOBS]:
            del jobs[key]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / f"jobs.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(jobs, fh, indent=1)
        for attempt in (1, 2):
            try:
                os.replace(tmp, JOBS_FILE)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.1)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def record_job(prompt_id: str, **meta: Any) -> None:
    """Never raises — a tracking failure must not fail a queued job."""
    try:
        jobs = _load()
        jobs[prompt_id] = {"submitted_at": time.time(), **meta}
        _save(jobs)
    except OSError as exc:
        log.warning("could not record job %s: %s", prompt_id, exc)


def update_job(prompt_id: str, **fields: Any) -> None:
    """Never raises — see record_job."""
    try:
        jobs = _load()
        if prompt_id in jobs:
            jobs[prompt_id].update(fields)
            _save(jobs)
    except OSError as exc:
        log.warning("could not update job %s: %s", prompt_id, exc)


def is_mine(prompt_id: str) -> bool:
    return prompt_id in _load()


def my_jobs() -> dict[str, Any]:
    return _load()
