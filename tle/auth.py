# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""Who is using the dashboard.

The dashboard sits behind Google Cloud's Identity-Aware Proxy (IAP), which signs people in with
their Google account and lets in only those on its access list (set up by the deploy, changed
in the IAP console). The app checks IAP's signed header against Google's keys and the audience
(TLE_IAP_AUDIENCE), and trusts nothing else.

For development, TLE_DEV_USER (an email address) stands in for a signed-in person. It's
refused on Cloud Run, so it can never be switched on there.

The API token (TLE_API_TOKEN) is separate: it's how MCP clients, ChatGPT and scripts call the
API. Slack's messages are checked with its signing secret instead.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass

import httpx
from fastapi import Request

from .config import on_cloud_run


log = logging.getLogger(__name__)

IAP_HEADER = "x-goog-iap-jwt-assertion"
IAP_KEYS = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"


@dataclass
class User:
    email: str
    name: str = ""
    method: str = ""

    @property
    def actor(self) -> str:
        """How this person appears in the history."""
        return self.email


def session_secret() -> str:
    """For signing the session cookie: TLE_SESSION_SECRET, or one derived from the API token."""
    if os.environ.get("TLE_SESSION_SECRET"):
        return os.environ["TLE_SESSION_SECRET"]
    token = os.environ.get("TLE_API_TOKEN", "")
    if token:
        return hmac.new(token.encode(), b"tle-dashboard-session", hashlib.sha256).hexdigest()
    log.warning("Neither TLE_SESSION_SECRET nor TLE_API_TOKEN is set: dashboard sessions end when the server restarts.")
    return secrets.token_hex(32)


# --- IAP's signed header -----------------------------------------------------------

class _IapKeys:
    """IAP's public keys (key ID -> PEM), fetched once an hour."""

    def __init__(self):
        self.lock = threading.Lock()
        self.keys: dict[str, str] = {}
        self.at = 0.0

    def get(self, refresh: bool = False) -> dict[str, str]:
        with self.lock:
            if refresh or not self.keys or time.monotonic() - self.at > 3600:
                self.keys = httpx.get(IAP_KEYS, timeout=10).raise_for_status().json()
                self.at = time.monotonic()
            return self.keys


_iap_keys = _IapKeys()


def verify_iap(token: str, audience: str) -> dict:
    """Checks the signature, audience, issuer and expiry, as Google documents for IAP.
    Raises on anything wrong."""
    from google.auth import jwt

    for refresh in (False, True):  # once more with fresh keys, in case they were rotated
        try:
            claims = jwt.decode(token, certs=_iap_keys.get(refresh=refresh), audience=audience, clock_skew_in_seconds=30)
            break
        except ValueError:
            if refresh:
                raise
    if claims.get("iss") != IAP_ISSUER:
        raise ValueError(f"Not issued by IAP: {claims.get('iss')}")
    return claims


def _iap_user(request: Request) -> User | None:
    token = request.headers.get(IAP_HEADER)
    if not token:
        return None
    audience = os.environ.get("TLE_IAP_AUDIENCE")
    if not audience:
        log.warning("An IAP header arrived but TLE_IAP_AUDIENCE isn't set, so it can't be checked.")
        return None
    try:
        claims = verify_iap(token, audience)
    except Exception as exc:  # noqa: BLE001
        log.warning("Rejected an IAP header: %s", exc)
        return None
    return User(email=claims.get("email", ""), method="iap")


def dev_user() -> User | None:
    """TLE_DEV_USER, for development only: never on Cloud Run."""
    email = os.environ.get("TLE_DEV_USER", "").strip()
    if not email:
        return None
    if on_cloud_run():
        log.warning("TLE_DEV_USER is set on Cloud Run and is ignored there.")
        return None
    return User(email=email, method="dev")


def current_user(request: Request) -> User | None:
    return _iap_user(request) or dev_user()


def check_token(supplied: str) -> bool:
    expected = os.environ.get("TLE_API_TOKEN", "")
    return bool(expected) and hmac.compare_digest(supplied.strip(), expected)


def csrf_token(request: Request) -> str:
    """A per-session value every dashboard form sends back, so other sites can't post to it."""
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


def check_csrf(request: Request, supplied: str) -> bool:
    expected = request.session.get("csrf", "")
    return bool(expected) and hmac.compare_digest(expected, supplied or "")
