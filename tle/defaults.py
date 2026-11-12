# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""The settings a new installation starts with, before anyone saves anything in Settings.

Only generic things live here: narratives, formats, how each source type is treated, and how
files and links are read. The company, its people and its themes start empty, and the
dashboard asks for them first.
"""

from __future__ import annotations

import copy
import os
from typing import Any

# Each provider's starting model, for tagging and for audio. Changed under Models in Settings.
MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-5", "gemini": "gemini-2.5-flash"}
# Cheaper models for quick jobs, such as finding the topics in a chat session.
FAST_MODELS = {"anthropic": "claude-haiku-5-5", "openai": "gpt-5-mini", "gemini": "gemini-2.5-flash-lite"}
AUDIO_MODELS = {"openai": "gpt-4o-transcribe", "gemini": "gemini-2.5-flash"}
KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}

_DEFAULTS: dict[str, Any] = {
    "company": "",
    "extraction": None,
    "themes": [],
    "narratives": [
        {"name": "Counter-narrative", "description": "A strongly held opinion that goes against the prevailing view."},
        {"name": "Predictions", "description": "A forecast about where the industry, market or practice is heading."},
        {"name": "Myth busting", "description": "Takes a widely held belief and shows why it's wrong or incomplete."},
        {"name": "Industry analysis", "description": "Reads a trend, event or data point and explains what it means."},
        {"name": "Frameworks", "description": "A repeatable model, process or method the company uses."},
        {"name": "Behind the scenes", "description": "How the company actually works, decides, fails and learns."},
        {"name": "Brand narrative", "description": "The company's story, mission and reason for existing."},
        {"name": "Data storytelling", "description": "A finding from the company's own data, told as a story."},
    ],
    "formats": ["LinkedIn post", "Blog article", "Whitepaper", "Newsletter", "Podcast episode",
                "Short-form video", "Infographic", "Case study"],
    "prompts": {},
    "sources": {
        # Public: already published or said on the record. Private: internal, and cited as such.
        "default_visibility": {
            "slack": "private", "clickup": "private", "meeting": "private", "document": "private",
            "voicenote": "private", "braindump": "private", "interview": "public", "linkedin": "public",
            "marketing": "public", "web": "public", "email": "private", "other": "private",
        },
        "slack": {"channels": []},
        "drive": {"folders": []},
        "clickup": {"chat_look_back_days": 7, "places": [], "channels": []},
        "email": {"only_people": False, "gmail": {"inbox": "", "processed_label": "tle-saved",
                                                  "rejected_label": "tle-rejected"}},
        "files": {"pdf_fallback": "llm", "min_chars_per_page": 100, "pages_per_call": 8, "max_pdf_pages": 50},
        "links": {"render": "auto", "min_chars": 400, "timeout_seconds": 30},
    },
}


def defaults() -> dict[str, Any]:
    """A fresh copy, safe to change. The models follow whichever keys are set: tagging with the
    first of Anthropic, OpenAI and Gemini that has one, and audio with OpenAI or Gemini if either
    does (Claude doesn't take audio)."""
    data = copy.deepcopy(_DEFAULTS)
    have = [p for p in KEYS if os.environ.get(KEYS[p])]
    provider = have[0] if have else "anthropic"
    data["llm"] = {"provider": provider, "model": MODELS[provider], "max_input_chars": 150_000}
    data["fast"] = {"provider": provider, "model": FAST_MODELS[provider]}
    audio = next((p for p in have if p in AUDIO_MODELS), None)
    data["transcription"] = {"provider": audio, "model": AUDIO_MODELS[audio]} if audio else None
    return data
