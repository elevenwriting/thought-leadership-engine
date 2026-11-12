# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""The scheduled syncs (ClickUp, Drive, email): when each last ran and what it found, and
starting one now.

Each run records its start and finish in the store, so Settings can say when a source was last
read and whether it's being read right now. A run that starts while another is still going steps
aside, so a run started by hand and the schedule never read the same things twice at once.

Deployed, "Read now" starts the source's Cloud Run job, the same one the schedule runs. When
developing, it runs the sync in the background in this process.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

import httpx

from .config import on_cloud_run

log = logging.getLogger(__name__)

# Source -> (Cloud Run job suffix, what it's called in messages)
SOURCES = {"slack": ("slack-sync", "Slack"), "clickup": ("clickup-sync", "ClickUp"), "drive": ("drive-sync", "Drive"),
           "email": ("email-sync", "Email")}
# A run that hasn't finished after this long has died (a job's time limit is 30 minutes).
STALE = timedelta(minutes=35)
METADATA = "http://metadata.google.internal/computeMetadata/v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(source: str) -> str:
    return f"sync:{source}"


def _time(value) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def status(store, source: str) -> dict:
    """{"running", "finished", "summary", "error", "requested"} for Settings."""
    state = store.get_state(_key(source)) or {}
    started, finished, requested = (_time(state.get(k)) for k in ("started", "finished", "requested"))
    running = bool(started and (not finished or started > finished) and _now() - started < STALE)
    # Asked for a moment ago and not started yet: a job takes a few seconds to start.
    waiting = bool(requested and (not started or requested > started) and _now() - requested < timedelta(minutes=3))
    return {"running": running or waiting, "finished": finished, "summary": state.get("summary", ""),
            "error": state.get("error", ""), "requested": requested}


def begin(store, source: str) -> bool:
    """Records that a run has started. False when another is still going, so this one should stop."""
    state = store.get_state(_key(source)) or {}
    started, finished = _time(state.get("started")), _time(state.get("finished"))
    if started and (not finished or started > finished) and _now() - started < STALE:
        log.info("A %s sync started at %s is still running; this one stops.", source, started)
        return False
    store.set_state(_key(source), {**state, "started": _now().isoformat()})
    return True


def end(store, source: str, summary: str = "", error: str = "") -> None:
    state = store.get_state(_key(source)) or {}
    store.set_state(_key(source), {**state, "finished": _now().isoformat(), "summary": summary, "error": error[:500]})


def run(source: str, work, store) -> str:
    """Runs a sync (`work` returns a one-line summary), recording its start and finish."""
    from .secrets import redact

    if not begin(store, source):
        return "Another run is still going, so this one stopped."
    try:
        summary = work()
    except Exception as exc:
        end(store, source, error=redact(str(exc)))
        raise
    end(store, source, summary=summary)
    return summary


def read_now(source: str, store) -> None:
    """Starts the source's sync now. Raises ValueError with a reason when it can't."""
    if source not in SOURCES:
        raise ValueError("There's no sync for that.")
    if status(store, source)["running"]:
        raise ValueError(f"{SOURCES[source][1]} is being read already.")
    state = store.get_state(_key(source)) or {}
    if on_cloud_run():
        _start_job(SOURCES[source][0])
    else:
        threading.Thread(target=_run_here, args=(source,), daemon=True).start()
    store.set_state(_key(source), {**state, "requested": _now().isoformat()})


def _start_job(suffix: str) -> None:
    """Starts the Cloud Run job, as the service's own account (which the schedule also uses)."""
    headers = {"Metadata-Flavor": "Google"}
    project = httpx.get(f"{METADATA}/project/project-id", headers=headers, timeout=5).text.strip()
    region = httpx.get(f"{METADATA}/instance/region", headers=headers, timeout=5).text.strip().rsplit("/", 1)[-1]
    token = httpx.get(f"{METADATA}/instance/service-accounts/default/token", headers=headers, timeout=5).json()
    prefix = os.environ.get("TLE_JOB_PREFIX") or os.environ.get("K_SERVICE", "thought-leadership").removesuffix("-dashboard")
    response = httpx.post(
        f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{prefix}-{suffix}:run",
        headers={"Authorization": f"Bearer {token['access_token']}"}, json={}, timeout=20)
    if response.status_code >= 400:
        message = (response.json().get("error") or {}).get("message", response.text) if response.content else ""
        raise ValueError(f"Couldn't start the job: {message or response.status_code}")


def _run_here(source: str) -> None:
    from .config import get_config
    from .store import get_store

    try:
        sync(source, get_config(), get_store())
    except Exception:  # noqa: BLE001
        log.exception("Reading %s failed", source)


def sync(source: str, config, store) -> str:
    """One run of a source's sync, recorded; returns its summary. The CLI's sync commands use it."""
    def work() -> str:
        if source == "slack":
            from .connectors import slack

            done = slack.sync(config, store)
            return f"{done['sessions']} chat sessions saved or updated, {done['channels']} channels"
        if source == "clickup":
            from .connectors import clickup

            done = clickup.sync(config, store)
            return (f"{done['sessions']} chat sessions saved or updated, {done['chat']} channels, "
                    f"{done['tasks']} tasks, {done['docs']} Docs")
        if source == "drive":
            from .connectors.drive_sync import sync as drive

            done = drive(config, store)
            return f"{done['saved']} new files, {done['updated']} edited files read again"
        from .connectors.email_gmail import poll

        result = poll(config, store)
        return _email_summary(result)

    return run(source, work, store)


def _email_summary(result) -> str:
    if isinstance(result, dict):
        parts = [f"{v} {k.replace('_', ' ')}" for k, v in result.items() if isinstance(v, int)]
        return ", ".join(parts) or "done"
    return str(result)
