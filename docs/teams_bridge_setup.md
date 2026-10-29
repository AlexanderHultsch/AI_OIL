# Setting up the Teams bridge

Step-by-step guide for the two Power Automate flows of `docs/teams_bridge_concept.md`. About 45 minutes. Nothing here needs IT or admin rights, and nothing posts to Teams.

Labels below are the ones Microsoft uses at the time of writing; your tenant may word them slightly differently. Where a step does not match what you see, stop at that step and send me a screenshot. Do not work around it.

## 0. Quick check (5 minutes)

| # | Check | How | Expected |
|---|---|---|---|
| 0.1 | Power Automate opens | https://make.powerautomate.com | Your company is shown at the top right |
| 0.2 | You may create flows | **Create** → **Automated cloud flow** | The dialog opens; no licence message |
| 0.3 | Mention trigger exists | In the dialog, search `mentioned` | **When I am @mentioned** (Microsoft Teams), without a "Premium" badge |
| 0.4 | Chat trigger exists | Search `chat message` | **When a new chat message is added** (Microsoft Teams), without a "Premium" badge |
| 0.5 | Excel action exists | Later, inside a flow: **+ New step**, search `add a row` | **Add a row into a table** (Excel Online (Business)), without a "Premium" badge |

Close the dialog after 0.4 without creating anything. If any check fails, stop and tell me which.

## 1. The Excel file (10 minutes)

1. Open OneDrive in the browser (https://www.office.com → OneDrive), go to **My files → Documents**.
2. **New → Folder**, name it `AI_OIL-Teams`. It sits next to your `AI_OIL` folder, not inside it.
3. In `AI_OIL-Teams`: **New → Excel workbook**, rename it to `Teams_Bridge_2026`.
4. Type these headers into row 1, one per cell from A1 to J1:
   `MessageId`, `ConversationId`, `ConversationType`, `ConversationName`, `Timestamp`, `SenderName`, `SenderEmail`, `Permalink`, `Text`, `Trigger`
5. Select A1:J1 → **Insert → Table** → tick **My table has headers** → OK.
6. **Table Design** (or **Table**) → **Table Name**: `TeamsMessages`.
7. Close the workbook. On the laptop, right-click the `AI_OIL-Teams` folder in Explorer → **Always keep on this device**.

## 2. Flow A: mentions (15 minutes)

1. **Create → Automated cloud flow**. Name: `AI_OIL Teams mentions`. Trigger: **When I am @mentioned** → **Create**.
2. In the trigger, set **Message type** to cover chats and channels.
   - If the trigger only lets you pick one specific team, channel or chat, and offers no "any" option, stop here and send me a screenshot.
3. **+ New step → Get message details** (Microsoft Teams).
   - **Message**: the message id from the trigger (dynamic content).
   - **Message type**: the same type the trigger reported (dynamic content), or "Chat" and "Channel" if you were asked to choose one.
4. **+ New step → Add a row into a table** (Excel Online (Business)).
   - **Location** OneDrive for Business, **Document Library** OneDrive.
   - **File** `/Documents/AI_OIL-Teams/Teams_Bridge_2026.xlsx`, **Table** `TeamsMessages`.
5. Fill the columns from the dynamic content of **Get message details**:

| Column | Value |
|---|---|
| MessageId | Message id |
| ConversationId | Chat id; for a channel message, the channel id |
| ConversationType | `chat` or `channel` (dynamic content for the message type, or type the text) |
| ConversationName | Chat topic, or the channel name if offered; otherwise leave empty |
| Timestamp | Created date time |
| SenderName | From user display name |
| SenderEmail | From user e-mail or UPN, if offered |
| Permalink | Web URL |
| Text | Body content |
| Trigger | type `Mention` |

6. **Save**. Status must be **On**.

## 3. Flow B: chats (10 minutes)

1. **Create → Automated cloud flow**. Name: `AI_OIL Teams chats`. Trigger: **When a new chat message is added** → **Create**.
2. Leave the chat selection at **any chat**.
   - If it insists on one specific chat, stop and send me a screenshot. One flow per chat is not what we agreed.
3. Add **Get message details** (Message type `Chat`) and **Add a row into a table**, exactly as in flow A, steps 3 to 5. ConversationType is `chat` and Trigger is `Chat`.
4. **Save**. Status must be **On**.

## 4. End-to-end test (5 minutes)

| # | Test | Expected |
|---|---|---|
| 4.1 | Ask a colleague to send you a chat message | Within about two minutes, a row with Trigger `Chat` appears in the workbook online |
| 4.2 | Ask a colleague to @mention you in a channel | A row with Trigger `Mention` appears |
| 4.3 | Open `Documents\AI_OIL-Teams\Teams_Bridge_2026.xlsx` on the laptop | Both rows are there, so the local copy is up to date |
| 4.4 | In Power Automate, **My flows → each flow → Run history** | Runs show **Succeeded** |

Send me one row of each kind (you can blank out the text). From those I check that the columns arrive as the connector expects.

## 5. Every January

Create `Teams_Bridge_2027.xlsx` with the same table, and change the **File** in both flows to the new file. AI_OIL reads every `Teams_Bridge*.xlsx` in the folder, so older years stay searchable.

## 6. Connect AI_OIL

Once the Teams connector is built (step T2), run:
`"%LOCALAPPDATA%\AI_OIL\venv\Scripts\oil.exe" setup --only teams`
