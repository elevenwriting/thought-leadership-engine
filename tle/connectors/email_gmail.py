"""Reads a Google Workspace inbox through the Gmail API.

The service account reads the inbox using domain-wide delegation, so there are no
passwords. A Workspace admin grants it once (see README). Processed mail is labelled
`processed_label` and rejected mail `rejected_label`; the inbox is otherwise left alone,
so people can still read it.
"""

from __future__ import annotations

import base64
import logging

import google.auth
from google.auth import iam
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build

from ..config import Config
from ..store import Store
from .. import events
from .email import process_raw

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def _credentials(inbox: str, service_account_email: str | None):
    """Service account credentials acting as the inbox user.

    On Cloud Run the service account has no key file, so the delegated token is signed
    through the IAM API instead. Locally, the same works with your own gcloud login as
    long as you can act as the service account (Service Account Token Creator), or with
    a key file in GOOGLE_APPLICATION_CREDENTIALS."""
    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if isinstance(source, service_account.Credentials):
        return source.with_scopes([SCOPE]).with_subject(inbox)
    request = Request()
    source.refresh(request)
    email = service_account_email or getattr(source, "service_account_email", None)
    if not email or email == "default":
        raise RuntimeError("Set sources.email.gmail.service_account to the service account's email.")
    signer = iam.Signer(request, source, email)
    return service_account.Credentials(signer, email, TOKEN_URI, scopes=[SCOPE], subject=inbox)


def _label_ids(service, names: list[str]) -> dict[str, str]:
    existing = {l["name"]: l["id"] for l in service.users().labels().list(userId="me").execute().get("labels", [])}
    for name in names:
        if name not in existing:
            created = service.users().labels().create(
                userId="me", body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
            ).execute()
            existing[name] = created["id"]
    return {name: existing[name] for name in names}


def poll(config: Config, store: Store) -> dict:
    settings = config.sources.get("email", {}).get("gmail", {})
    inbox = settings.get("inbox")
    if not inbox:
        log.info("No inbox set under Email on Sources, so there's nothing to read.")
        return {"saved_emails": 0, "items": 0, "rejected": 0}
    processed = settings.get("processed_label", "tle-saved")
    rejected = settings.get("rejected_label", "tle-rejected")

    service = build("gmail", "v1", credentials=_credentials(inbox, settings.get("service_account")),
                    cache_discovery=False)
    labels = _label_ids(service, [processed, rejected])
    query = f"in:inbox -label:{processed} -label:{rejected}"

    counts = {"saved_emails": 0, "items": 0, "rejected": 0}
    page_token = None
    while True:
        response = service.users().messages().list(userId="me", q=query, pageToken=page_token).execute()
        for ref in response.get("messages", []):
            message = service.users().messages().get(userId="me", id=ref["id"], format="raw").execute()
            raw = base64.urlsafe_b64decode(message["raw"])
            try:
                accepted, items = process_raw(raw, config, store)
            except Exception as exc:  # noqa: BLE001
                # Left unlabelled, so the next run tries again.
                log.exception("Failed on Gmail message %s", ref["id"])
                events.problem(store, f"Couldn't process an email (Gmail message {ref['id']}); it will be retried: {exc}",
                               via="email", key=f"gmail:{ref['id']}")
                continue
            label = processed if accepted else rejected
            service.users().messages().modify(userId="me", id=ref["id"], body={"addLabelIds": [labels[label]]}).execute()
            counts["saved_emails" if accepted else "rejected"] += 1
            counts["items"] += items
        page_token = response.get("nextPageToken")
        if not page_token:
            return counts
