# Expertise Database (thought-leadership-engine)

- **Setting it up for a company?** Follow [docs/SETUP_WITH_AN_AGENT.md](docs/SETUP_WITH_AN_AGENT.md):
  what to ask first, which steps only the person can do, and how to confirm each one.
- It runs on Google Cloud only. There are no config files: settings live in the database and
  are edited in the dashboard, and keys are Secret Manager secrets (environment variables
  when developing). See the README's Development section for running it locally.
- **`tle check`** says whether the database, keys and each source work, and what to fix.
- Never print secret values or put them on a command line, and never commit database files.
- Tests: `.venv/bin/python -m pytest -q`. They never call real services (see `tests/conftest.py`).
