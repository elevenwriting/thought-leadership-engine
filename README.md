# Expertise Database

AI content systems with true human EEAT: capture what your people actually think, organise
it with AI, and build content on it from Claude, ChatGPT or any MCP client.

From the brightonSEO talk *Best of Both Worlds: AI Content Systems with True Human EEAT*
by [Christian Rigg](https://linkedin.com/in/christian-rigg), [Eleven Content](https://11content.com).

```
 Capture                    Parse                     Organise               Query, ideate, refine
 ─────────                  ─────                     ────────               ─────────────────────
 Web form  ─┐                                                                ┌─ Claude (MCP + skill)
 Slack bot ─┤   Cloud Run   extract text ─► LLM tags  Postgres: a row per   ├─ ChatGPT (custom GPT)
 Email job ─┼─► /ingest ──► (PDF, DOCX,     themes,  ─► item, cited and     ─┼─ Cursor, any MCP client
 Drive job ─┤               audio, images)  narratives  flagged             └─ REST API / CLI
 ClickUp   ─┤
 Links     ─┘                               + summary
```

## What it does

**Capture** thought leadership from the people who have it (founders, team leads, customers)
in the places it already lives: Slack, meeting transcripts, documents, voice notes,
interviews and published posts.

**Parse** each item with the LLM of your choice (Claude, OpenAI or Gemini), which tags it
against *your* themes (values, pain points, outcomes, objections) and a set of narratives
(counter-narrative, predictions, myth busting, industry analysis, frameworks, behind the
scenes, brand narrative, data storytelling), and writes a summary.

**Organise** it in a real database, one row per item, with author, source, date, citation
and visibility. Tags make it countable, so you can see coverage gaps and trends, not just
search it.

**Query** it from Claude or ChatGPT with 13 built-in playbooks: stance, coverage, updates,
trends, atomise, aggregate, case studies, mining, debate, foresight, post-mortems, briefs
and media ideas.

### Governance, built in

- **Authors are tagged on ingestion**, worked out from the content and matched to your People by email or Slack ID.
- **Only chosen channels are automated.** Slack, Drive and ClickUp only read the channels, folders and spaces you pick, and the email inbox can be limited to your People.
- **Every item is public or private.** Each source type has a default (a LinkedIn post is
  public, a meeting transcript private) that you can override per item.
- **Private items are returned, but flagged.** They can inform content, but every result
  carries a `private_flag`, and the playbooks tell the model not to quote or publish them
  without the author's approval.
- **Every insight cites its source**, e.g. `J SMITH - SLACK #NEWS - 19.09.2026`.
- **Meeting notes are never quoted.** Notes from Gemini, Granola or a notetaker are a summary,
  not anyone's words, so they're cited as notes and flagged `paraphrased`; transcripts are
  quoted as usual. See [Meeting notes and transcripts](#meeting-notes-and-transcripts).

## Set it up

It runs in your own Google Cloud project: the API and dashboard on Cloud Run, a Postgres
database on Cloud SQL, keys in Secret Manager, and Google sign-in in front of the dashboard.
Everything else (your company, people, themes, models and sources) is set in the dashboard.
There are no config files to edit.

Needs the [gcloud CLI](https://cloud.google.com/sdk/docs/install) (or Cloud Shell) and a
Google Cloud project with billing. Setting it up with an AI agent such as Claude Code? Point
it at [docs/SETUP_WITH_AN_AGENT.md](docs/SETUP_WITH_AN_AGENT.md), which it reads automatically
through `CLAUDE.md`.

```bash
git clone https://github.com/elevenwriting/thought-leadership-engine.git
cd thought-leadership-engine
gcloud auth login
PROJECT=your-gcp-project DASHBOARD_ACCESS=domain:yourcompany.com ./deploy/deploy.sh
```

It asks which AI should read and tag what comes in (Claude, OpenAI or Gemini) and for that
provider's API key, without showing it. Then open the dashboard address it prints. It starts on Settings: add your company name and
check the model, then your themes on Taxonomy and your people on People. See
[Deploy](#deploy) for what the script does and its options.

## The dashboard

At `/dashboard`, for seeing what's in the database, fixing what went wrong and changing
settings without editing files:

| Page | |
| --- | --- |
| **Items** | Where the dashboard opens. Two views light up when something needs attention: **Needs review** (orange: tagging failed, the author is unclear, an unknown source type, or new theme ideas, each fixable in place) and **Problems** (red: unreadable files, refused links, rejected emails, Slack, Drive or ClickUp errors, each dismissable). The menu shows both counts. Search and filter everything; open an item to edit any field, tag it again, add deliverables, or delete it. Each item keeps a history of its changes. |
| **People** | Who items are credited to, with every email, Slack ID and other spelling. Names on items that match no one are listed, to add as a person or credit to someone. Renaming a person renames them on their items. |
| **Taxonomy** | Themes, narratives and formats, with how many items use each. Renaming one renames it on every item. |
| **Coverage** | The theme × narrative grid, and trends against the previous period. |
| **Sources** | Slack, Google Drive, ClickUp, email and files, each with when it was last read and a Read now button, and default visibility. |
| **Settings** | Company name, models and tagging instructions. |

**Sign-in** is Google's Identity-Aware Proxy (IAP), which the deploy sets up: the dashboard
runs as its own Cloud Run service, and only the people, groups or domains on IAP's access
list can reach it, with their Google account. Add or remove people in the IAP console. Anyone
who can sign in can do everything, and the history records who did what.

**Settings live in the database.** A new installation starts from built-in defaults (the
narratives, formats, how each source type is treated, the Claude model) and everything is
changed in the dashboard. Running services pick up a change within 30 seconds.

## Settings and secrets

Everything a company sets is in the dashboard's **Sources**, **Settings**, **Taxonomy** and **People**:
the company name, the models (for tagging, and for audio), the tagging
instructions, default visibility, themes, narratives, formats, people, and each way in.

Keys are secrets in Secret Manager, never settings. The deploy stores the ones you give it:

| Secret | For |
| --- | --- |
| `TLE_API_TOKEN` | The key to the API, for Claude and other MCP clients, ChatGPT and scripts (generated if you don't give one). Anyone with it can read and change everything, so treat it like a password |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` | The models picked in Settings. Audio needs OpenAI or Gemini |
| `ANTHROPIC_WORKSPACE_ID` | Only for an organisation-wide Anthropic key, not one made inside a workspace |
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` | The Slack app |
| `CLICKUP_API_TOKEN` | ClickUp, a personal token from a dedicated user |
| `DATABASE_URL` | Made for you with Cloud SQL, or your own Postgres (Neon, Supabase, a server you run) |

### Why tags and not just a vector database or NotebookLM?

Vector search and managed RAG tools (NotebookLM, Gemini File Search) are good at "find
passages like this". They can't answer the counting questions: which theme × narrative
cells are empty, which themes spiked this month, who hasn't been heard from. Tags can, and
they're explainable: you can always say why an item came back. When tags stop being
enough, add an embedding column (commented out in [`tle/schema/`](tle/schema)).

## Deploy

```bash
PROJECT=your-gcp-project DASHBOARD_ACCESS=domain:yourcompany.com ./deploy/deploy.sh
```

The script enables the APIs and creates a service account. Pass any keys as environment
variables; it stores them in Secret Manager, generates `TLE_API_TOKEN` if there isn't one,
and, if there's no model key at all, asks which AI to use (Claude, OpenAI or Gemini) and for its
key. A new installation starts on that provider, with audio on if it's OpenAI or Gemini. It creates a small Cloud SQL
Postgres instance (`db-f1-micro`, about the cheapest there is; set `DB_TIER` for a bigger
one, or pass `DATABASE_URL` to use your own Postgres) and deploys the service, which creates
its tables when it starts. It schedules the email (every 5 minutes), Drive (hourly) and
ClickUp (every 15 minutes) jobs, which do nothing until you set those up on Sources. It
deploys the dashboard as a second service behind IAP, open to `DASHBOARD_ACCESS` (`domain:`,
`group:` or `user:`, comma-separated). The main service stays open for MCP clients, ChatGPT and
scripts, which use the API token, and for Slack, whose messages are checked with its signing secret. Last, it runs `tle check` on Cloud Run and prints how to read the
result, then the addresses you need for the ways in below.

Run it again after changing the code, or to add a key: export it first (e.g. with
`read -s SLACK_BOT_TOKEN && export SLACK_BOT_TOKEN`, so it isn't left in your shell history),
then rerun. Keys already in Secret Manager are kept.

## Ways in

| Way in | Set up |
| --- | --- |
| **Web form** | Open `/ingest` on the dashboard or the service. Paste text, upload a file or share a link, with an optional note ("Jane's take on pricing") and author. **Preview** shows how it's been read: the author (worked out from the content if you left it blank), date, source, visibility, label, live citation, summary, themes, narratives and full text, all editable. **Save** stores exactly what's on screen. On the service itself (outside the dashboard), enter the API token once per browser. |
| **Slack bot** | Create an app from [`deploy/slack-manifest.yaml`](deploy/slack-manifest.yaml) with the deploy's Slack events address, pass its bot token and signing secret to the deploy (`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`), and then pick each channel under Slack on Sources. Every public channel is listed, and the bot joins the ones you pick when you save; private channels are listed once someone invites the bot (`/invite`). Speakers are matched to People by their Slack ID, or by the email on their Slack profile, so Slack IDs needn't be typed in: saving a person with an email fills it in. If the app was created before `channels:join` or `users:read.email` was in the manifest, add those scopes under OAuth & Permissions and reinstall it. Each channel has its own rules. In `reaction` mode (default), a message is saved when someone reacts with the channel's emoji (📍 `:round_pushpin:` unless you pick another). In `all` mode, every message in the channel is saved. With `include_threads` (default), a thread is saved as one item, the parent plus every reply with each speaker named, and it's rebuilt and re-tagged as the thread grows. With `include_files` (default), files shared in those messages (documents, PDFs, images, voice clips) are saved as items of their own, with the message as a note. With `include_links` (default), linked pages are read; see [Shared links](#shared-links) for what's kept. LinkedIn often hides posts from logged-out visitors, so those may fail and show as a problem on the dashboard. |
| **Email** | A dedicated inbox (e.g. `ideas@yourcompany.com`) that people send or forward things to. It's a Google Workspace mailbox, read every 5 minutes by a scheduled job; see [Email inbox](#email-inbox) below. Authors come from fields at the top of the body (`Author:`, `Date:`, `Type:`…) or, without them, are worked out by the LLM, since the sender may be sending in someone else's work. Each attachment becomes its own item, and a subject like `meeting: Q3 all-hands` sets the source type. Mail from senders who aren't allowed is filed as rejected. |
| **Drive** | Share each folder with the service account, then pick it under Google Drive on Sources, with the kinds of content it holds (e.g. your Meet transcripts folder as `meeting`), an optional visibility, and whether to include subfolders. Sources lists every folder shared with the service account. Authors are worked out the same way as for email (fields at the top of the file, else the LLM, else the file owner flagged for review), because the owner of a Meet transcript is just whoever organised the meeting. Read hourly: new files are saved once, and a file edited since it was saved is read and tagged again once it's been left alone for half an hour, keeping its status, visibility, deliverables and authors. |
| **ClickUp** | Pass a personal API token from a dedicated ClickUp user to the deploy (`CLICKUP_API_TOKEN`), then pick spaces, folders or lists and Chat channels under ClickUp on Sources, each with its own rules. Read every 15 minutes. Tasks with the place's tag (`ingest` unless changed) are saved with their comment thread, and rebuilt as comments are added; Docs in the place can be saved too, and are updated as they change. Chat is saved as sessions or by reaction: see [ClickUp Chat](#clickup-chat). Attachments become items of their own. |
| **Links** | Share a URL in the web form ("Share a link"), in an email, or with `tle ingest --url`. The page is fetched and its main content pulled out, dropping menus and footers, and pages that build their content with JavaScript are rendered in a headless browser first. Links to PDFs and other files are read like uploads. The page's byline helps work out the author. Shared links default to source type `web` (public), or `linkedin` for linkedin.com. Addresses on private or internal networks are refused. |
| **Claude / ChatGPT** | Both can save items when you ask ("save this as a braindump from Jane"). |
| **API / scripts** | `POST /ingest` with JSON, or `POST /ingest/file` with a file. Send `Authorization: Bearer $TLE_API_TOKEN`. Docs at `/docs`. |

Email, Drive and ClickUp are read on a schedule. Sources shows when each was last read and what
it found, and **Read now** reads one straight away (`tle sync-email`, `tle sync-drive` and
`tle sync-clickup` do the same from a terminal). After each run, new items are linked to related
ones from other sources (see [Related items](#related-items)).

### ClickUp Chat

Each channel is saved in one of two ways:

- **As sessions** (every message). The channel is split wherever it went quiet for 4 hours (or
  the channel's own gap), and each session is saved as one item once it's ended: its messages
  numbered, with thread replies under them. A late reply rebuilds its session.
- **Reacted messages.** A message someone reacts to with the channel's emoji is saved with its
  replies, as in Slack.

Nothing posted before a channel's *Starting from* date is saved (it defaults to the day the
channel is added); an earlier date reads its history, a batch of sessions per run. Changing a
channel's mode or date later never re-reads anything. To start over, remove the channel and add
it again. Each channel also says how many days to keep watching a message for new reactions and
replies (7 unless changed).

**Topics.** Once a session is saved, the quick model (under Models in Settings; Claude Haiku
unless changed) splits it into topics, each a piece of work or a question with the messages it
covers, and links a topic to the channel's earlier one when it's the same work picked up again.
Linked topics form a chain: a session's page shows what each topic continues and where it's
continued, and `get_item` and the `get_chain` MCP tool return them. Anyone can correct a topic
from the session's page (rename it, move a message to another topic, add or remove a link);
once corrected, the model leaves that session's topics alone.

**Views** (optional, per channel). The main model lists each person's positions in every topic,
credited to them and pointing at their messages. They're a summary, so clients are told to
quote the messages, not the view.

Docs shared in a channel are read as described in [Shared links](#shared-links). A link is read
when its session is saved, so one posted in a busy channel appears once the channel has been
quiet for the gap. ClickUp marks its Chat API as experimental, so Chat may need fixes if ClickUp
changes it.

### Meeting notes and transcripts

A transcript records what people said, so a view in it can be quoted and credited. Notes from
Gemini, Granola or a notetaker are an AI's or a person's summary: "Jane argued for value
pricing" is the notetaker's sentence, not Jane's. So each meeting records which it is, from the
text's shape (a speaker on most lines, or headings like "Summary" and "Next steps") or, when
that isn't clear, the author check:

- **Transcript**: quoted and credited as usual.
- **Notes**: kept, but cited as notes (`… - MEETING NOTES … - 06.11.2026`) and flagged
  `paraphrased` in every result, and clients are told to report them, never quote them.
- **Both in one doc**, as Meet makes them: the transcript is the item's text, and the notes are
  kept beside it.

When the same meeting comes in twice (the transcript from Drive, the notes by email), the two
are linked and the notes are kept on the transcript too.

### Email inbox

Send or forward anything to the inbox: text in the body, attachments, or links. A short
body (up to 500 characters, not counting links) alongside attachments or links is a **note
about them**, e.g. "Jane's take on pricing, the second half is the good bit". It guides the
tagging and author detection rather than being saved on its own. A longer body is saved as
content itself, with its links left inline. Each attachment becomes its own item. Links follow
[Shared links](#shared-links), except that a link sent with no comment is a deliberate save,
like the web form's: the page is saved, credited to whoever wrote it.

### Shared links

The database holds your company's own thinking, so when someone shares a link in Slack, ClickUp
or the inbox, what they say about it is what's kept:

- **A link with commentary** (a message, a thread, a task's description and comments): the
  commentary is the item, credited to the people who wrote it. The linked page is kept on it as
  context (title, site, author and an excerpt), shown under "Responding to" on the item and read
  by the tagger, which tags and summarises their response rather than the page.
- **A page written by someone on People** (a colleague's own LinkedIn post) is their thought
  leadership, so it's also saved as an item of its own, credited to them.
- **A link shared with no commentary** (just the link, or "worth a read") is saved as
  **reading**: someone else's content, marked as not your company's view. It's listed under
  Reading on Items and left out of searches, citations, coverage and People unless asked for
  (`include_reading` on the API and MCP search).
- **Your own docs** (Google Docs and Drive files, Granola notes) are never reading. Each is
  stored once, as a meeting or a document of its own (a Drive file already saved from a folder
  is reused), and linked from the item that shared it. Drive files are read as the service
  account, so share them (or their folder) with it; one it can't open stays a bare link.

A page is only credited to someone on People when the page itself shows them (its byline, title
or opening, or their profile in its address), never just because they shared it. Fewer than four
words beside the links don't count as commentary. An item can be switched between your own
content and context ("someone else's content") on its page.

### Related items

After each scheduled run, every new item is compared with items from within three weeks that
share a person and share themes or words, and the quick model links the ones about the same
piece of work, client, project or decision. Related items show on each other's page, with the
reason, and in `get_item`.

Everything sent to the inbox is ingested, from anyone, so anybody can contribute. Turn on
`sources.email.only_people` ("Only allow registered people" on Sources) to accept mail only
from people listed under People (matched on email); everything else is filed as rejected.
Processed mail is labelled or moved, so nothing is saved twice and the inbox stays readable.

**Who's the author?** The sender isn't assumed to be, because people send in colleagues'
notes, interview transcripts and forwarded threads. Put fields at the top of the email body
to say so directly:

```
Author: Jane Smith, Mo Jones
Date: 2026-09-12
Type: interview
Visibility: public
Label: Podcast with Acme

(the content, or a note about the attachment)
```

Without an `Author:` line, the LLM reads the content (and the email around an attachment)
and works out whose words they are: the original writer of a forwarded email, the people
giving their views in a transcript, the person named in "Jane's take on pricing", or the
sender if it's their own writing. It also picks up the date if the content shows one.
If it can't tell, the sender is used and the item gets `author_needs_review` in its
metadata, as do items whose detected author isn't in `people`. Those show up in Items, under Needs review.
Every email item records `submitted_by` and `author_source` (`provided`, `llm` or `submitter`).

Drive files and Slack files and links work the same way, with the file owner, uploader or
sharer in place of the sender. Whatever the way in, the author is always worked out from the
content first, and every fallback is flagged for review.

**Setting it up.** No passwords: the service account reads the mailbox with domain-wide
delegation.

1. Create the mailbox in Google Workspace, e.g. `ideas@yourcompany.com`.
2. Deploy. The script prints the service account's client ID.
3. Put the inbox address under Email on Sources.
4. A Workspace admin goes to **Admin console > Security > Access and data control > API controls >
   Domain-wide delegation**, adds that client ID, and grants the scope
   `https://www.googleapis.com/auth/gmail.modify`. This lets the service account act only as
   mailboxes it names, and the job only ever names the inbox set on Sources.
5. Saved mail gets the label `tle-saved` and rejected mail `tle-rejected`.

The inbox has to be in Google Workspace. Mail from elsewhere can be forwarded to it.

Files can be text, Markdown, PDF, DOCX, VTT/SRT captions, images (described and
transcribed by the LLM) or audio (transcribed). Files are read locally where possible, which
is free and word for word. The model only reads what can't be read that way: images, and
scanned PDFs or slides saved as pictures (PDFs with almost no text layer are rendered page by
page and transcribed; see Files on Sources).

## Ways out

### Claude (Desktop, Code) and other MCP clients

The service has an MCP server at `/mcp`. Clients connect with the address and the API token,
with nothing to install and no database access on anyone's computer. The deploy prints the
address; the token is in Secret Manager:

```bash
gcloud secrets versions access latest --secret tle-tle-api-token
```

**Claude Code:**

```bash
claude mcp add --transport http thought-leadership https://YOUR-SERVICE-URL/mcp --header "Authorization: Bearer YOUR-TOKEN"
```

**Claude Desktop, Cursor and other clients** that take a local command: use
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) to reach the address:

```json
{
  "mcpServers": {
    "thought-leadership": {
      "command": "npx",
      "args": ["mcp-remote", "https://YOUR-SERVICE-URL/mcp", "--header", "Authorization: Bearer YOUR-TOKEN"]
    }
  }
}
```

claude.ai's web connectors expect a sign-in flow (OAuth) rather than a token, so they can't
connect yet.

Then install the skill in [`skills/claude/thought-leadership`](skills/claude/thought-leadership):
upload the folder as a skill in Claude, or copy it to `~/.claude/skills/` for Claude Code.

### ChatGPT

A custom GPT with an Action that calls the deployed API. See [`skills/chatgpt`](skills/chatgpt).

### The playbooks

All clients share one set of instructions in [`tle/playbooks.md`](tle/playbooks.md). Edit
it to change how every client works.

| Playbook | Ask |
| --- | --- |
| query | "What's our stance on X?" / "I'm writing about X." |
| coverage | "What should we make next?" Gaps across theme × narrative × format, as a content calendar |
| updates | "Is this article out of date?" against newer insights |
| trends | "What are people talking about this month?" |
| atomise | "Turn this transcript into LinkedIn posts." |
| aggregate | "What's the full story on X since June?" |
| case-studies | "Turn our calls with Client into a case study." |
| mining | "What stories are in our own data?" |
| debate | "Where does the team disagree?" |
| foresight | "When did we call something early?" |
| post-mortems | "Show how we learn." |
| briefs | "Write a content brief for X." |
| media | "Podcast, video and infographic ideas." |

## The data

One row per captured item ([schemas](tle/schema)):

| Column | |
| --- | --- |
| `row_id` | Unique ID |
| `date_created` | When it was said or written |
| `date_ingested` | When it was captured |
| `source_type` | The kind of content ("Type" on the dashboard): slack, clickup, meeting, document, voicenote, braindump, interview, linkedin, marketing, web, email, other. Anything else is saved as `other`, with what was given kept in `source_metadata.source_type_given` |
| `via` | The way in ("Source" on the dashboard): slack, clickup, email, drive, web, api, mcp, cli |
| `reading` | Someone else's content shared without comment; see [Shared links](#shared-links) |
| `source_metadata` | Channel, file name, URL, label for the citation, suggested new themes, and `links`: the pages an item responds to |
| `authors` | Who said it |
| `author_ids` | Their IDs in the people table, when they're in it |
| `drive_file_id` | The original file, if it's in Drive |
| `extracted_text` | The full text, transcript or image description |
| `theme_tags`, `narrative_tags` | From your Taxonomy |
| `summary` | Written by the LLM, keeping the speaker's position |
| `deliverables` | Content made from it, as `Format: title or URL` |
| `status` | new, reviewed, used, archived |
| `visibility` | public or private |
| `flags` | What needs review: `tagging_failed`, `author_unclear`, `unknown_source`, `theme_ideas` |

The tagger also suggests new themes when it sees a recurring topic you haven't listed.
They're kept in `source_metadata.suggested_themes` and shown on the dashboard's Needs
review page, where the good ones can be added with one click.

Beside the items: `topics` (a chat session's topics), `links` (topic chains and related items),
`views` (people's positions in a topic), `events` (what happened), `people`, `settings` (every
saved version) and `sync_state` (where each scheduled read got to).

## Development

Locally it runs on SQLite, with a stand-in for Google sign-in. Settings still live in the
database, and keys come from environment variables.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium     # for reading pages that need JavaScript
pytest                          # never calls real services

export TLE_SQLITE_PATH=data/dev.db TLE_DEV_USER=you@example.com TLE_API_TOKEN=dev ANTHROPIC_API_KEY=sk-ant-...
tle serve                       # http://127.0.0.1:8080/dashboard
tle check                       # what works and what to fix
```

`TLE_DEV_USER` signs you in as that address, and is ignored on Cloud Run. To keep these in a
file instead, put `KEY=value` lines in a git-ignored file and pass it with `--env-file`, e.g.
`tle --env-file .env serve`. Only for development: deployed, keys are Secret Manager secrets.

## License

MIT
