"""The engine's secrets (keys and tokens, from Secret Manager or the environment): trimmed when
the engine starts, and blanked out of anything it shows or saves, such as error messages."""

from __future__ import annotations

import logging
import os
import re

NAMES = ("TLE_API_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "OPENAI_API_KEY", "GEMINI_API_KEY",
         "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "CLICKUP_API_TOKEN", "DATABASE_URL", "TLE_SESSION_SECRET")
# Shapes of keys and tokens, for ones that aren't in the environment (say, quoted back by an API).
SHAPES = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}|sk-(?:proj-)?[A-Za-z0-9_-]{16,}|xox[abpr]-[A-Za-z0-9-]{8,}|"
                    r"pk_[0-9]+_[A-Z0-9]{8,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                    r"(?<=://)[^:/@\s]+:[^@\s]+(?=@)")


def trim() -> None:
    """A secret saved with a trailing newline or space (easy to do) breaks headers and logins."""
    for name in NAMES:
        value = os.environ.get(name)
        if value is not None and value != value.strip():
            os.environ[name] = value.strip()


def redact(text: str) -> str:
    """The text with any secret blanked out."""
    for name in NAMES:
        value = (os.environ.get(name) or "").strip()
        if len(value) >= 8:
            text = text.replace(value, "[hidden]")
    return SHAPES.sub("[hidden]", text)


def _redacting_log_records() -> None:
    """Every log line, from any library, has secrets blanked out before anything writes it:
    error messages often quote what was sent, and logs end up in Cloud Logging."""
    make = logging.getLogRecordFactory()
    if getattr(make, "redacts", False):
        return

    def factory(*args, **kwargs):
        record = make(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return record
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, ()
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return record

    factory.redacts = True
    logging.setLogRecordFactory(factory)


trim()
_redacting_log_records()
