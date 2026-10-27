# Implementation plan

Work order for building AI_OIL against `docs/specification.md` v0.2. Each step is verified on its own before the next starts.

## Conventions (binding for every step)

| Topic | Rule |
|---|---|
| Python | 3.12, virtual environment at `.venv`, package in `src/ai_oil`, tests in `tests/` mirroring the package |
| Commands | Verification uses `.venv/bin/pytest -q <path>` and `.venv/bin/ruff check` |
| Database access | SQLAlchemy 2 Core. Service functions take `conn: sqlalchemy.Connection` as their first argument and use `sqlalchemy.text()` with named parameters. The caller owns the transaction (`with engine.begin() as conn:`); services never commit |
| Engine | `ai_oil.db.engine.create_db_engine(path)` enables `PRAGMA foreign_keys=ON` and WAL on every connection |
| Migrations | Alembic. Revision 0001 executes `src/ai_oil/db/sql/0001_baseline.sql` |
| Time | `ai_oil.timeutil.utcnow_iso()` returns `YYYY-MM-DDTHH:MM:SS.fffZ` (UTC), matching the schema defaults. Tests freeze time by monkeypatching `ai_oil.timeutil._now` |
| Errors | Domain errors subclass `ai_oil.errors.AiOilError`. Unreachable sources are reported in results, never swallowed (AP-10) |
| Requirement IDs | Referenced in docstrings of the function that implements them, e.g. `(DM-12)` |
| CLI | Standard-library `argparse`, entry point `oil = ai_oil.cli.main:main`. Each command group is a module `ai_oil/cli/<group>.py` exposing `register(subparsers)` |
| System access | Outlook COM, HTTP and the file system sit behind injectable interfaces; tests use fixtures and never touch the network (CON-6) |
| Secrets | `keyring` only; tests use an in-memory keyring backend |
| Lint | ruff, line length 110, rules E, F, I, UP, B |

## Status (27 October 2026)

**Phases 1 and 2 are complete.** Open points of all phases are collected in `docs/open_points.md`.

**Phase 2:** the local web interface (`oil serve`) covers every P1 operation except the deliberately CLI-only `oil init`, `oil connector add/set` and `oil restore` (OP-39). 2,652 tests pass in about 80 s (step 25c made the suite twice as fast); a real-Chromium test (`pytest -m browser`, needs the `[browser]` extra) repeats the P1 acceptance through the browser. NFR-3 measured: issue list with 500 rows about 110 ms, busy issue page about 10 ms (limit 300 ms). Steps 23, 24, 25 and 26 were split into sub-steps (23a–b, 24a–b, 25a–c, 26a1, 26a2, 26b, 26c).

**Phase 1:** 1,759 tests pass, lint and format are clean, and the phase-1 end-to-end test (`tests/e2e/test_p1.py`) covers the exit criteria with fixture-backed connectors. The real acceptance run on the target laptop (`oil connector check --live`, then a first real `oil sync`) is still open.

Steps 5, 8, 13, 17 and 18 were split into sub-steps during execution (5a–c, 8a–c, 13a–b, 17a–b, 18a–b); step 21b fixed a missing credential store found in a manual run.

## Web conventions (phase 2, binding)

| Topic | Rule |
|---|---|
| Stack | FastAPI + Jinja2 templates + htmx, served by uvicorn from the same Python process (UI-1). No npm, no build step, no CDN at runtime: htmx is a pinned file in `src/ai_oil/web/static/` |
| Binding | `oil serve [--port N] [--no-browser]` listens on 127.0.0.1 only (UI-2); default port 8765, `--port 0` picks a free one |
| Host check | Requests whose Host header is not `127.0.0.1:<port>` or `localhost:<port>` are rejected (DNS-rebinding protection) |
| CSRF | One random token per server process; every POST/PUT/PATCH/DELETE must carry it as header `X-CSRF-Token` (htmx via `hx-headers` on `<body>`) or form field `csrf_token`; otherwise 403 (UI-3) |
| Layout | `src/ai_oil/web/`: `app.py` (factory `create_app(config, engine)`), `deps.py`, `csrf.py`, `routes/<area>.py`, `templates/`, `static/` (`htmx.min.js`, `app.css`, `app.js`) |
| Writes | Through the same domain services as the CLI, one transaction per request, one audit entry per change with the CLI's action names |
| Fragments | Full pages for GET; htmx requests (`HX-Request` header) get fragments; errors render inline with the domain error message |
| Tests | FastAPI `TestClient` (httpx) against a temporary data folder; one browser test with the pre-installed Playwright Chromium |
| Design | Jira-like and plain: left navigation, top search bar, tables, a right-hand side panel on the issue page; own small CSS, system font, light theme |

## Steps

| # | Step | Verification |
|---|---|---|
| 0 | Spec v0.2 with the decisions of 27 October 2026 | review |
| 1 | Project setup | `pip install -e .[dev] && oil --version && pytest && ruff check` |
| 2 | Schema in the package (Cancelled change), engine, Alembic baseline, constraint tests | `pytest tests/db` |
| 3 | Configuration, data folder, secrets, working days, `oil init` | `pytest tests/test_config.py tests/test_workdays.py tests/cli/test_init.py` |
| 4 | People, projects, components, labels | `pytest tests/domain/test_people.py tests/domain/test_projects.py` |
| 5 | Issues: keys, history, workflow, hierarchy, issue links, comments | `pytest tests/domain` |
| 6 | Governance: audit log, action classes | `pytest tests/governance` |
| 7 | CLI: project, issue, comment, issue-link, person | `pytest tests/cli` |
| 8 | Source model, connector registry, link service, source store | `pytest tests/sources` |
| 9 | Text extraction, blob store | `pytest tests/sources/test_extract.py` |
| 10 | Sync engine | `pytest tests/sources/test_sync.py` |
| 11 | Full-text index | `pytest tests/search/test_fts.py` |
| 12 | OQL parser | `pytest tests/search/test_oql_parser.py` |
| 13 | OQL compiler, `oil search` | `pytest tests/search` |
| 14 | Manual connector, `oil link/unlink/sync` | `pytest tests/connectors/test_manual.py tests/cli/test_link_sync.py` |
| 15 | `oil source show --diff`, `oil backup/restore` | `pytest tests/cli/test_source_backup.py` |
| 16 | Email text normalisation | `pytest tests/connectors/test_email_text.py` |
| 17 | Outlook connector | `pytest tests/connectors/test_outlook.py` |
| 18 | Jira connector | `pytest tests/connectors/test_jira.py` |
| 19 | Confluence connector | `pytest tests/connectors/test_confluence.py` |
| 20 | SharePoint connector | `pytest tests/connectors/test_sharepoint.py` |
| 21 | `oil connector add/list/check [--live]`, P1 end-to-end test | `pytest tests/e2e/test_p1.py && pytest && ruff check` |
| 22 | Web foundation: dependencies, vendored htmx, `oil serve`, app factory, loopback + Host check, CSRF, base layout, static assets | `pytest tests/web/test_app.py` |
| 23a | Saved filters service with built-ins (SRCH-8); issue navigator read view (OQL, columns, overdue and fetch-error markers, OQL error with caret) | `pytest tests/search/test_saved_filters.py tests/web/test_navigator.py` |
| 23b | Navigator interaction: quick-filter chips, sort by header, inline edit of status / priority / assignee / due, save filter | `pytest tests/web/test_navigator.py tests/web/test_navigator_edit.py` |
| 24a | Issue detail page: main column, side panel, status transitions with reason, in-place field edits | `pytest tests/web/test_issue_detail.py` |
| 24b | Issue detail: comments, issue links, labels, components, history tab | `pytest tests/web/test_issue_detail.py tests/web/test_issue_detail_edit.py` |
| 25a | Sources panel with refresh, link dialog with preview (reference, selected Outlook email, subject search, new note), unlink | `pytest tests/web/test_sources_ui.py` |
| 25b | Activity timeline (comments, history, source entries, new versions), expandable items, version diff view | `pytest tests/web/test_activity.py` |
| 26a | Create dialog, connectors page (incl. live check), sync log, people (merge), projects and components, keyboard shortcuts, NFR-3 timing test | `pytest tests/web` |
| 26b | Browser end-to-end test in Chromium (P2 exit criteria: P1 acceptance repeated without the CLI) | `pytest tests/e2e/test_p2_browser.py && pytest && ruff check` |
| 26c | Note editing, per-connector sync, backup now, automatic backup after web syncs | `pytest tests/web && pytest -m browser tests/e2e/test_p2_browser.py && pytest` |
| I1 | `truststore`: AI_OIL trusts the Windows certificate store (config switch, `ca_bundle` still overrides) | `pytest tests/test_truststore.py tests/connectors/test_atlassian.py` |
| I2 | `oil setup` framework (system probe, stages, `--only`, validation, idempotence) + stages data folder (OneDrive guard, OP-53), me, backup — done | `pytest tests/setup` |
| I3 | Stages Outlook (classic/new detection, COM) and network (proxy, TLS probe) — done | `pytest tests/setup` |
| I4 | Stages Jira/Confluence (URL normalisation, token, live lists) and SharePoint (OneDrive registry libraries) — done | `pytest tests/setup` |
| I5 | Finish stage (live check, first project, summary), re-run behaviour | `pytest tests/setup && pytest` |
| I6 | `install.bat`, `start.bat`, `update.bat`, `uninstall.bat` + static checks; install guide rewritten around them | `pytest tests/test_batch_files.py && pytest && ruff check` |
| 27–31 | P3 AI enrichment | per step |
| 32–33 | P4 assistant | per step |
| 34–36 | P5 write-back and dashboards | per step |
