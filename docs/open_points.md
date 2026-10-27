# Open points

The single list of everything that is not done, not decided or known to be imperfect — for the current and all future phases. Every entry has an ID that commits and the spec can refer to. Closed points stay in the list with their resolution.

| Status | Meaning |
|---|---|
| open | Nothing done yet |
| decide | Needs a decision by Alex |
| accepted | Known limitation, deliberately left for now |
| closed | Done; resolution noted |

## 1. Acceptance on the target laptop (phase 1)

Everything here was built and tested against recorded fixtures and simulated systems. Only a run on the corporate laptop proves it.

| ID | Item | Why it matters | Action | Status |
|---|---|---|---|---|
| OP-1 | `oil connector check --live` for all four connectors | First contact with the real systems | Run it after `oil connector add …`; every line must read `[ok]` | open |
| OP-2 | Outlook `GetConversation()` across Inbox, subfolders and Sent Items in cached Exchange mode (spec OI-9) | The whole email linking depends on it | Link one real thread with `--outlook-selected`, reply to it, `oil sync`, check `oil source show ID --entries` lists both messages | open |
| OP-3 | Outlook time values from pywin32 (local wall-clock tagged as UTC) | Wrong times would mis-order messages and break "did I reply?" later | Compare `oil source show --entries` times with Outlook | open |
| OP-4 | Outlook `ReceivedTime` on Sent Items | Subject search and the mailbox scan sort by it | Same check as OP-2 for a sent message | open |
| OP-5 | Corporate SSL inspection for Jira / Confluence | Requests fail with certificate errors | If so: `oil connector set Jira --setting ca_bundle=C:/path/corp-ca.pem` | open |
| OP-6 | Atlassian API token and Windows Credential Manager | Tokens must land in Credential Manager | `oil connector add jira … --token`; `oil connector list` shows `credential: stored` | open |
| OP-7 | OneDrive Files-On-Demand attributes (offline placeholders) | Hydration is reported, not silent | Link a cloud-only file, sync, check the "hydrated" warning | open |
| OP-8 | Python 3.12, pip and PyPI access on the corporate laptop | No install, no tool | `pip install -e .[dev]`; wheels can be vendored if PyPI is blocked | open |
| OP-9 | Performance with real data (NFR-3, SRCH-9) | Measured only with synthetic data | Note the duration lines of `oil sync` and `oil search` after a few weeks | open |

## 2. Known limitations in the phase-1 code

| ID | Item | Impact | Proposed action | Status |
|---|---|---|---|---|
| OP-10 | `summary ~`, `description ~` and `comment ~` fold only ASCII letters (SQLite LIKE) | `summary ~ "müller"` does not find "MÜLLER"; `text ~` (full-text) is not affected | Register a Python `casefold` SQL function on every connection and use it in the compiler | open |
| OP-11 | An email attachment that failed to load once is not retried | Rare; the warning is shown once | Retry attachments of messages whose attachment children are missing | accepted |
| OP-12 | `html_to_text` drops indentation inside `<pre>` and list bullets / heading markers | Code blocks from Confluence lose indentation; search is unaffected | Keep leading whitespace inside `<pre>`; render `- ` and `#` | open |
| OP-13 | Every Confluence comment costs one extra request for replies | Slower sync of pages with many comments | Use `childrenCount` from the v2 API to skip | accepted |
| OP-14 | A sibling like `report-final.docx` is flagged as a OneDrive conflict copy of `report.docx` | False warning only | Match only the machine-name pattern OneDrive uses | accepted |
| OP-15 | A Jira issue moved to another project keeps its old key as display reference | The key shown in AI_OIL is outdated | Let `FetchResult` carry an updated `display_ref` | open |
| OP-16 | The Alembic stamp of the very first migration is written after the schema script commits | A crash in that instant leaves tables without a version row; `oil init` then fails | Detect "tables but no version" and stamp | accepted |
| OP-17 | A connector's `parse_reference` that raises aborts `oil link` (only construction failures are isolated) | One broken connector can block linking | Isolate `parse_reference` like construction (CON-3) | open |
| OP-18 | A sync run whose engine itself crashes stays `running` in the sync log | Cosmetic in the log | Mark stale `running` runs as `failed` on the next start | accepted |
| OP-19 | A plugin constructor raising a non-domain exception shows a traceback in `oil connector add` | Only for faulty plugins | Wrap in the CLI | accepted |
| OP-20 | `oil link` exits 0 when the initial fetch fails; the link stays | Intended, but easy to miss | Print the failure prominently (done in the report); revisit in the UI | accepted |
| OP-21 | Jira search escapes only `"` and `\` in JQL text | Unusual characters may break candidate search (P3) | Escape Lucene specials when P3 uses it | open |
| OP-22 | `update_project` cannot clear lead or description | Minor | Add explicit clearing | accepted |
| OP-23 | `oil issue show` does not display `snoozed_until` | Snooze arrives in P4 | Add the line in P4 | open |
| OP-24 | FTS snippet comes from the body; a title-only match shows no highlight | Cosmetic | Snippet over both columns | accepted |
| OP-25 | Full test suite takes about 70 s (the OQL compiler fixture rebuilds the database per test) | Slower feedback | Module-scoped fixture for the compiler tests | open |
| OP-26 | SharePoint URLs with `..` segments are normalised but not blocked | Local single-user tool | Reject `..` | accepted |
| OP-27 | No `CoUninitialize` for Outlook COM threads | None known | Watch during OP-2 | accepted |
| OP-28 | Starlette warns that `httpx` for its TestClient is deprecated in favour of `httpx2` | Test-only warning | Switch the dev dependency when httpx2 is stable | accepted |
| OP-29 | Web GET pages run inside `BEGIN IMMEDIATE` and briefly take the write lock | Harmless for one user; a long sync keeps transactions short | Read-only dependency with `engine.connect()` if contention shows | accepted |
| OP-35 | After a rejected inline edit in the issue list, the control keeps the chosen value until the page is reloaded (only the error message shows) | Misleading until reload | Re-render the row on error | accepted |
| OP-36 | Descriptions and comments are Markdown by spec (DM-19, UI-20) but are shown as plain text with line breaks | No formatting in the web UI | Add a Markdown renderer (e.g. markdown-it-py, sanitised) | open |

## 3. Decisions needed

| ID | Question | Proposal | Status |
|---|---|---|---|
| OP-30 | May archived projects still receive new issues and labels? | No: creating an issue in an archived project is rejected; existing issues stay editable | decide |
| OP-31 | May merged persons still be assigned (by id)? | No: assignment to a merged person is redirected to the surviving person | decide |
| OP-32 | Custom fields per project (spec OI-6) | Not before a concrete need; then an `issue_field_value` table | decide (later) |
| OP-33 | Semantic search with embeddings (spec OI-10) | Only if keyword search proves insufficient after P4 | decide (later) |
| OP-34 | Outlook online archive, PST files and shared mailboxes (spec OI-2) | Today: Inbox + subfolders + Sent Items + extra folders. Revisit after OP-2 | decide (later) |

## 4. Future phases

| ID | Phase | Item | Status |
|---|---|---|---|
| OP-40 | P2 | Web interface (spec chapter 10) — see `docs/implementation_plan.md` steps 22–26 | in progress |
| OP-41 | P3 | OpenCode adapter contract on the target machine: does a headless `opencode run` hang without `--auto`? (as in Professional-Assistant OC-6) | open |
| OP-42 | P3 | Model per task type and attestation of the company-internal model for program content | open |
| OP-43 | P3 | Token budget per run (AI-20) and the fixed OpenCode overhead of about 8,000 input tokens per call | open |
| OP-44 | P3 | Scan window and Jira/Confluence scope for the import inbox (spec OI-4, OI-5: decided defaults, revisit with real data) | open |
| OP-45 | P4 | Daily briefing: "first open on a working day" needs a reliable definition when the UI stays open overnight | open |
| OP-46 | P4 | Waiting-on-me for unlinked mail depends on OP-2 and the mailbox scan | open |
| OP-47 | P5 | Approval tokens, write-back to Jira / Confluence / Outlook drafts (never send) | open |
| OP-48 | P5 | Dashboards as server-side SVG | open |
| OP-49 | P5 | Teams chat connector — deferred; needs the Power Automate bridge format first | open |
| OP-50 | later | Team / multi-user operation (out of scope in all phases today) | open |

## 5. Spec open items (chapter 16) — status

| Spec ID | Resolution | Status |
|---|---|---|
| OI-1 | Cancelled is a sixth status with a mandatory reason | closed |
| OI-2 | Inbox + subfolders + Sent Items + extra folders; see OP-34 | closed (revisit) |
| OI-3 | OpenCode CLI with the company-internal model | closed |
| OI-4 | Since last scan, first run 14 days; see OP-44 | closed (revisit) |
| OI-5 | Configured list per connector (`projects`, `spaces`) | closed |
| OI-6 | See OP-32 | decide (later) |
| OI-7 | Done/Cancelled issues keep syncing 14 days | closed |
| OI-8 | SharePoint: text only, no raw copy | closed |
| OI-9 | See OP-2 | open |
| OI-10 | See OP-33 | decide (later) |
