"""`tle check`: whether everything the engine needs is in place, in plain words, with what to do
about anything that isn't. People run it after setting up; agents run it after each step.

Nothing here writes anything or uses model tokens: keys are checked by listing models, and
Slack, ClickUp, Drive and the inbox by reading what they can see.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

OK, FAIL, SKIP = "ok", "fail", "skip"


@dataclass
class Result:
    name: str
    status: str
    detail: str
    fix: str = ""


def _run(name: str, check: Callable[[], Result]) -> Result:
    from .secrets import redact

    try:
        result = check()
    except Exception as exc:  # noqa: BLE001
        result = Result(name, FAIL, f"{type(exc).__name__}: {str(exc)[:300]}")
    # Error messages can quote what was sent, such as a token in a header.
    result.detail, result.fix = redact(result.detail), redact(result.fix)
    return result


def _config() -> Result:
    from .config import get_config

    config = get_config()
    where = f"the database (version {config.settings_version})" if config.settings_version else "the built-in defaults"
    detail = f"Settings come from {where}. Company: {config.company or 'not set'}; {len(config.themes)} themes."
    missing = [what for what, there in (("the company name in Settings", config.company),
                                        ("some themes in Taxonomy", config.themes)) if not there]
    fix = f"Add {' and '.join(missing)}." if missing else ""
    return Result("Config", OK, detail, fix)


def _database() -> Result:
    from .config import get_config, storage
    from .store import SearchQuery, store_for

    # Read only: store_for, unlike get_store, doesn't make missing tables.
    store = store_for(storage())
    try:
        store.search(SearchQuery(limit=1))
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "does not exist" in text or "no such table" in text:
            return Result("Database", FAIL, "It's reachable, but its tables haven't been made yet.",
                          "They're made the first time the service is used: open the dashboard, or rerun the deploy.")
        if "password authentication failed" in text:
            return Result("Database", FAIL, "The service's database password is refused.",
                          "Rerun the deploy, which makes the database user match the tle-database-url secret.")
        raise
    backend = get_config().storage.get("backend", "sqlite")
    note = "" if backend == "postgres" else " (for development only)"
    return Result("Database", OK, f"{backend} is reachable{note}.")


def _model(kind: str) -> Result:
    from .config import get_config

    config = get_config()
    model = {"tagging": config.llm, "transcription": config.transcription}[kind]
    name = "Model for tagging and authors" if kind == "tagging" else "Model for audio"
    if model is None:
        return Result(name, SKIP, "Off, so voice notes and recordings can't be read.",
                      "Pick an OpenAI or Gemini model for audio under Models in Settings, if you want it.")
    if not os.environ.get(model.api_key_env):
        if kind == "transcription":
            return Result(name, SKIP, f"{model.provider} is set for audio, but {model.api_key_env} isn't, so voice "
                          "notes and recordings will fail.", f"Add {model.api_key_env} if you want audio.")
        return Result(name, FAIL, f"{model.provider} {model.model}, but {model.api_key_env} isn't set.",
                      f"Add {model.api_key_env} as a secret (an environment variable when developing).")
    if model.provider == "anthropic":
        import anthropic

        from .llm import anthropic_client

        try:
            anthropic_client(model.api_key).models.list(limit=1)
        except anthropic.BadRequestError as exc:
            if "workspace" not in str(exc):
                raise
            return Result(name, FAIL, "The Anthropic key is an organisation-wide one, so it needs a workspace named.",
                          "Add ANTHROPIC_WORKSPACE_ID (Console > Settings > Workspaces) as a secret, "
                          "or use a key created inside a workspace.")
    elif model.provider == "openai":
        import openai

        openai.OpenAI(api_key=model.api_key).models.list()
    else:
        return Result(name, OK, f"{model.provider} {model.model}; {model.api_key_env} is set (not tested).")
    return Result(name, OK, f"{model.provider} {model.model}; the key works.")


def _api_token() -> Result:
    if os.environ.get("TLE_API_TOKEN"):
        return Result("API token", OK, "TLE_API_TOKEN is set, for MCP clients, ChatGPT and scripts.")
    return Result("API token", FAIL, "TLE_API_TOKEN isn't set, so MCP clients, ChatGPT and scripts can't call the API.",
                  "Add a long random value as a secret, e.g. from `openssl rand -hex 32`.")


def _slack() -> Result:
    from .config import get_config
    from .connectors import slack

    if not os.environ.get("SLACK_BOT_TOKEN"):
        return Result("Slack", SKIP, "No SLACK_BOT_TOKEN, so Slack is off.",
                      "To use it, create the app from deploy/slack-manifest.yaml and add its bot token and signing secret.")
    if not os.environ.get("SLACK_BOT_TOKEN", "").startswith("xoxb-"):
        return Result("Slack", FAIL, "SLACK_BOT_TOKEN isn't a bot token (it should start xoxb-).",
                      "Use the Bot User OAuth Token from the app's OAuth & Permissions page.")
    visible = {c["id"]: c for c in slack.list_channels()}
    chosen = [c["id"] for c in get_config().sources.get("slack", {}).get("channels") or []]
    missing = [c for c in chosen if c not in visible]
    not_in = [f"#{visible[c]['name']}" for c in chosen if c in visible and not visible[c]["member"]]
    detail = f"The bot sees {len(visible)} channels; {len(chosen)} chosen on Sources."
    if not os.environ.get("SLACK_SIGNING_SECRET"):
        return Result("Slack", FAIL, detail + " SLACK_SIGNING_SECRET isn't set, so Slack's events would be refused.",
                      "Add the Signing Secret from the app's Basic Information page.")
    if "users:read.email" not in slack.scopes():
        return Result("Slack", FAIL, detail + " It can't read people's emails, so Slack IDs aren't found from email.",
                      "Add the users:read.email scope under OAuth & Permissions in the Slack app, then reinstall it.")
    if missing:
        return Result("Slack", FAIL, detail + f" It can't see {', '.join(missing)}.",
                      "Invite the bot to those channels (/invite), or remove them on Sources.")
    if not_in:
        return Result("Slack", FAIL, detail + f" It isn't in {', '.join(not_in)} yet.",
                      "Save Settings so the bot joins them, or /invite it.")
    return Result("Slack", OK, detail, "" if chosen else "Pick channels under Slack on Sources.")


def _clickup() -> Result:
    from .config import get_config
    from .connectors import clickup

    if not os.environ.get("CLICKUP_API_TOKEN"):
        return Result("ClickUp", SKIP, "No CLICKUP_API_TOKEN, so ClickUp is off.",
                      "To use it, add a personal API token (pk_...) from a dedicated ClickUp user.")
    config = get_config()
    client = clickup.ClickUp(patient=False)
    team = clickup.workspace(client, config)
    settings = config.sources.get("clickup", {})
    places, channels = settings.get("places") or [], settings.get("channels") or []
    detail = f"The token sees workspace {team.get('name')}; {len(places)} places and {len(channels)} Chat channels chosen."
    return Result("ClickUp", OK, detail, "" if places or channels else "Pick places and channels under ClickUp on Sources.")


def _drive() -> Result:
    from .config import get_config
    from .connectors import drive_sync

    chosen = get_config().sources.get("drive", {}).get("folders") or []
    try:
        visible = {f["id"] for f in drive_sync.list_folders()}
    except Exception as exc:  # noqa: BLE001
        if not chosen:
            return Result("Google Drive", SKIP, f"No folders chosen, and Drive can't be read here ({str(exc)[:120]}).",
                          "Deployed, Drive is read as the service account. Locally, see the README's Drive section.")
        raise
    missing = [f["id"] for f in chosen if f.get("id") not in visible]
    detail = f"{len(visible)} folder{'s' if len(visible) != 1 else ''} shared with this account; {len(chosen)} chosen on Sources."
    if missing:
        return Result("Google Drive", FAIL, detail + f" Can't see {', '.join(missing)}.",
                      "Share those folders with the service account (Viewer), or remove them on Sources.")
    return Result("Google Drive", OK, detail, "" if chosen else "Share a folder with the service account and pick it on Sources.")


def _email() -> Result:
    from .config import get_config

    settings = get_config().sources.get("email", {})
    inbox = settings.get("gmail", {}).get("inbox")
    if not inbox:
        return Result("Email inbox", SKIP, "No inbox set, so email is off.",
                      "Set the inbox address under Email on Sources, if you want one.")
    from googleapiclient.discovery import build

    from .connectors.email_gmail import _credentials

    service = build("gmail", "v1", credentials=_credentials(inbox, settings.get("gmail", {}).get("service_account")),
                    cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    return Result("Email inbox", OK, f"Can read {profile.get('emailAddress', inbox)}.")


CHECKS: list[tuple[str, Callable[[], Result]]] = [
    ("Config", _config), ("Database", _database), ("Model for tagging and authors", lambda: _model("tagging")),
    ("Model for audio", lambda: _model("transcription")), ("API token", _api_token),
    ("Slack", _slack), ("ClickUp", _clickup), ("Google Drive", _drive), ("Email inbox", _email),
]


def run() -> list[Result]:
    results = []
    for name, check in CHECKS:
        results.append(_run(name, check))
        if name == "Config" and results[-1].status == FAIL:
            break  # nothing else can be checked without it
    return results


def report(results: list[Result]) -> str:
    marks = {OK: "✓", FAIL: "✗", SKIP: "–"}
    lines = []
    for r in results:
        lines.append(f"{marks[r.status]} {r.name}: {r.detail}")
        if r.fix:
            lines.append(f"    {r.fix}")
    failed = sum(r.status == FAIL for r in results)
    lines.append("")
    lines.append(f"{failed} problem{'s' if failed != 1 else ''} to fix." if failed else "Everything that's set up works.")
    return "\n".join(lines)
