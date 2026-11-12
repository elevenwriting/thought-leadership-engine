# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""Scheduled script: ingests new files from the Drive folders chosen in Settings.

Each folder says what its files are (source types), optionally their
visibility, and whether to include subfolders. Run it as a Cloud Run job on a Cloud
Scheduler trigger (see deploy/deploy.sh), or locally with `tle sync-drive`. Files already in the database (matched on
drive_file_id) are skipped, so it's safe to run as often as you like, unless they've been edited since: a file
changed since it was saved, and left alone for half an hour, is read again and tagged again, keeping its status,
visibility, deliverables and authors.

The service account needs read access to each folder: share the folder with its email.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timedelta, timezone

import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from ..attribution import attribute, parse_date, parse_fields
from ..attribution import metadata as attribution_metadata
from .. import events
from ..config import SOURCE_TYPES, Config
from ..extract import extract_text
from .. import meetings
from ..ingest import ingest_text, refresh_text
from ..store import Store

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
}


def _download(service, file: dict) -> tuple[bytes, str, str]:
    mime = file["mimeType"]
    if mime in GOOGLE_EXPORTS:
        export_mime, ext = GOOGLE_EXPORTS[mime]
        data = service.files().export(fileId=file["id"], mimeType=export_mime).execute()
        return data, f"{file['name']}{ext}", export_mime
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, service.files().get_media(fileId=file["id"], supportsAllDrives=True))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue(), file["name"], mime


def _subfolders(service, folder_id: str) -> list[str]:
    """The folder and every folder beneath it."""
    found, queue = [folder_id], [folder_id]
    while queue:
        parent, page_token = queue.pop(), None
        while True:
            response = service.files().list(
                q=f"'{parent}' in parents and trashed = false and mimeType = '{FOLDER_MIME}'",
                fields="nextPageToken, files(id)", pageToken=page_token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            for child in response.get("files", []):
                found.append(child["id"])
                queue.append(child["id"])
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    return found


def _files(service, folder_id: str):
    page_token = None
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false and mimeType != '{FOLDER_MIME}'",
            fields="nextPageToken, files(id, name, mimeType, createdTime, modifiedTime, webViewLink, "
                   "owners(emailAddress, displayName), lastModifyingUser(emailAddress, displayName))",
            pageToken=page_token, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        yield from response.get("files", [])
        page_token = response.get("nextPageToken")
        if not page_token:
            return


def _service():
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


FILE_ID = re.compile(r"/(?:document|spreadsheets|presentation|file|forms)/(?:u/\d+/)?d/([\w-]{20,})|[?&]id=([\w-]{20,})")
FILE_FIELDS = ("id, name, mimeType, createdTime, modifiedTime, webViewLink, owners(emailAddress, displayName), "
               "lastModifyingUser(emailAddress, displayName)")


def file_id(url: str) -> str | None:
    """The Drive file ID in a Docs, Sheets, Slides or Drive link."""
    match = FILE_ID.search(url)
    return (match.group(1) or match.group(2)) if match else None


def read_file(fid: str, config: Config) -> tuple[dict, str] | None:
    """A Drive file's details and text, or None when the service account can't open it (it
    hasn't been shared with it, or the file is gone)."""
    service = _service()
    try:
        file = service.files().get(fileId=fid, fields=FILE_FIELDS, supportsAllDrives=True).execute()
    except HttpError as exc:
        if exc.resp.status in (403, 404):
            return None
        raise
    data, filename, mime = _download(service, file)
    return file, extract_text(data, filename, config, mime)


def owner(file: dict) -> str:
    """Files in shared drives have no owner, so whoever last edited them stands in."""
    people = file.get("owners") or [file.get("lastModifyingUser") or {}]
    return next((p.get("emailAddress") or p.get("displayName") for p in people if p), "unknown")


def list_folders(limit: int = 2000) -> list[dict]:
    """Folders the service account can see (the ones shared with it, and their subfolders),
    for picking in Settings."""
    service, folders, page_token = _service(), [], None
    while len(folders) < limit:
        response = service.files().list(
            q=f"mimeType = '{FOLDER_MIME}' and trashed = false", fields="nextPageToken, files(id, name)",
            corpora="allDrives", supportsAllDrives=True, includeItemsFromAllDrives=True,
            pageSize=1000, pageToken=page_token,
        ).execute()
        folders += [{"id": f["id"], "name": f["name"]} for f in response.get("files", [])]
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return sorted(folders, key=lambda f: f["name"].lower())


def folder_types(folder: dict) -> list[str]:
    """The kinds of content a folder holds; older settings name one as source_type."""
    types = [t for t in folder.get("source_types") or [folder.get("source_type") or "document"] if t in SOURCE_TYPES]
    return types or ["document"]


def _save(service, file: dict, folder: dict, config: Config, store: Store) -> None:
    who_owns = owner(file)

    data, filename, mime = _download(service, file)
    fields, text = parse_fields(extract_text(data, filename, config, mime))
    # A Type: line wins; otherwise, when the folder holds several kinds of content, the
    # author check also picks which one each file is.
    types = folder_types(folder)
    given = fields.get("source_type", "").lower()
    choices = [given] if given in SOURCE_TYPES else types

    # The owner isn't always the author: a Meet transcript belongs to whoever organised
    # the meeting, and people file colleagues' documents.
    who = attribute(text, config=config, submitter=who_owns, fields=fields, source_types=choices,
                    context=f"Google Drive file: {file['name']}\nOwner: {who_owns}\nFolder holds: {', '.join(types)}")
    source_type = who.source_type or choices[0]
    given_date = parse_date(fields["date"]) if fields.get("date") else None
    ingest_text(
        text, config=config, store=store, source_type=source_type, via="drive", actor=who_owns,
        visibility=(fields.get("visibility") or "").lower() or folder.get("visibility"),
        authors=who.authors,
        date_created=given_date or who.date or datetime.fromisoformat(file["createdTime"].replace("Z", "+00:00")),
        drive_file_id=file["id"],
        source_metadata={
            "url": file.get("webViewLink"), "label": fields.get("label") or file["name"],
            "file_name": filename, "mime_type": mime, **attribution_metadata(who, who_owns),
            **({"drive_modified": file["modifiedTime"]} if file.get("modifiedTime") else {}),
        },
    )


# A file edited less than this long ago may still be being written, so it waits for a later run.
SETTLE = timedelta(minutes=30)


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def changed(item, file: dict, now: datetime | None = None) -> bool:
    """Whether a saved file has been edited since it was read, and left alone long enough to read
    again. Items saved before edit times were recorded count from when they were saved."""
    modified = _time(file.get("modifiedTime"))
    if not modified or (now or datetime.now(timezone.utc)) - modified < SETTLE:
        return False
    read = _time(item.source_metadata.get("drive_modified")) or item.date_ingested
    return modified > read


def _reread(service, file: dict, item, config: Config, store: Store) -> bool:
    """Reads an edited file again. The text is replaced and tagged again; status, visibility,
    deliverables and authors stay as they are. Returns whether the text had changed."""
    data, filename, mime = _download(service, file)
    _, text = parse_fields(extract_text(data, filename, config, mime))
    metadata = {k: v for k, v in item.source_metadata.items() if k != "notes"}
    metadata["drive_modified"] = file["modifiedTime"]
    text = meetings.prepare(text, metadata, item.source_type)
    metadata.setdefault("notes", None)  # notes split off last time, but not this time, are dropped
    if text.strip() == item.extracted_text:
        store.update(item.row_id, {"source_metadata": {**item.source_metadata, "drive_modified": file["modifiedTime"]}})
        return False
    refresh_text(item, text, config=config, store=store, authors=item.authors, source_metadata=metadata, via="drive")
    return True


def sync(config: Config, store: Store) -> dict[str, int]:
    """Saves new files and reads edited ones again. Returns {"saved": n, "updated": n}."""
    folders = config.sources.get("drive", {}).get("folders", [])
    if not folders:
        log.info("No Drive folders configured.")
        return {"saved": 0, "updated": 0}

    service = _service()
    saved = updated = 0
    for folder in folders:
        folder_ids = _subfolders(service, folder["id"]) if folder.get("include_subfolders") else [folder["id"]]
        for folder_id in folder_ids:
            for file in _files(service, folder_id):
                existing = store.find_by_drive_file_id(file["id"])
                if existing:
                    if not changed(existing, file):
                        continue
                    try:
                        if _reread(service, file, existing, config, store):
                            updated += 1
                            log.info("Read %s again: it was edited", file["name"])
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Couldn't read %s again", file["name"])
                        events.problem(store, f"Couldn't read the edited Drive file {file['name']} again: {exc}",
                                       via="drive", key=f"reread:{file['id']}", url=file.get("webViewLink"))
                    continue
                try:
                    _save(service, file, folder, config, store)
                    saved += 1
                    log.info("Saved %s", file["name"])
                except Exception as exc:  # noqa: BLE001
                    log.exception("Skipped %s", file["name"])
                    events.problem(store, f"Couldn't save Drive file {file['name']}: {exc}", via="drive", key=file["id"],
                                   url=file.get("webViewLink"), drive_file_id=file["id"])
    return {"saved": saved, "updated": updated}
