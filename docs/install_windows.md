# Installing AI_OIL on the Windows laptop

Install with `install.bat`, start with `start.bat`, update with `update.bat`. No admin rights, no PowerShell, no Python knowledge needed. First real run: open points OP-1 to OP-9 and OP-54.

## Where things go — and the OneDrive question

Keeping the **code** in your OneDrive-synced Documents folder is fine. Two things must **not** live in OneDrive:

| What | Where | Synced by OneDrive? | Why |
|---|---|---|---|
| Code (git clone or unzipped download) | `Documents\AI_OIL` | yes — fine | Text files that change only when you update. `install.bat` pins the folder so OneDrive keeps it on the device |
| Python environment | `%LOCALAPPDATA%\AI_OIL\venv` | **no** | Thousands of files, and Python locks `.pyd` files while running — OneDrive then reports sync errors and can corrupt the environment |
| Database and data folder | `%LOCALAPPDATA%\AI_OIL` (the default) | **no** | A live SQLite database under a sync client is a known corruption risk (spec 4.3). `oil setup` refuses a data folder inside OneDrive (OP-53) |
| Daily backups | `Documents\AI_OIL-Backup` | **yes — that's the point** | A closed copy made by SQLite's backup function is safe to sync; this is how your data survives a lost laptop (NFR-8) |

## Quick install

### Prerequisites

| Item | How to check |
|---|---|
| Python 3.12 from python.org, installed for the current user, "py launcher" ticked | `py -3.12 --version` in a Command Prompt |
| Classic Outlook (not "New Outlook"), signed in | The "New Outlook" toggle at the top right must be off |
| Atlassian API token for Jira and Confluence | https://id.atlassian.com/manage-profile/security/api-tokens → "Create API token". The wizard asks for it |
| Git for Windows (only needed for `update.bat`) | `git --version` |

`install.bat` itself needs only Python. If Python is missing it shows the download link and stops; it never installs anything on its own.

### 1. Get the code

| Way | Steps |
|---|---|
| Git (recommended, enables `update.bat`) | In a Command Prompt: `cd %USERPROFILE%\Documents`, then `git clone https://github.com/AlexanderHultsch/AI_OIL.git` |
| ZIP | On GitHub choose Code → Download ZIP. **Before unzipping**, right-click the ZIP → Properties → tick "Unblock" → OK. Then unzip into `Documents`, so that `Documents\AI_OIL\install.bat` exists |

### 2. Run `install.bat`

Double-click `install.bat` in the `AI_OIL` folder. If Windows warns about the file: "More info" → "Run anyway" (or right-click → Properties → Unblock).

It creates the environment in `%LOCALAPPDATA%\AI_OIL\venv`, installs AI_OIL (with fallbacks for a proxy, SSL inspection and blocked PyPI), pins the folder if it is inside OneDrive and starts the setup wizard. Run it a second time to reinstall packages or to run only the wizard.

### 3. The setup wizard

The wizard proposes, you confirm. Press Enter to accept the value in brackets, type `s` to skip a stage. Nothing is written before a stage is validated, and it never writes to Outlook, Jira, Confluence, SharePoint or Teams.

| Stage (`--only` name) | What it detects | What it asks |
|---|---|---|
| Data folder (`data`) | `%LOCALAPPDATA%\AI_OIL`; refuses any folder inside OneDrive | Nothing |
| About you (`me`) | Name and e-mail via Windows and Outlook | Confirmation |
| Backups (`backup`) | OneDrive Documents folder (also when redirected) | Confirm `Documents\AI_OIL-Backup` or choose another folder |
| Outlook (`outlook`) | Classic vs. New Outlook, COM reachable, folders resolvable | Whether to switch off New Outlook, if needed |
| Network (`network`) | Windows proxy; TLS test to Atlassian with and without the Windows certificate store | Nothing |
| Jira and Confluence (`atlassian`) | Site URL from any pasted Jira link; Confluence derived | API token (hidden, goes to Windows Credential Manager only); then pick projects and spaces from live lists |
| SharePoint and OneDrive files (`sharepoint`) | All OneDrive-synced libraries with name and URL | Pick from a numbered list |
| Teams chats (`teams`) | The `AI_OIL-Teams` folder in Documents and in OneDrive; its `Teams_Bridge*.xlsx` files, the `TeamsMessages` table and the newest captured message | Confirm or choose the folder; skipped with an open item until the bridge of `docs/teams_bridge_setup.md` exists |
| Finish (`finish`) | Live check of every connector | First project; whether to start AI_OIL now |

### 4. Start AI_OIL

Double-click `start.bat`. The browser opens http://127.0.0.1:8765/ (a free port is used if 8765 is busy). Keep the window open while you work; close it or press Ctrl+C to stop.

## Updating

Double-click `update.bat`. It needs Git: it pulls the new code (fast-forward only) and reinstalls the packages. Database changes are applied automatically the next time AI_OIL starts; your data stays in `%LOCALAPPDATA%\AI_OIL`. Restart `start.bat` if it was running.

| Situation | What happens | What to do |
|---|---|---|
| Local changes in the AI_OIL folder | `update.bat` stops and lists them, so nothing is lost | Copy the changed files elsewhere if you need them, then undo the changes: `git restore .` in the folder (untracked files: delete them or move them out). Run `update.bat` again |
| Folder came from a ZIP (no Git) | `update.bat` explains and stops | Download the new ZIP, unzip it over a new folder (Unblock first), run its `install.bat` — your data in `%LOCALAPPDATA%\AI_OIL` is kept |
| `git pull` cannot fast-forward | `update.bat` stops | Local commits exist: ask for help rather than merging by hand |

## Running the setup again

`install.bat` offers "[S]etup only" when AI_OIL is already installed. The same from a Command Prompt:

```
"%LOCALAPPDATA%\AI_OIL\venv\Scripts\oil.exe" setup
```

A second run shows the current values as defaults and asks which area to change.

| Command | Does |
|---|---|
| `oil.exe setup` | Whole wizard (re-run menu if already configured) |
| `oil.exe setup --only STAGE` | One stage, e.g. `--only atlassian` to enter a new token |
| `oil.exe setup --list` | List the stage names |

## Uninstalling

Double-click `uninstall.bat`. It asks twice, separately:

| Step | Answer | Effect |
|---|---|---|
| Program environment `%LOCALAPPDATA%\AI_OIL\venv` | Y / N | Removed or kept. `install.bat` recreates it |
| Your data (database, configuration, stored files) in `%LOCALAPPDATA%\AI_OIL` | Type `DELETE` exactly, or press Enter | Only the typed word deletes data. Backups in OneDrive are never touched |

Not removed: API tokens (Control Panel → Credential Manager → Windows Credentials, entries containing `ai_oil`) and the AI_OIL folder with the code — delete that folder yourself.

## Offline installation (PyPI blocked, OP-8)

If `install.bat` cannot reach PyPI, prepare the packages on a machine with internet and Python 3.12 — from the AI_OIL folder:

```
py -3.12 -m pip download -d wheels --only-binary=:all: --platform win_amd64 --python-version 3.12 . "setuptools>=69" pip
```

Copy the resulting `wheels\` folder into the AI_OIL folder on the laptop, then run `install.bat`. It tries the normal install first and falls back to `wheels\` automatically.

Some pure-Python packages are published only as source archives, so `--only-binary=:all:` can refuse them. In that case run the same command **on a Windows machine** without `--only-binary=:all:` (and without `--platform` and `--python-version`), so pip can build wheels for those packages locally.

## Live check and first real use

Run `start.bat`, or check from a Command Prompt:

```
"%LOCALAPPDATA%\AI_OIL\venv\Scripts\oil.exe" connector check --live
```

Every line should read `[ok]`. The wizard's Finish stage runs the same check.

Acceptance run (OP-2 to OP-7):

1. Create an issue ("Create" in the header).
2. In Outlook select an email of a real thread; on the issue page "Link source" → "Use selected Outlook email" → Link.
3. Reply to that thread in Outlook, then "Refresh" on the source → open the source page: both messages, correct times, your reply marked as outgoing (OP-2, OP-3, OP-4).
4. Link a Jira key, a Confluence page URL and a SharePoint file URL; make a change in each; Sync log → "Sync all now" → the changes appear (OP-5, OP-6, OP-7).
5. Search in the issue list, e.g. `text ~ "<word from an email>"`.

Please send me the output of `connector check --live`, the console output of `install.bat` if anything looked wrong (OP-54), and anything else that looked wrong.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `install.bat`: "Python 3.12 was not found" | Install Python 3.12 from https://www.python.org/downloads/windows/ for the current user, keep "py launcher" ticked, run `install.bat` again |
| "python" opens the Microsoft Store | That is a placeholder, not Python. The batch files use `py -3.12` and ignore it. Install Python from python.org as above. Optional: Settings → Apps → Advanced app settings → App execution aliases → switch off `python.exe` and `python3.exe` |
| pip: `SSL: CERTIFICATE_VERIFY_FAILED` | Corporate SSL inspection. `install.bat` retries automatically with the Windows certificate store (this needs the current Python 3.12 installer from python.org, whose pip uses the Windows certificate store). If it still fails, ask IT for the corporate CA file and install manually with `--cert C:\path\corporate-ca.pem` (see Manual installation) |
| pip: connection timed out, "Could not find a version" | A proxy is needed. `install.bat` reads the Windows proxy setting; if it is a script (PAC) it asks for the address once (e.g. `http://proxy.company.com:8080`). Ask IT for the address if you do not know it |
| PyPI blocked completely | Use the `wheels\` folder — see Offline installation |
| "running scripts is disabled on this system" | Does not apply: the batch files never use PowerShell. If you see it, you ran a `.ps1` file from an old instruction — use `install.bat` / `start.bat` instead |
| Windows warns about the `.bat` files | Mark of the Web from the ZIP download. "More info" → "Run anyway", or right-click the ZIP → Properties → Unblock *before* unzipping |
| `start.bat`: "Port 8765 is busy" | AI_OIL may already be running — use the browser tab that is open. Otherwise `start.bat` picks a free port automatically and prints it |
| Outlook: "classic Outlook is not available … new Outlook has no COM interface" | Switch off "New Outlook" (toggle top right), start classic Outlook, run `oil.exe setup --only outlook` |
| Jira/Confluence: certificate / SSL error | Normally nothing to do: the Windows certificate store is used automatically. Only if IT hands out a specific bundle: `oil connector set Jira --setting ca_bundle=C:/path/to/corporate-ca.pem` (same for Confluence). To switch the Windows store off: `oil connector set Jira --setting use_system_certificates=false` — OP-5 |
| Jira/Confluence: "authentication failed (401)" | Wrong e-mail or token: `oil.exe setup --only atlassian` and enter the token again |
| SharePoint: "folder not found" | The library is not synced or the path changed: `oil.exe setup --only sharepoint` |
| Setup: "data folder is inside OneDrive" (refused) | By design (OP-53). Use the default `%LOCALAPPDATA%\AI_OIL`, and do not set `AI_OIL_HOME` to a OneDrive path |
| "no secure credential store is available" | Should not happen on Windows; send me the output |
| `update.bat`: "You have local changes" | See Updating |

## Manual installation (reference)

Use this only if the batch files fail. Commands are for a Command Prompt; `oil` below means `"%LOCALAPPDATA%\AI_OIL\venv\Scripts\oil.exe"`.

```
cd %USERPROFILE%\Documents
git clone https://github.com/AlexanderHultsch/AI_OIL.git
cd AI_OIL
py -3.12 -m venv "%LOCALAPPDATA%\AI_OIL\venv"
"%LOCALAPPDATA%\AI_OIL\venv\Scripts\python.exe" -m pip install --upgrade pip
"%LOCALAPPDATA%\AI_OIL\venv\Scripts\python.exe" -m pip install -e .
"%LOCALAPPDATA%\AI_OIL\venv\Scripts\oil.exe" setup
```

Certificate errors from corporate SSL inspection: add `--use-feature=truststore` to the `pip install`, or `--cert C:\path\to\corporate-ca.pem`. Proxy: `set HTTPS_PROXY=http://proxy.company.com:8080` before `pip`.

Instead of the wizard you can configure everything by hand:

```
oil init --name "Alexander Hultsch" --email alexander.hultsch@<company>.com --atlassian-account-id <accountId>
oil connector add outlook Outlook
oil connector add jira Jira --setting base_url=https://<tenant>.atlassian.net --setting email=<you>@<company>.com --setting "projects=[\"ABC\",\"XYZ\"]" --token
oil connector add confluence Confluence --setting base_url=https://<tenant>.atlassian.net/wiki --setting email=<you>@<company>.com --setting "spaces=[\"SPACE\"]" --token
oil connector check --live
oil project add OIL "Open issues"
oil serve
```

Backups into OneDrive: create `Documents\AI_OIL-Backup`, open `%LOCALAPPDATA%\AI_OIL\config.toml` and set the folder with **forward slashes**:

```toml
[backup]
folder = "C:/Users/<you>/OneDrive - <Company>/Documents/AI_OIL-Backup"
keep = 14
```

Test it with `oil backup` → "Backup written: …". From then on every sync writes a backup once a day.

**PowerShell quoting.** The steps above need no PowerShell (in a Command Prompt the JSON value is written in double quotes with `\"` inside, as shown). If you do use PowerShell for `oil connector add … --setting` with JSON values, quoting depends on the version (`$PSVersionTable.PSVersion`). In Windows PowerShell 5.1, every double quote *inside* a single-quoted argument must be written as `\"`, e.g. `--setting 'projects=[\"ABC\",\"XYZ\"]'`. In PowerShell 7.3 or newer, write plain `"`: `--setting 'projects=["ABC","XYZ"]'`. SharePoint libraries need such a value (`url` = library address as in the browser, `path` = local folder in Explorer):

```
# Windows PowerShell 5.1
oil connector add sharepoint SharePoint --setting 'sync_roots=[{\"url\":\"https://<tenant>.sharepoint.com/sites/<Site>/Shared Documents\",\"path\":\"C:/Users/<you>/<Company>/<Site> - Documents\"}]'
```

`oil connector list` shows what is stored (tokens only as `credential: stored`).

## Restoring from a backup

```
"%LOCALAPPDATA%\AI_OIL\venv\Scripts\oil.exe" restore "C:/Users/<you>/OneDrive - <Company>/Documents/AI_OIL-Backup/ai_oil-YYYYMMDD-HHMMSS.db" --yes
```

The current database is kept as a safety copy next to it.
