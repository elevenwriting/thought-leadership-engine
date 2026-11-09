# Setting it up for a company, with an AI agent

For an agent (Claude Code or similar) asked to "set this up for our company". The README is the
full reference; this is the order to do it in, what to ask the person, which steps only the
person can do, and how to confirm each step worked before moving on.

It runs in the company's own Google Cloud project. There are no config files: keys go into
Secret Manager through the deploy, and everything else is set in the dashboard.

**Ground rules**

- The check is how you know a step worked. The deploy runs `tle check` on Cloud Run at the
  end and prints how to read it and run it again. Don't report a step as done until its line
  is ✓ (or – for a source the company doesn't want).
- Never print, echo or paste a secret's value (API keys, tokens, passwords), and never put
  one on a command line, where it stays in shell history. The deploy asks for the Anthropic
  key itself; for others, have the person `read -s NAME && export NAME` in their own terminal.
- Steps marked **Person** need a browser, an admin console or a decision. Stop, say exactly
  what to do and where, and wait. Don't try them yourself or work around them.

## 1. Ask these first

Collect the answers before changing anything, so you don't stop halfway through:

1. **Google Cloud project**: a new one (the person creates it and links billing) or an
   existing one. Its ID, and the region (default `europe-west2`).
2. **Who may open the dashboard**: their Google Workspace domain (e.g. `acme.com`), a group,
   or named people. Sign-in is with Google accounts.
3. **Which ways in they want**: Slack, Google Drive, ClickUp, an email inbox (in Google
   Workspace, and its address), or only the web form and AI tools.
4. **Which AI**: Claude, OpenAI or Gemini, and whether they want audio (voice notes and
   recordings need an OpenAI or Gemini key; a company tagging with Claude can add one too).

The company name, people and themes are entered in the dashboard afterwards, not now.

## 2. Deploy

1. **Person:** create the project and link billing (Cloud console), then run
   `gcloud auth login` (in Cloud Shell they're already signed in).
2. **Agent:** from the repo, `PROJECT=<id> DASHBOARD_ACCESS=domain:<their domain> ./deploy/deploy.sh`.
   It asks which AI to use and for that provider's key, without showing it; let the person
   type both. The first run
   takes a while (the database takes several minutes). Keep its closing summary: the
   addresses, the service account, the check command and the notes are needed below.
3. **Agent:** read the check's output with the command the deploy printed. Database, Model
   and API token should be ✓. If the Model line says the Anthropic key "needs a workspace
   named", the person exports `ANTHROPIC_WORKSPACE_ID` (Anthropic Console > Settings >
   Workspaces) and you rerun the deploy; or they use a key made inside a workspace.

## 3. In the dashboard

**Person**, at the dashboard address the deploy printed (it opens on Settings for a new
installation): the company name and models in Settings, themes on Taxonomy, people on
People. Then the check's Config line no longer asks for them.

## 4. Connect the ways in

Do the ones the company chose. After each, rerun the check (the command the deploy printed),
or open Sources, which shows each source in red until it works.

- **Slack.** **Person:** create the app from `deploy/slack-manifest.yaml` with the request URL
  set to the deploy's "Slack events URL", install it, then in their terminal export
  `SLACK_BOT_TOKEN` (the `xoxb-` token) and `SLACK_SIGNING_SECRET` with `read -s`.
  **Agent:** rerun the deploy, which stores them. **Person:** pick channels under Slack in
  Settings; invite the bot to private ones with `/invite`.
- **Google Drive.** **Person:** share each folder with the service account the deploy printed
  (`tle-runner@<project>.iam.gserviceaccount.com`) as Viewer, then pick it under Google Drive
  on Sources. If Drive refuses the share, a Workspace admin must allow sharing outside the
  domain for that address.
- **ClickUp.** **Person:** create a dedicated ClickUp user, add it to the spaces and Chat
  channels it should read, create its personal API token (`pk_`) and export it as
  `CLICKUP_API_TOKEN`. **Agent:** rerun the deploy. **Person:** pick places and channels under
  ClickUp on Sources.
- **Email inbox.** **Person:** a Workspace admin grants the service account's client ID the
  Gmail scope (the deploy prints both and where to do it), and the inbox address goes under
  Email on Sources.

## 5. Connect Claude

**Person:** in Claude Code, `claude mcp add --transport http thought-leadership <service address>/mcp
--header "Authorization: Bearer <token>"`, with the token from
`gcloud secrets versions access latest --secret tle-tle-api-token` (they run that themselves;
don't print it). Then install the skill (see the README's Ways out). Ask Claude something
like "What are our themes?" to confirm it answers from the database.

## 6. Try it

With the person, add something through each way in they set up (the web form, a Slack
reaction, a file in a Drive folder, a tagged ClickUp task, an email) and check it appears
under Items with a sensible summary, tags and author. Then ask Claude about it. Anything
confusing in the README along the way is worth fixing there.
