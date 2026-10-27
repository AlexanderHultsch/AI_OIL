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

**Phase 1 is complete.** 1,759 tests pass, lint and format are clean, and the phase-1 end-to-end test (`tests/e2e/test_p1.py`) covers the exit criteria with fixture-backed connectors. The real acceptance run on the target laptop (`oil connector check --live`, then a first real `oil sync`) is still open.

Steps 5, 8, 13, 17 and 18 were split into sub-steps during execution (5a–c, 8a–c, 13a–b, 17a–b, 18a–b); step 21b fixed a missing credential store found in a manual run.

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
| 22–26 | P2 web interface | per step |
| 27–31 | P3 AI enrichment | per step |
| 32–33 | P4 assistant | per step |
| 34–36 | P5 write-back and dashboards | per step |
