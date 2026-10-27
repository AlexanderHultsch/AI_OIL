# Installing AI_OIL on the Windows laptop

First installation, first real run (open points OP-1 to OP-9), and how to update later.

## Where things go — and the OneDrive question

Keeping the **code** in your OneDrive-synced Documents folder is fine. Two things must **not** live in OneDrive:

| What | Where | Synced by OneDrive? | Why |
|---|---|---|---|
| Code (git clone) | `Documents\AI_OIL` | yes — fine | Text files that change only when you `git pull`. Mark the folder "Always keep on this device" |
| Python environment | `%LOCALAPPDATA%\AI_OIL\venv` | **no** | Thousands of files, and Python locks `.pyd` files while running — OneDrive then reports sync errors and can corrupt the environment |
| Database and data folder | `%LOCALAPPDATA%\AI_OIL` (the default) | **no** | A live SQLite database under a sync client is a known corruption risk (spec 4.3). Never point `AI_OIL_HOME` into OneDrive |
| Daily backups | `Documents\AI_OIL-Backup` | **yes — that's the point** | A closed copy made by SQLite's backup function is safe to sync; this is how your data survives a lost laptop (NFR-8) |

So the "issue" only concerns the database and the Python environment, and the default setup already keeps the database out of OneDrive.

## 0. Prerequisites

| Item | How to check |
|---|---|
| Python 3.12 (python.org installer, tick "Add python.exe to PATH" or use the `py` launcher) | `py -3.12 --version` |
| Git for Windows | `git --version` |
| Classic Outlook (not "New Outlook"), running and signed in | The "New Outlook" toggle at the top right must be off |
| Atlassian API token for Jira and Confluence | https://id.atlassian.com/manage-profile/security/api-tokens → "Create API token" |
| Your Atlassian account id (optional, for "me") | In Jira, click your avatar → Profile; the URL ends with `/people/<accountId>` |
| SharePoint libraries you want to link, synced with OneDrive | They appear in Explorer under your company name |

**PowerShell version matters for quoting.** Check with `$PSVersionTable.PSVersion`. In Windows PowerShell 5.1, every double quote *inside* a single-quoted argument must be written as `\"` (examples below show both forms). In PowerShell 7.3 or newer, write plain `"`.

## 1. Get the code

```powershell
cd ([Environment]::GetFolderPath('MyDocuments'))
git clone https://github.com/AlexanderHultsch/AI_OIL.git
cd AI_OIL
git checkout main
```

In Explorer: right-click the `AI_OIL` folder → "Always keep on this device".

## 2. Python environment (outside OneDrive)

```powershell
py -3.12 -m venv $env:LOCALAPPDATA\AI_OIL\venv
$env:LOCALAPPDATA\AI_OIL\venv\Scripts\python.exe -m pip install --upgrade pip
$env:LOCALAPPDATA\AI_OIL\venv\Scripts\python.exe -m pip install -e .
```

Then make `oil` available in this PowerShell window:

```powershell
& $env:LOCALAPPDATA\AI_OIL\venv\Scripts\Activate.ps1
oil --version
```

If activation is blocked ("running scripts is disabled"): `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or call `$env:LOCALAPPDATA\AI_OIL\venv\Scripts\oil.exe` directly.

If `pip` fails with certificate errors (corporate SSL inspection): `python -m pip install --cert C:\path\to\corporate-ca.pem -e .`. If PyPI is blocked entirely, tell me — the wheels can be prepared elsewhere (OP-8).

## 3. Initialise

```powershell
oil init --name "Alexander Hultsch" --email alexander.hultsch@<company>.com --atlassian-account-id <accountId>
```

This creates `%LOCALAPPDATA%\AI_OIL` with `ai_oil.db`, `config.toml`, `blobs\` and `logs\`.

## 4. Backups into OneDrive

```powershell
mkdir ([Environment]::GetFolderPath('MyDocuments') + '\AI_OIL-Backup')
notepad $env:LOCALAPPDATA\AI_OIL\config.toml
```

In the `[backup]` section set the folder with **forward slashes**:

```toml
[backup]
folder = "C:/Users/<you>/OneDrive - <Company>/Documents/AI_OIL-Backup"
keep = 14
```

Test it: `oil backup` → "Backup written: …". From now on every sync writes a backup once a day.

## 5. Connectors

Outlook (classic Outlook must be open):

```powershell
oil connector add outlook Outlook
```

Jira and Confluence (each asks for the API token in a hidden prompt; it goes to Windows Credential Manager only):

```powershell
# Windows PowerShell 5.1
oil connector add jira Jira --setting base_url=https://<tenant>.atlassian.net --setting email=<you>@<company>.com --setting 'projects=[\"ABC\",\"XYZ\"]' --token
oil connector add confluence Confluence --setting base_url=https://<tenant>.atlassian.net/wiki --setting email=<you>@<company>.com --setting 'spaces=[\"SPACE\"]' --token

# PowerShell 7.3+: the same with plain quotes, e.g. --setting 'projects=["ABC","XYZ"]'
```

SharePoint / OneDrive libraries — one entry per synced library (`url` = the library address as shown in the browser, `path` = the local folder in Explorer):

```powershell
# Windows PowerShell 5.1
oil connector add sharepoint SharePoint --setting 'sync_roots=[{\"url\":\"https://<tenant>.sharepoint.com/sites/<Site>/Shared Documents\",\"path\":\"C:/Users/<you>/<Company>/<Site> - Documents\"}]'
```

Check what is stored: `oil connector list` (tokens show only as `credential: stored`).

## 6. Live check (OP-1)

```powershell
oil connector check --live
```

Every line should read `[ok]`. If not:

| Message | Fix |
|---|---|
| Outlook: "classic Outlook is not available … new Outlook has no COM interface" | Switch off "New Outlook", start classic Outlook, run again |
| Jira/Confluence: certificate / SSL error | `oil connector set Jira --setting ca_bundle=C:/path/to/corporate-ca.pem` (same for Confluence) — OP-5 |
| Jira/Confluence: "authentication failed (401)" | Wrong e-mail or token: `oil connector set Jira --token` |
| SharePoint: "folder not found" | Fix the `path` with `oil connector set SharePoint --setting …` |
| "no secure credential store is available" | Should not happen on Windows; send me the output |

## 7. First real use

```powershell
oil project add OIL "Open issues"
oil serve
```

The browser opens http://127.0.0.1:8765/. Keep the PowerShell window open while you work (Ctrl+C stops the server).

Acceptance run (OP-2 to OP-7):

1. Create an issue ("Create" in the header).
2. In Outlook select an email of a real thread; on the issue page "Link source" → "Use selected Outlook email" → Link.
3. Reply to that thread in Outlook, then "Refresh" on the source → open the source page: both messages, correct times, your reply marked as outgoing (OP-2, OP-3, OP-4).
4. Link a Jira key, a Confluence page URL and a SharePoint file URL; make a change in each; Sync log → "Sync all now" → the changes appear (OP-5, OP-6, OP-7).
5. Search in the issue list, e.g. `text ~ "<word from an email>"`.

Please send me the output of `oil connector check --live` and anything that looked wrong.

## Updating later

```powershell
cd ([Environment]::GetFolderPath('MyDocuments') + '\AI_OIL')
git pull
$env:LOCALAPPDATA\AI_OIL\venv\Scripts\python.exe -m pip install -e .
```

Then restart `oil serve`. Database changes are applied automatically when `oil` starts; your data stays in `%LOCALAPPDATA%\AI_OIL`. Do not edit files in the clone — local changes make `git pull` stop with a conflict.

## Restoring from a backup

```powershell
oil restore "C:/Users/<you>/OneDrive - <Company>/Documents/AI_OIL-Backup/ai_oil-YYYYMMDD-HHMMSS.db" --yes
```

The current database is kept as a safety copy next to it.
