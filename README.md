# AI_OIL

An AI-assisted open issue list: a deliberately simplified Jira in which every issue carries a dictionary of linked sources (Outlook conversations, Jira tickets, Confluence pages, SharePoint files, manual notes). The system pulls the latest state of every source into the issue, with versions, so that one place holds everything that is known about a topic. On that base, an AI summarises, extracts actions and open questions, proposes links and new issues, and reminds you who is waiting for your reply.

Single user, local on a Windows laptop, Python, SQLite.

## Status

**Phases 1 and 2 are implemented**: data model, Manual / Outlook / Jira / Confluence / SharePoint connectors, on-demand sync with versioning, full-text search and the OQL query language, CLI, backup — and a Jira-like local web interface (`oil serve`, then http://127.0.0.1:8765/). Phases 3–5 (AI) are not started.

**Installation on Windows:** follow [`docs/install_windows.md`](docs/install_windows.md) — code in your (OneDrive-synced) Documents folder is fine, but the Python environment and the database stay in `%LOCALAPPDATA%\AI_OIL`, and only the daily backups go to OneDrive.

| Document | Content |
|---|---|
| [`docs/specification.md`](docs/specification.md) | The specification (v0.4). Chapter 9 lists every CLI command |
| [`src/ai_oil/db/sql/0001_baseline.sql`](src/ai_oil/db/sql/0001_baseline.sql) | The data model as an executable SQLite schema |
| [`docs/implementation_plan.md`](docs/implementation_plan.md) | Conventions, work order and status |
| [`docs/install_windows.md`](docs/install_windows.md) | Installation, first run and updates on the Windows laptop |
| [`docs/open_points.md`](docs/open_points.md) | Every open point, limitation and decision, for all phases |

## Phases

| Phase | Content |
|---|---|
| P1 | Data foundation: schema, connectors (Manual → Outlook → Jira → Confluence → SharePoint), on-demand sync, search, CLI. No LLM |
| P2 | Jira-like local web interface |
| P3 | AI enrichment: summaries, extraction, link suggestions, import inbox, proposals to approve |
| P4 | AI assistant: My Inbox, daily briefing, "waiting on me" |
| P5 | Write-back with per-action approval, dashboards, Teams connector |
