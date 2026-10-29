# Open points

The single list of everything that is not done, not decided or known to be imperfect — for the current and all future phases. Every entry has an ID that commits and the spec can refer to. Closed points stay in the list with their resolution.

| Status | Meaning |
|---|---|
| open | Nothing done yet |
| decide | Needs a decision by Alex |
| decided | Decided by Alex; to be built (step named in the row) |
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
| OP-5 | Corporate SSL inspection for Jira / Confluence | Requests fail with certificate errors | Since step I1 the Windows certificate store is used automatically (`truststore`); only if that fails: `oil connector set Jira --setting ca_bundle=C:/path/corp-ca.pem` | open |
| OP-6 | Atlassian API token and Windows Credential Manager | Tokens must land in Credential Manager | `oil connector add jira … --token`; `oil connector list` shows `credential: stored` | open |
| OP-7 | OneDrive Files-On-Demand attributes (offline placeholders) | Hydration is reported, not silent | Link a cloud-only file, sync, check the "hydrated" warning | open |
| OP-8 | Python 3.12, pip and PyPI access on the corporate laptop | No install, no tool | Run `install.bat`; if PyPI is blocked use the `wheels\` folder (install_windows.md, Offline installation) | open |
| OP-9 | Performance with real data (NFR-3, SRCH-9) | Measured only with synthetic data | Note the duration lines of `oil sync` and `oil search` after a few weeks | open |
| OP-54 | `install.bat` / `start.bat` / `update.bat` / `uninstall.bat` have only been checked statically | Batch behaviour on the real Windows shell (quoting, choice, reg query parsing, attrib +P) | First run on the laptop; send the console output if anything looks wrong | open |
| OP-56 | Teams bridge flows in Power Automate (`docs/teams_bridge_setup.md`) | Triggers, fields and "any chat" could not be checked here; nothing is captured before the flows run | Do sections 0–4 of the setup guide; send one row of each kind | open |

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
| OP-25 | Full test suite took about 120 s | Slower feedback | Template-and-copy fixtures (step 25c): about 56 s, every test isolated, no assertion changed; the SRCH-9 performance test accounts for about 20 s of the rest | closed |
| OP-26 | SharePoint URLs with `..` segments are normalised but not blocked | Local single-user tool | Reject `..` | accepted |
| OP-27 | No `CoUninitialize` for Outlook COM threads | None known | Watch during OP-2 | accepted |
| OP-28 | Starlette warns that `httpx` for its TestClient is deprecated in favour of `httpx2` | Test-only warning | Switch the dev dependency when httpx2 is stable | accepted |
| OP-29 | Web GET pages run inside `BEGIN IMMEDIATE` and briefly take the write lock | Harmless for one user; a long sync keeps transactions short | Read-only dependency with `engine.connect()` if contention shows | accepted |
| OP-35 | After a rejected inline edit in the issue list, the control keeps the chosen value until the page is reloaded (only the error message shows) | Misleading until reload | Re-render the row on error | accepted |
| OP-36 | Descriptions and comments are Markdown by spec (DM-19, UI-20) but are shown as plain text with line breaks | No formatting in the web UI | Add a Markdown renderer (e.g. markdown-it-py, sanitised) | open |
| OP-37 | Web UI error messages reuse CLI wording ("choose one with --connector", "add one with `oil connector add outlook`") | Slightly confusing in the browser | Give the link service messages without CLI syntax; let the CLI add its hints | open |
| OP-38 | Without a credential store (no keyring backend) the /connectors page shows an error page instead of the connectors with credential state "unknown" | Only on machines without a keyring; Windows has Credential Manager | Catch ConfigError per connector and show it on the card | open |
| OP-51 | `/sync?backup=…` and `?backup_error=…` render the text carried in the URL (escaped) | A crafted local link can show a misleading banner on the sync page | Pass a short-lived message id instead of the text | accepted |
| OP-52 | A note created through the issue page's "New note" form keeps the browser's CRLF line endings (editing normalises them) | A later identical save creates one extra version | Normalise line endings on creation too | open |
| OP-53 | Nothing stops `AI_OIL_HOME` (database) or the Python environment from being placed inside a OneDrive folder | Corruption risk if someone does it | Guard in `oil init` (refuses), every command (warns) and `oil setup` (step I2) | closed |

## 3. Decisions needed

| ID | Question | Proposal | Status |
|---|---|---|---|
| OP-30 | May archived projects still receive new issues and labels? | Decided 28 October 2026: archiving a project is refused while any of its issues is not Done or Cancelled; the error lists those issue keys. An archived project accepts no new issues. Its existing issues stay editable and can be reopened. To be built in step D2 | closed (built in D2) |
| OP-31 | May merged persons still be assigned (by id)? | Decided 28 October 2026: assigning (or setting reporter, or any person reference) to a merged person is redirected to the surviving person, following the merge chain. To be built in step D2 | closed (built in D2) |
| OP-32 | Custom fields per project (spec OI-6) | Decided 28 October 2026: not now; add custom fields when a concrete field is missing (then an `issue_field_value` table) | closed (revisit on need) |
| OP-33 | Semantic search with embeddings (spec OI-10) | Decided 28 October 2026: only if keyword search proves insufficient after P4 | closed (revisit after P4) |
| OP-34 | Outlook online archive, PST files and shared mailboxes (spec OI-2) | Decided 28 October 2026: keep today's scope (Inbox + subfolders + Sent Items + extra folders); decide after the Outlook test OP-2 | decide (after OP-2) |
| OP-39 | Which P1 operations stay CLI-only? Today: `oil init`, `oil connector add/set` (token entry), `oil restore` | Decided 28 October 2026: `oil init`, `oil connector add/set` and `oil restore` stay CLI-only; `install.bat` / `oil setup` cover setup and token entry. Everything else is in the browser | closed |

## 4. Future phases

| ID | Phase | Item | Status |
|---|---|---|---|
| OP-40 | P2 | Web interface (spec chapter 10) — steps 22–26c | closed (27 October 2026) |
| OP-41 | P3 | OpenCode adapter contract on the target machine: does a headless `opencode run` hang without `--auto`? (as in Professional-Assistant OC-6) | open |
| OP-42 | P3 | Model per task type and attestation of the company-internal model for program content. Decided 28 October 2026: one company model is approved for program content, for every task; per-task models remain possible through configuration. Still open: the model name (comes from the OP-41 test) | decided |
| OP-43 | P3 | Token budget per run (AI-20) and the fixed OpenCode overhead of about 8,000 input tokens per call. Decided 28 October 2026: default budget 50 model calls per run; before exceeding it, the run asks the user to continue (a confirmation in the browser, a prompt on the CLI); without consent it stops and reports what is left | decided |
| OP-44 | P3 | Scan window and Jira/Confluence scope for the import inbox (spec OI-4, OI-5). Decided 28 October 2026: keep the defaults — since the last scan; the first scan goes 14 days back | decided |
| OP-45 | P4 | Daily briefing: "first open on a working day" needs a reliable definition when the UI stays open overnight. Decided 28 October 2026: the briefing is generated only on demand (a "Briefing" button in the browser, a CLI command), never automatically when the app opens | decided |
| OP-46 | P4 | Waiting-on-me for unlinked mail depends on OP-2 and the mailbox scan. Decided 28 October 2026: "Waiting on me" includes unlinked emails, once OP-2 is confirmed | decided |
| OP-47 | P5 | Approval tokens, write-back to Jira / Confluence / Outlook drafts (never send). Decided 28 October 2026: no write-back in any phase; AI_OIL only reads external systems. P5 becomes "Dashboards" only. Approval of AI proposals inside AI_OIL (P3) is unaffected | decided |
| OP-48 | P5 | Dashboards as server-side SVG. Decided 28 October 2026: dashboards are decided in P5 | open (decided in P5) |
| OP-49 | before P3 | Teams chat connector. Decided 28 October 2026: moves forward, built next before P3 as the sixth connector, through a new Power Automate flow that writes into an Excel table in a synced SharePoint/OneDrive library. Concept agreed 29 October 2026 (`docs/teams_bridge_concept.md`): all @-mentions and all chats captured into `Documents\AI_OIL-Teams` in OneDrive; whole conversations are linked; flows set up with `docs/teams_bridge_setup.md` | decided (step T2) |
| OP-55 | P3 | Sync and scan are separate. Decided 29 October 2026: sync only updates linked sources; "Scan for new topics" is its own action over the whole mailbox, the unlinked Teams chats and Jira. Each proposal carries its source: an accepted new-issue proposal creates the issue with the email or chat linked; a link proposal links the conversation to an existing issue (AI-12, AI-19) | decided (P3) |
| OP-50 | later | Team / multi-user operation (out of scope in all phases today). Decided 28 October 2026: maybe later; keep in mind | open |

## 5. Spec open items (chapter 16) — status

| Spec ID | Resolution | Status |
|---|---|---|
| OI-1 | Cancelled is a sixth status with a mandatory reason | closed |
| OI-2 | Inbox + subfolders + Sent Items + extra folders; see OP-34 | closed (revisit) |
| OI-3 | OpenCode CLI with the company-internal model | closed |
| OI-4 | Since last scan, first run 14 days; see OP-44 | closed (revisit) |
| OI-5 | Configured list per connector (`projects`, `spaces`) | closed |
| OI-6 | See OP-32 | closed (revisit on need) |
| OI-7 | Done/Cancelled issues keep syncing 14 days | closed |
| OI-8 | SharePoint: text only, no raw copy | closed |
| OI-9 | See OP-2 | open |
| OI-10 | See OP-33 | closed (revisit after P4) |
