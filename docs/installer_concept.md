# Installer and setup wizard — concept

**Status:** agreed 27 October 2026, to be built as steps I1–I6 of `docs/implementation_plan.md`.
**Replaces:** the manual steps of `docs/install_windows.md` (which stays as the reference and troubleshooting guide).

## 1. Goal

A user with a corporate Windows laptop double-clicks `install.bat` in the cloned (or unzipped) AI_OIL folder and ends with a working, configured installation and a live check of every connector — without knowing what a virtual environment, a sync root, a Jira project key or a certificate chain is.

**The machine proposes, the user confirms** (principle taken from the Professional-Assistant setup wizard, decision 0003 there).

## 2. Decisions (27 October 2026)

| # | Topic | Decision |
|---|---|---|
| 1 | Language | English only (no German console) |
| 2 | Corporate SSL inspection | `truststore`: AI_OIL uses the Windows certificate store live; no exported PEM files. An explicit `ca_bundle` setting still overrides |
| 3 | Python 3.12 or Git missing | Show the download link and stop; no automatic installation |
| 4 | Extras | None: no desktop or start-menu shortcuts, no autostart. `start.bat` is double-clicked in the folder |

## 3. Architecture

Batch only bootstraps until Python exists; everything interactive is Python (`oil setup`), because batch cannot parse JSON, read the registry cleanly, call web APIs or quote reliably.

| File | Does |
|---|---|
| `install.bat` | Check Python 3.12 (via the `py` launcher; detects the Microsoft Store stub) → create `%LOCALAPPDATA%\AI_OIL\venv` → `pip install` AI_OIL (with proxy, truststore and offline-wheel fallbacks) → run `oil setup` |
| `oil setup [--only STAGE]` | The interactive wizard (section 5) |
| `start.bat` | Start `oil serve` (browser opens); if port 8765 is busy, a free port |
| `update.bat` | `git pull` (refuses cleanly with local changes), reinstall, report |
| `uninstall.bat` | Remove the environment; data folder and backups only after an explicit, separate confirmation |

## 4. Windows cases handled by the batch part

| Case | Handling |
|---|---|
| Python missing / wrong version / only the Microsoft Store stub | Detect via `py -3.12`; otherwise stop with the python.org link and "tick Add to PATH / install for current user" |
| Git missing | `install.bat` works without Git (ZIP download); `update.bat` explains that updates need Git and shows the link |
| Several Python versions | Always `py -3.12` |
| No admin rights | Everything per user |
| PowerShell execution policy | Avoided: `python.exe` is called directly, never `Activate.ps1` |
| Downloaded file blocked (Mark of the Web, SmartScreen) | First screen explains "More info → Run anyway" / Properties → Unblock |
| Spaces and umlauts in paths | Everything quoted; console switched to UTF-8 (`chcp 65001`) |
| Corporate proxy | Read the Windows proxy setting for pip; if only a PAC script exists, ask once |
| SSL inspection during `pip install` | Retry with pip's `truststore` feature (Windows certificate store) |
| PyPI blocked | If a `wheels\` folder is present, install offline from it; how to build it is documented |
| Code folder in OneDrive is online-only | Pin it (`attrib +P`) so it stays on the device |
| Second run | Detect an existing environment and configuration; offer repair, update or reconfiguring one area |

## 5. Wizard stages

| # | Stage | Detected | Asked only if needed |
|---|---|---|---|
| 1 | Data folder | `%LOCALAPPDATA%\AI_OIL`; refuses any folder inside OneDrive (closes OP-53) | — |
| 2 | Me | Name and e-mail via `whoami /upn` and Outlook | Confirm |
| 3 | Backup | OneDrive Documents folder (also when redirected); proposes `Documents\AI_OIL-Backup` | Confirm or choose |
| 4 | Outlook | Classic vs. New Outlook (registry), COM reachable, folders resolvable | "Switch off New Outlook" when needed |
| 5 | Network | Proxy; TLS probe to Atlassian with and without the Windows store | — |
| 6 | Jira / Confluence | Site URL normalised from any pasted Jira link; Confluence derived | Token (hidden, to Credential Manager only); then live project and space lists to pick from |
| 7 | SharePoint | All OneDrive-synced libraries with name and URL from the OneDrive registry | Pick from a numbered list |
| 8 | Finish | Live check of every connector; first project | Start now? |

Rules: detect before asking; validate every answer at once and re-ask with the reason; write nothing until a stage is validated; every stage can be skipped (the summary lists what is open); the wizard never writes to Outlook, Jira, Confluence or SharePoint; the token is never shown or written to a file; re-running offers every current value as the default.

## 6. Verification

The wizard's system access (registry, environment, `whoami`, Outlook COM, HTTP, file system) sits behind injectable interfaces and is tested with fixtures here. The batch files can only be checked statically here (structure, quoting, line endings, no PowerShell activation); their real test is the first run on the laptop — which is why they stay thin.
