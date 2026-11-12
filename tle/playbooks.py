# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""Reads playbooks.md: one shared source for the MCP server, the API and the skills."""

from __future__ import annotations

from importlib import resources


def _sections() -> tuple[str, dict[str, str]]:
    text = resources.files("tle").joinpath("playbooks.md").read_text()
    intro, *parts = text.split("\n## ")
    return intro.split("\n", 1)[1].strip(), {p.split("\n", 1)[0].strip(): p.split("\n", 1)[1].strip() for p in parts}


def names() -> list[str]:
    return list(_sections()[1])


def get(name: str) -> str:
    """The ground rules plus the named playbook."""
    rules, playbooks = _sections()
    if name not in playbooks:
        raise KeyError(f"No playbook '{name}'. Choose from: {', '.join(playbooks)}")
    return f"{rules}\n\n## {name}\n\n{playbooks[name]}"
