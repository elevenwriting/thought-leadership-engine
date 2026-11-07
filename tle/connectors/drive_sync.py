"""Scheduled script: ingests new files from the Drive folders chosen in Settings.

Each folder says what its files are (source types), optionally their
visibility, and whether to include subfolders. Run it as a Cloud Run job on a Cloud
Scheduler trigger (see deploy/deploy.sh), or locally with `tle sync-drive`. Files already in the database (matched on
drive_file_id) are skipped, so it's safe to run as often as you like.

The service account needs read access to each folder: share the folder with its email.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime

import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from ..attribution import attribute, parse_date, parse_fields
from ..attribution import metadata as attribution_metadata
from .. import events
from ..config import SOURCE_TYPES, Config
from ..extract import extract_text
from ..ingest import ingest_text
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
            fields="nextPageToken, files(id, name, mimeType, createdTime, webViewLink, owners(emailAddress, displayName), "
                   "lastModifyingUser(emailAddress, displayName))",
            pageToken=page_token, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        yield from response.get("files", [])
        page_token = response.get("nextPageToken")
        if not page_token:
            return


def _service():
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


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
    # Files in shared drives have no owner, so fall back to whoever last edited them.
    people = file.get("owners") or [file.get("lastModifyingUser") or {}]
    owner = next((p.get("emailAddress") or p.get("displayName") for p in people if p), "unknown")

    data, filename, mime = _download(service, file)
    fields, text = parse_fields(extract_text(data, filename, config, mime))
    # A Type: line wins; otherwise, when the folder holds several kinds of content, the
    # author check also picks which one each file is.
    types = folder_types(folder)
    given = fields.get("source_type", "").lower()
    choices = [given] if given in SOURCE_TYPES else types

    # The owner isn't always the author: a Meet transcript belongs to whoever organised
    # the meeting, and people file colleagues' documents.
    who = attribute(text, config=config, submitter=owner, fields=fields, source_types=choices,
                    context=f"Google Drive file: {file['name']}\nOwner: {owner}\nFolder holds: {', '.join(types)}")
    source_type = who.source_type or choices[0]
    given_date = parse_date(fields["date"]) if fields.get("date") else None
    ingest_text(
        text, config=config, store=store, source_type=source_type, via="drive", actor=owner,
        visibility=(fields.get("visibility") or "").lower() or folder.get("visibility"),
        authors=who.authors,
        date_created=given_date or who.date or datetime.fromisoformat(file["createdTime"].replace("Z", "+00:00")),
        drive_file_id=file["id"],
        source_metadata={
            "url": file.get("webViewLink"), "label": fields.get("label") or file["name"],
            "file_name": filename, "mime_type": mime, **attribution_metadata(who, owner),
        },
    )


def sync(config: Config, store: Store) -> int:
    folders = config.sources.get("drive", {}).get("folders", [])
    if not folders:
        log.info("No Drive folders configured.")
        return 0

    service = _service()
    saved = 0
    for folder in folders:
        folder_ids = _subfolders(service, folder["id"]) if folder.get("include_subfolders") else [folder["id"]]
        for folder_id in folder_ids:
            for file in _files(service, folder_id):
                if store.find_by_drive_file_id(file["id"]):
                    continue
                try:
                    _save(service, file, folder, config, store)
                    saved += 1
                    log.info("Saved %s", file["name"])
                except Exception as exc:  # noqa: BLE001
                    log.exception("Skipped %s", file["name"])
                    events.problem(store, f"Couldn't save Drive file {file['name']}: {exc}", via="drive", key=file["id"],
                                   url=file.get("webViewLink"), drive_file_id=file["id"])
    return saved
