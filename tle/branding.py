"""A company's own logo and accent colour, set in Settings and kept with the other settings.

They sit on top of the theme: an uploaded logo replaces the packaged Eleven one, and the accent
colour overrides the theme's accent everywhere it's used. The logo always sits on a white chip,
so one file works in dark mode too.
"""

from __future__ import annotations

import base64
import re

LOGO_MAX_BYTES = 200_000
LOGO_TYPES = {"image/svg+xml": "SVG", "image/png": "PNG", "image/jpeg": "JPEG"}
# What an uploaded SVG mustn't contain: anything that could run, or pull in another document.
SVG_UNSAFE = re.compile(r"<\s*(script|foreignObject|iframe|embed|object)\b|\bon[a-z]+\s*=|javascript:|<!ENTITY",
                        re.IGNORECASE)
# Served with uploaded logos, so even an SVG opened on its own can't run anything.
LOGO_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
    "X-Content-Type-Options": "nosniff",
}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def read_logo(data: bytes, filename: str) -> dict[str, str]:
    """An uploaded logo as it's stored ({"type", "data"}), or ValueError saying what's wrong."""
    if not data:
        raise ValueError(f"{filename} is empty.")
    if len(data) > LOGO_MAX_BYTES:
        raise ValueError(f"{filename} is {len(data) // 1000} KB. Logos can be up to {LOGO_MAX_BYTES // 1000} KB.")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        kind = "image/jpeg"
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = ""
        if "<svg" not in text[:2000].lower():
            raise ValueError(f"{filename} isn't an SVG, PNG or JPEG.")
        if SVG_UNSAFE.search(text):
            raise ValueError(f"{filename} contains scripts or embedded content, which logos can't have. "
                             "Export it again as a plain SVG, or use a PNG.")
        kind = "image/svg+xml"
    return {"type": kind, "data": base64.b64encode(data).decode()}


def logo(branding: dict) -> tuple[bytes, str] | None:
    """The uploaded logo, if there is one. It's shown on a white chip in light and dark mode."""
    found = branding.get("logo")
    if not found:
        return None
    return base64.b64decode(found["data"]), found["type"]


def valid_colour(value: str) -> str:
    value = (value or "").strip()
    if not HEX.match(value):
        raise ValueError(f"{value or 'That'} isn't a colour. Use the colour picker, or a value like #1F5DFF.")
    return value.upper()


def _readable_on(colour: str) -> str:
    """White or near-black text, whichever reads better on the colour."""
    r, g, b = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return "#111827" if luminance > 0.4 else "#FFFFFF"


def accent_css(colour: str | None) -> str:
    """CSS that swaps the theme's accent for the company's, in light and dark mode. It uses the
    theme's own selectors so it overrides them when it comes after theme.css."""
    if not colour or not HEX.match(colour):
        return ""
    on = _readable_on(colour)
    mix = lambda pct, other: f"color-mix(in srgb, {colour} {pct}%, {other})"  # noqa: E731
    light = {
        "--accent": colour, "--accent-hover": mix(75, "black"), "--accent-ink": mix(75, "black"), "--on-accent": on,
        "--accent-wash": mix(10, "transparent"), "--focus": colour, "--unread": colour, "--link-hover": colour,
        "--unread-row": mix(4, "transparent"), "--tag-theme-bg": mix(10, "transparent"), "--tag-theme-ink": mix(75, "black"),
    }
    ink = mix(30, "white")
    dark = {
        "--accent": colour, "--accent-hover": mix(75, "black"), "--accent-ink": ink, "--on-accent": on,
        "--accent-wash": mix(22, "transparent"), "--focus": ink, "--unread": ink, "--link-hover": ink,
        "--unread-row": mix(10, "transparent"), "--tag-theme-bg": mix(28, "transparent"), "--tag-theme-ink": ink,
    }
    block = lambda values: "; ".join(f"{k}: {v}" for k, v in values.items())  # noqa: E731
    return (f':root, [data-theme="light"] {{ {block(light)} }}\n'
            f'[data-theme="dark"] {{ {block(dark)} }}\n'
            f'@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ {block(dark)} }} }}')
