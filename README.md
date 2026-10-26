# AI_OIL

An AI-assisted open issue list: a deliberately simplified Jira in which every issue carries a dictionary of linked sources (Outlook conversations, Jira tickets, Confluence pages, SharePoint files, manual notes). The system pulls the latest state of every source into the issue, with versions, so that one place holds everything that is known about a topic. On that base, an AI summarises, extracts actions and open questions, proposes links and new issues, and reminds you who is waiting for your reply.

Single user, local on a Windows laptop, Python, SQLite.

## Status

Specification phase. Nothing is built yet.

| Document | Content |
|---|---|
| [`docs/specification.md`](docs/specification.md) | The specification (v0.1, draft for review). Start with chapter 5, the data model |
| [`schema/schema_v1.sql`](schema/schema_v1.sql) | The data model as an executable SQLite schema |

## Phases

| Phase | Content |
|---|---|
| P1 | Data foundation: schema, connectors (Manual → Outlook → Jira → Confluence → SharePoint), on-demand sync, search, CLI. No LLM |
| P2 | Jira-like local web interface |
| P3 | AI enrichment: summaries, extraction, link suggestions, import inbox, proposals to approve |
| P4 | AI assistant: My Inbox, daily briefing, "waiting on me" |
| P5 | Write-back with per-action approval, dashboards, Teams connector |
