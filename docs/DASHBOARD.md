# Dashboard

A web app beside the ingester for seeing what's in the database, fixing what went wrong,
and changing every setting. There are no config files.

## Pages

| Page | What it does |
| --- | --- |
| **Activity** | Everything that happened, newest first: items saved, threads re-tagged, Drive files updated, and every problem (unreadable files, failed tagging, refused links, rejected emails, Slack errors). Unread items are marked, with a count in the menu. Filter by kind and source. |
| **Needs review** | Items that need a person: tagging failed, the author couldn't be worked out, an unknown source type, suggested new themes, and anything still `new`. Each one can be fixed right there. |
| **Items** | The database as a table: search, and filter by theme, narrative, author, source, visibility, status and date. Choose the columns shown, from the **Columns** menu. |
| **Item** | One item: every field editable (authors, date, label, source, visibility, status, summary, tags, text), the citation, deliverables, the source link and metadata, and a history of changes. **Re-tag** runs the tagger again. **Archive** hides it from search; **Delete** removes it after a confirmation. |
| **People** | The people table: name, role, emails, Slack ID, other spellings, still at the company. Each person's page lists everything credited to them, with theme and narrative counts. **Unmatched names** shows author names on items that match no one, with **Add as person** or **Same as…** (which credits their items to an existing person). |
| **Taxonomy** | Themes, narratives and formats, with descriptions and how many items use each. Renaming one renames it on every item. Removing one asks whether to remove it from items too. Suggested themes from the tagger are listed for adding. |
| **Coverage** | Totals (items, items without a theme, pairs with material but nothing made, pairs with no material), the theme × narrative grid shaded by how much material each pair has, and trends, all clickable through to the items. |
| **Settings** | The company name, branding, models (for tagging, and for audio, with a red line when a provider's key is missing), the tagging prompt, default visibility per source (Public or Private), Slack channels, Drive folders and ClickUp places and Chat channels (a list on the left, the picked one's rules on the right; a folder can hold several source types), email and scanned PDFs, as ordinary fields and switches. A new installation opens on Settings, then Taxonomy, until there's a company name and some themes. |
| **Add** | The web form at `/ingest` (`/` opens the dashboard), in the same header, in three steps: add, check and edit, saved. Signed out, it asks for the API token instead. |

## What changes underneath

**Three tables beside the items,** in Postgres (and SQLite, for development):

- `events`: one row per thing that happened (time, kind, level, source, item ID, message, details, read or not). Every way in writes to it: the API, the Slack bot, the email and Drive jobs. Errors that today only reach the logs end up here. A problem
  that keeps happening (an email the job retries every few minutes, a Drive file that won't
  save) stays one row, with a count and when it last happened.
- `people`: ID, name, role, emails, Slack ID, other spellings, active.
- `settings`: each save writes a new row with the full settings, who saved it and when. The newest row is current.

**Items gain `author_ids`** next to `authors`. Names stay for readable citations and plain SQL, and IDs make a person's page and renames reliable.

**Settings live in the database.** When the service first starts, it creates its tables and saves the built-in defaults ([tle/defaults.py](../tle/defaults.py)) as the first settings. Everything is changed in the dashboard from then on, and running services pick up a change within about 30 seconds. Keys are secrets, not settings.

**Item history:** each edit made in the dashboard or through the API is written to `events`, with the old and new values, which gives the history on each item's page.

## How it's built

- Served by the same FastAPI app, under `/dashboard` ([tle/dashboard.py](../tle/dashboard.py)).
  Pages are rendered on the server from [Jinja templates](../tle/templates/dashboard), and
  every form posts back and redirects, so there's no JavaScript build step. The one thing
  loaded from another site is the Inter font, from Google Fonts; a Content-Security-Policy
  added later must allow `fonts.googleapis.com` and `fonts.gstatic.com`.
- It uses the same store and pipeline code as the API. Item edits go through
  [tle/items.py](../tle/items.py), which records each change; events are written by
  [tle/events.py](../tle/events.py) from every way in.
- Sign-in ([tle/auth.py](../tle/auth.py)) is Google Cloud's IAP: the app checks IAP's signed
  header and trusts nothing else, and IAP's access list decides who gets in. For development,
  `TLE_DEV_USER` stands in, and it's ignored on Cloud Run. Every form carries a per-session
  value so other sites can't post to it.
- On Cloud Run, `deploy/deploy.sh` runs it as a second service from the same image, behind
  IAP. The main service stays as it is for Slack, ChatGPT and MCP. When developing,
  `tle serve` serves both.
- When signed in to the dashboard, the web form at `/ingest` works without the API token.

## Branding

The dashboard and the form at `/ingest` come in Eleven Content's branding. All of it lives in three
files in [tle/static](../tle/static): `theme.css` (every colour, font and radius, for light
and dark mode) and the logo, `logo-light.svg`, which always sits on a small white background
so the same file works in dark mode. `dashboard.css` holds the layout and only reads the
theme's variables.

The quickest way to use your own is **Branding** in Settings: upload a logo (SVG, PNG or
JPEG, up to 200 KB) and pick an accent colour. Buttons, links, highlights and theme tags take on the
colour in light and dark mode, with dark or white button text, whichever reads better. Logos
are saved with the other settings, so every running service shows them. SVGs with scripts or
embedded content are refused, and uploaded logos are served so that nothing in them can run.

For more than that (fonts, every colour, corner radius), change `theme.css` in your fork. The
product name, Expertise Database, and the "Crafted with expertise by Eleven Content" credit
in the footer stay as they are.

Dark mode follows the operating system until someone uses the sun and moon switch in the
header, which overrides it in that browser and is remembered there.

## Not in this version

- Roles: everyone who can sign in can do everything. The history shows who did what.
- Notifications outside the dashboard (Slack or email digests).
- Charts beyond the coverage grid and trends tables.
- Sign-in with anything other than a Google account (Microsoft 365, for example) would need
  IAP set up with Google's Identity Platform.
