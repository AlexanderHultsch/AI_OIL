# AI_OIL

An AI-assisted open issue list: a deliberately simplified Jira in which every issue carries a dictionary of linked sources (Outlook conversations, Jira tickets, Confluence pages, SharePoint files, manual notes). The system pulls the latest state of every source into the issue, with versions, so that one place holds everything that is known about a topic. On that base, an AI summarises, extracts actions and open questions, proposes links and new issues, and reminds you who is waiting for your reply.

Single user, local on a Windows laptop, Python, SQLite.

## Status

**Phases 1 and 2 are implemented**: data model, Manual / Outlook / Jira / Confluence / SharePoint connectors, on-demand sync with versioning, full-text search and the OQL query language, CLI, backup — and a Jira-like local web interface (`oil serve`, then http://127.0.0.1:8765/). Phases 3–5 (AI) are not started.

Quick start (Windows, Python 3.12, classic Outlook):

```
python -m venv .venv && .venv\Scripts\pip install -e .[dev]
oil init --name "Your Name" --email you@example.com
oil project add OIL "Open issues"
oil connector add outlook Outlook
oil connector add jira Jira --setting base_url=https://acme.atlassian.net --setting email=you@example.com --token
oil connector check --live
oil issue create -p OIL -s "Clarify inductor lead time" -d "Supplier quoted 26 weeks"
oil link OIL-1 --outlook-selected
oil sync --all
oil search 'text ~ "lead time" AND statusCategory != Done'
oil serve
```

| Document | Content |
|---|---|
| [`docs/specification.md`](docs/specification.md) | The specification (v0.3). Chapter 9 lists every CLI command |
| [`src/ai_oil/db/sql/0001_baseline.sql`](src/ai_oil/db/sql/0001_baseline.sql) | The data model as an executable SQLite schema |
| [`docs/implementation_plan.md`](docs/implementation_plan.md) | Conventions, work order and status |
| [`docs/open_points.md`](docs/open_points.md) | Every open point, limitation and decision, for all phases |

## Phases

| Phase | Content |
|---|---|
| P1 | Data foundation: schema, connectors (Manual → Outlook → Jira → Confluence → SharePoint), on-demand sync, search, CLI. No LLM |
| P2 | Jira-like local web interface |
| P3 | AI enrichment: summaries, extraction, link suggestions, import inbox, proposals to approve |
| P4 | AI assistant: My Inbox, daily briefing, "waiting on me" |
| P5 | Write-back with per-action approval, dashboards, Teams connector |
