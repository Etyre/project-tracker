# Project Tracker

A personal GTD system with a project hierarchy, a Claude-powered quick-add, and a **feed** that serves you one
card at a time, chosen from an internal schedule that is rebuilt from your calendar, daily checklist, exercise
sheet, open tasks and your own rules document, and re-planned whenever the day drifts off it. Built from
2026-10-29 (the original brief is `Project Tracker spec.md`) because no todo app combined a Workflowy-style
outline with a scheduler that decides *what now* from calendar, checklist, exercise and priorities, files time
into Toggl and mirrors Linear, all on this Mac with the data in a local SQLite file.

A standalone macOS app: a native window plus a menu bar item (current card, start/stop timer, complete,
quick add). Under the hood a local Python server on 127.0.0.1:8791 renders the UI inside the window; data is
SQLite in `~/Library/Application Support/TaskTracker/`.

## How it works

**The app** - `python -m tracker app` starts a FastAPI server on 127.0.0.1:8791 and opens a native window (a
WKWebView over it) plus a menu bar item. `dist/Project Tracker.app` is a tiny native launcher that runs this
project's `.venv` Python with `-m tracker app` and restarts it if it crashes (logged to `launcher.log` in the data
folder). The bundle references this project directory (its venv and `config.toml`), so rebuild after moving the
project. A background loop in the server (every minute) reconciles Toggl, syncs Linear, builds the day plan at the
start of the work day, re-plans on drift, prefetches the sheets at night, writes the daily snapshot and has Claude
judge the kind of work of tasks nobody marked. `python -m tracker web` serves the same UI to a browser instead
(headless / development).

**Projects** - one outline, modelled on Workflowy and Roam. Every project header and task is a line; projects
are the bold lines. Every project heading gets a box, with sub-projects as boxes inside their parent's box;
nested tasks are simply indented. Inside an opened project its own tasks sit below its sub-projects; in the list they come first. There are no buttons
on the lines: you navigate with the arrow keys and type.

- Click a task line, or arrow onto it, and it is open for editing (click on the text to put the caret there).
  Project lines and completed tasks are only highlighted; a project is renamed only inside the opened project.
  Double-click a task to open its notes box and double-click a project to open just that project (Back button
  and breadcrumb at the top).
- Enter opens a new line below (the first child when the line shows children); on a project line it opens the
  project's first task. Enter in the middle of a line splits it. Enter on an empty new line steps it out one
  level: out of a sub-task, then out of the project as a new project line beside it (so a top-level project is
  one Enter away from any list). Tab / Shift+Tab nest and un-nest, including a new line at a project's root,
  which Shift+Tab turns into a project line. Cmd+B makes a task bold, i.e. a project, and a sub-project back
  into a task. The "+ Project" button at the top ("+ Sub-project" inside an opened project) adds one at the
  top of the list. Whatever you have typed in a line is kept when you click away; a split line undoes in one step.
- Up / Down move between lines, editor and all. Cmd+Up / Cmd+Down collapse and expand (Left / Right do the
  same on a highlighted completed line). Cmd+Shift+Up / Down move a line among its siblings.
- Cmd+Enter (or the checkbox) completes a task: it stays where it is in the outline, ticked and struck through, until the
  small "archive N done" button at the top of its project puts the completed tasks away (Cmd+Z brings them
  back; the Archived tasks filter shows them). Shift+Enter opens its notes and, pressed again on the line or inside the box,
  closes them (the box also closes by itself when you move to another line), Cmd+E opens its dates and priority, Alt+Right /
  Alt+Left zoom into and out of a project, Cmd+Shift+Backspace deletes a line. Backspace itself never removes a
  line: a line you empty out goes away when you click elsewhere. Backspace at the start of a line joins it onto
  the line above, which undoes an accidental split; on an empty line it does nothing, so the line stays until you click elsewhere.
  Deleting is refused, with a message, for anything with sub-items, tracked time or a Linear link.
- Right-click a line for a small menu: complete, notes, dates & priority, make project (or open / make task on
  a project line) and Delete. From the menu a task is deleted together with its sub-items (Cmd+Z brings the
  whole subtree back); a project with content is archived rather than deleted.
- Text is Markdown everywhere: **bold**, *italic*, `code`, ~~strike~~ and [links](url) render in titles, names
  and notes, and a line that starts with one to six hashes (`## Big step`) is shown as a heading of that size.
  The raw text shows only while you edit it. Lines wrap while you type, so a long line stays fully visible.
- Tags typed in a line set its fields and disappear from the title: `#p1` to `#p5` for priority (`#p1`
  highest), `#today`, `#tomorrow` or `#due:fri` / `#due:nov3` / `#due:2026-11-03` for the deadline,
  `#blocking` or `#blocking:Sam`, `#onesitting`, and `#focus` or `#task` for the kind of work. `#nodue`, `#p0`,
  `#notblocking` and `#nokind` clear those again. They
  work in pasted outlines and the quick-add bar too; any other `#tag` stays in the text.
- Shift+Up / Shift+Down (or Shift+click) select several lines; Cmd+C copies them, with everything under
  them, as an indented bulleted list that pastes back into the app or anywhere else. With several lines
  selected, typing `#` opens a small field whose tags (`#p2 #due:fri`) apply to every selected task at once,
  and Cmd+E opens a dates-and-priority dialog for all of them; project lines in the selection are left alone.
  "Edit multiple" at the top switches to a selection mode where clicking any lines (not only a run) adds and
  removes them; the Tags… and Dates & priority… buttons beside the count apply to them, Done or Escape leaves.
- Cmd+Z undoes the last change on the page, as many steps back as you like. The feed has its own history:
  there Cmd+Z reverses the last completion, skip, "done for now" or checklist tick.
- Paste a bulleted or indented list into any line and it lands in the outline with its nesting: the first
  line goes into the text at the cursor, the rest become lines below it.
- Drag the handle to reorder; rows move live under the pointer, and dragging to the right over a row nests
  under it.
- A task's title line carries its deadline, a "blocking" tag and importance dots when set (there is no
  priority by default); its notes box has a "dates & priority…" link for the rest. Time estimates are never
  typed: the app forecasts one when a task is created and learns from tracked time.
- Every task is also judged to be a quick **task** (done in minutes, fits a gap) or **focused work** (needs a
  stretch of concentration), shown as a small tag on the line. Claude makes the call when the task is created
  (quick add, the outline, the agent) and works through older tasks in the background; `#focus` / `#task` in the
  title, the edit dialog or the dates-and-priority dialog set it by hand, `#nokind` hands it back to Claude. The
  day plan uses it: focused work gets long uninterrupted blocks, tasks fill the gaps. Claude also marks tasks that
  are mainly **reading** (`#reading` / `#notreading` by hand), which the daily reading card draws on, and mainly
  **writing** (`#writing` / `#notwriting`); a timer on such a task carries the "reading" or "writing" Toggl tag
  (`[reading] tag`, `[writing] tag`) whatever project it is filed under.
- **Someday / maybe** (its own tab) is a plain nested list of ideas you are not committing to: items with
  sub-items if you like, but no projects and no checkboxes, nothing there is ever completed (Enter or a click on
  the empty space adds one). Right-click a task anywhere for
  "Someday / maybe" and it moves there with its sub-tasks, dropping its deadline and priority; right-click an
  item there for "Make active…" and pick the project it goes into. Nothing on that list is scheduled or filed
  into by quick add, and its items carry no deadlines, priorities or blocking tags (typed tags stay as text).
- Inside an opened project its name and notes are edited in place and it can be archived, which takes its
  sub-projects and open tasks with it (restore brings them back). The Filters button (hidden by default)
  opens a small panel to show completed tasks and archived projects alongside the active ones.

**Drafts are never lost** - every text field in the UI (the quick-add bar, a new or edited line, a notes box, the
edit and bulk dialogs, the "what are you reading?" box on the feed, every field on the Settings page) saves what
you type, as you type, to the web view's `localStorage` under a key naming the field and the record it belongs to,
and shows it again the next time that field opens, after a re-render, a crash or a restart, until it is saved or
deliberately discarded (Escape, Cancel). Password fields (the API keys) are never kept.

**Focus monitor** - through the work day the feed prompts "Review your focus monitor" at a random interval of
`[focus_monitor] min_minutes` to `max_minutes` (60 to 120), with a macOS notification; Done on the feed starts the
next interval and ticks the "Review Focus Monitor" row in the sheet the first time each day.

**Nightly prefetch** - at `[sheets] prefetch_time` (23:50) the checklist, the exercise log and the calendar feeds
are read afresh and kept, so a midnight decision (the day's exercise, the recurring cards) uses that copy when the internet
is off at night. A session decided while the exercise log cannot be read is provisional and is decided again as soon
as the log is readable.

**Snapshots** - every day at `[snapshots] time` (05:00) the whole outline is written to
`~/Library/Application Support/TaskTracker/snapshots/YYYY-MM-DD.md` (or `[snapshots] dir`): projects as a bold
nested list, open tasks as checkboxes with their tags, notes underneath, and the tasks completed since the last
snapshot at the end. If the app was asleep at that hour the file is written when it next runs that day.
The **Snapshots** tab lists them by day and shows any of them; its **Snapshot now** button (or
`python -m tracker snapshot`) writes one on demand.

**Reading and writing** - the daily reading and writing rows of your checklist (`[reading] routine`, "Reading
books or papers"; `[writing] routine`, "Writing - 40 minutes") are recurring cards every day, like Anki review.
Each card offers the highest-priority open task of its kind from your projects (by the same priority score the
planner uses: blocking, deadline, importance, age), with "open in tree" and "finished it" links; Start runs the
timer on that task, so its time is tracked and filed under the task's project, and Done ticks the row in the sheet.
When nothing of that kind is queued the card falls back to `[reading] fallback` ("Read a book") or `[writing]
fallback` ("Free writing") and asks what you are reading or writing: that names the timer, which is filed by its
title (the project it had last time, else Claude's judgement for that title), never under a fixed project, since
it depends on the piece. Every reading or writing timer carries the "reading" or "writing" Toggl tag whatever its
project.

**Exercise** - the exercise logging sheet written by your exercise app is the record. Its own sync script answers
`?action=export` with the logged sessions; paste that script's /exec URL as "Exercise log script URL" in Settings
and each session's category, start time and duration become the history (read-only), with the app's timers and
Toggl filling in only sessions the sheet lacks. `Exercise plan.md` in the data folder (the Exercise box on the Settings page) holds your rules, one per line:
`squats: at least every 7 days, at most every 3 days`, `cardio interval training: at least every 4 days, at most
every 2 days`, `never back to back: deadlifts, squats`, `fill: benchpress, deadlifts, shoulder press`. Once each
morning (from 06:00, or when the first plan is built) the app applies them to the log with no model involved:
required items are due when their period has elapsed and allowed after their gap, the one done longest ago goes
first, never-back-to-back pairs are kept apart (including the day before the other falls due), and on days nothing
is due the fill-in done longest ago is picked. Claude then reviews the pick and may override it, rarely, when the other lines in the box or the
log clearly call for it (soreness, travel, a session you ask for); those lines are also the day plan's timing
notes. The choice is kept through every re-plan. The
length is never configured: it is the typical length of that kind in your Toggl history (the app's exercise
timers plus any Toggl entry tagged "exercise" or named like a known kind). Every day has a session. Without
Claude the plan cycles through `[exercise] rotation`.

**Quick add** (⌘K) - free text goes to Claude with the project tree; it returns one or more tasks placed in the
best project (creating a sub-project only when nothing fits), with deadline / blocking / importance / estimate
inferred. Without an API key it files into "Inbox". Claude's project path is matched against your tree by how the
names read (heading marks, case, punctuation and spacing ignored); a skipped or dropped level is found when the
name is unique; only the last name may be new, and only as a sub-project. A path whose first name is no top-level
project of yours puts the task in Inbox, with the notice saying so, rather than starting a new tree. The "Filed …
in …" lines of the notice at the bottom are links:
click one and the Projects page opens zoomed into that project, with the new task selected and its notes open.

**The schedule** is internal. It is built at the start of the work day (and on demand) from the free windows
between meetings, the prioritised open tasks (blocking > deadline > importance > age, with estimates
multiplied by your measured actual/estimate ratio), the checklist, the exercise sheet and the rules doc. Claude
does the packing when configured; a deterministic packer is the fallback and the validator (anything outside
the free windows is clamped or dropped).

**Claude and the LLM inbox** - every call to Claude (quick add, estimates and work kinds, the day plan, the
exercise review, Toggl filing) goes through one function, `structured()` in `tracker/llm.py`: a JSON-schema
constrained request to `[llm] model` with the system prompt cached as a prefix. Following the house standard in
`../CLAUDE.md`, that system prompt always starts with the shared preamble `../shared/llm-system-prompt-preamble.md`
(word for word, via `shared/llm_contact.py`), the `contact_eli` tool is on every call, and every response is passed
to `llm_contact.check()`. If the model calls `contact_eli` or writes an `@ELI:` line, the message with its full
context lands in the LLM inbox (`~/Library/Application Support/LLMInbox/messages.jsonl`), Eli is notified, the task
that was running stops with the message "Claude asked to talk to Eli (llm-inbox show <id>)" (quick add shows it,
the planner and the other callers fall back to their rules-only paths), and the app asks Claude nothing more until
**Settings -> Test connections** is pressed (which resumes it and says what had happened) or the app restarts.
Read and answer such messages with `llm-inbox` (see `../shared/README.md`).

**Toggl filing** - every timer the app starts goes to the right Toggl project, and nearly always without a model:

- *Recurring cards* (the checklist rows with their own card, the exercise sessions named in your exercise rules,
  journaling) use the **Recurring cards** list on the Settings page's Toggl card: one line each, `Anki review: Personal`.
  "Fill in with Claude" sorts every recurring card into one of your Toggl projects in one go (creating a project when
  none fits, and saying so); you edit the lines and save. A card missing from the list is decided by Claude
  the first time its timer starts, and the line is added for you.
- *Tasks* use the Toggl project their project is **linked** to (sub-projects inherit their parent's link). The
  opened project shows it as "Toggl: Palisade" in its header line; click it to pick another, or clear it so Claude
  decides again. The first timer in an unlinked project asks Claude, which reads your Toggl projects and clients
  with the descriptions you have filed under each, plus the project's path, notes and open tasks, and picks one,
  or creates a new Toggl project (with a client) when the project is plainly a new line of work. The link is saved
  on the project, so every later timer there is filed instantly.
- Anything still undecided (Claude off, or an error) falls back to the project this exact description had last
  time in Toggl, then to `[toggl] project` (the default), then to no project.

Exercise timers are named "going to do squats", "going to do cardio interval training": the session picked for
the day, with travel time inside the timer; they are listed under the session name ("squats"). Off with
`[toggl] auto_project = false`.

Toggl meters API calls per sliding hour: user-scoped reads (`/me/...`) get 30 an hour on every plan below
Enterprise, workspace-scoped calls get the plan's allowance (30 on Free). The app reads projects and clients
through workspace paths once an hour, fetches the entry history once an hour for everything that needs it, checks
whether you stopped the running timer in Toggl only every `[toggl] check_minutes` (5), and stops calling a bucket
that reports itself used up (HTTP 402) until Toggl says it resets. "Test connections" shows the user-scoped calls
left this hour. Every fetch of the history also trues up the app's own entries against Toggl's: a timer you stopped
in Toggl is stopped here at that time (its card goes back to pending), and starts or stops that drifted by more than
a minute take Toggl's values, so tracked time and the estimates learned from it stay right.

**The feed** shows one card; completing it flashes it green and slides it away, and the next card takes its place. Above the deck, from `meeting_alert_minutes` (45) before a meeting, or all day with `meeting_alert_all_day`, a line
says "12 minutes until Daily regather", counting down in whole minutes (hours and minutes when it is over an hour away). The top card is the one the schedule says is next: Start (a Toggl timer tagged `task-tracker`, named
after the task), a completion checkbox (completes the task, pushes to Linear), "Done for now" for a block of a
task that continues later, Skip, and at the bottom the alternatives: today's exercise and Journal. A task card
shows the task's own title (with the block's name underneath when the planner or the agent named it differently);
the agent's notes are not on the card at all, only on the Projects page. A task card's
"open in tree" link opens the Projects page zoomed into the task's own project, with the task selected and its notes
open. Once the day's
session is done the exercise button offers the extra session from Settings instead (`[exercise] after_done`, "zone 2
cardio" by default), and reads "(done)" once that has been tracked too.
The checklist card lists the sheet's items with checkboxes.

**Drift** - every minute (and on every feed load) the schedule is compared with reality. If the next block is
more than `replan_drift_minutes` late or early, the active block has overrun by `overrun_minutes`, the day
finished early, or the calendar changed, the rest of the day is re-planned from now in the background; done,
skipped and active blocks are kept. Never more often than `replan_min_interval_minutes`.

**Estimates** - never user-entered. New tasks get a forecast from Claude (grounded in your recent actuals) and
time tracked per task (from the app's timers; if you stop the timer in Toggl the app notices) is compared with
it; the median ratio feeds back into scheduling. See the Stats tab. A task with open sub-tasks is treated as a
container and is not scheduled itself.

### Integrations

**Calendar** - Google Calendar -> Settings -> your calendar -> *Secret address in iCal format*. Paste one or more
URLs into `[calendar] ics_urls`. Recurring events are expanded; all-day and "free" events do not block time.

**Daily checklist and exercise sheets (read and write)**
1. Open the spreadsheet with the checklist -> Extensions -> Apps Script, paste `apps_script/Code.gs`.
2. Script Properties: `SHEET_TOKEN` (random string), `CHECKLIST_SHEET` (tab name), `EXERCISE_SHEET`
   (and `EXERCISE_SHEET_ID` if the exercise log is a different file), optionally `ITEM_COLUMN` (a column
   letter or header), `HEADER_ROW` (found automatically: the first row holding a date) and `MODE`.
3. Deploy -> Web app (Execute as me, access Anyone). Put the `/exec` URL in `[sheets] apps_script_url`
   and the token in `SHEETS_TOKEN`. The manifest `apps_script/appsscript.json` (turn on "Show appsscript.json"
   in the project settings to paste it) asks only for the `spreadsheets.currentonly` scope, so the script can
   touch nothing but the spreadsheet it lives in; the exercise sheet must be a tab of the same file, or get its
   own copy of the script.

Rows listed under "Rows with their own card" in Settings (`[sheets] card_items`, e.g. `Anki review: 20`) are
recurring cards: each gets its own feed card every day, and marking it done ticks it in the sheet (today's date
column, a "Last done" column, or a "Done" checkbox, per `MODE`). The rest of the checklist is left to the sheet. The scheduler reads every other column (frequency, minutes, when...) as-is,
so you can lay the sheet out however you like: date headers may sit below other rows, real date cells work,
hidden rows are skipped, and a row is checkable only where today's cell is a checkbox (a formula row shows
its computed state, a heading or dropdown row is shown as text). Without the script, `daily_checklist_csv` /
`exercise_csv` CSV-export URLs work read-only.

**Linear** - set `LINEAR_API_KEY` and `[linear] enable = true`. Issues assigned to you are mirrored as tasks under the
"Linear" project (one sub-project per Linear project or team). Edits to title, description, due date and
importance, and completing a task, are pushed back; changes made in Linear arrive on the next sync
(every 10 min, or **Stats -> sync**, or `python -m tracker sync-linear`).

**Journal** - `[journal] app` is opened and the Mac switches to `[journal] desktop` (needs the *Switch to Desktop N*
shortcut enabled in System Settings -> Keyboard -> Shortcuts -> Mission Control, and Accessibility permission
for the terminal). A "Journaling" Toggl timer starts.

### Agent API

An executive-assistant agent gets its own door into the app, everything under `http://127.0.0.1:8791/api/agent`.
Whatever comes through it is attributed to the agent: tasks and projects it adds carry an `agent` chip, context it
attaches to any item is a separate signed note (never merged into your own text), and a schedule it sets is shown as
the agent's on the plan page. Interactive documentation of every call is at `/docs`; `GET /api/agent` returns the map.

**Access.** Optional bearer token: paste one into Settings -> Assistant agent (or `AGENT_API_TOKEN`, or the menu bar
item's *Set Agent API Token…*) and the agent sends `Authorization: Bearer <token>`. Without one, any process on this
Mac may call the agent API (the server only listens on 127.0.0.1). The agent names itself with `X-Agent-Name`
(default `assistant`); that name is what you see next to its notes.

| Call | What it does |
| --- | --- |
| `GET /api/agent/tree` | projects (with id, path, open-task count) and the full tree of open tasks, including the notes the agent added |
| `GET /api/agent/tasks?q=&project_id=` / `GET /api/agent/tasks/{id}` | find open tasks; one task with its path, tracked minutes and notes |
| `POST /api/agent/projects` | `{name, parent_id | parent_path, notes}`; the parent must exist |
| `POST /api/agent/tasks` | `{title, project_id | project_path | parent_id, description, deadline, blocking, blocking_for, importance, chunkable, note}`; hashtags in the title work (`#p2 #due:fri`); `note` is attached as the agent's note straight away; with no project given Claude files it like the quick-add bar (Inbox without Claude); `create_project: true` creates a missing path |
| `POST /api/agent/quick_add` | `{text}`: Claude turns free text into tasks in the right projects |
| `POST /api/agent/notes` | `{task_id | project_id, body}`: add context to any item; `GET /api/agent/notes?task_id=` lists, `DELETE /api/agent/notes/{id}` takes one back |
| `GET /api/agent/schedule?date=` | what the agent needs to set a day: the current plan with statuses, free windows between meetings, prioritised candidate tasks with calibrated estimates and the agent's notes, routines, the day's exercise, your rules document and any standing instructions |
| `PUT /api/agent/schedule` | `{items: [{kind, task_id, title, start, end, note}], date, work_start, work_end, rationale, pin}`: set the day's schedule block by block (kinds `task`, `routine`, `exercise`, `journal`, `break`, `meeting`). Blocks are clipped to the free windows and what did not fit is returned in `dropped`; calendar meetings are added; done, skipped and active blocks keep their status when a schedule is re-sent. With `pin` (the default) the drift re-planner leaves the day alone |
| `PUT /api/agent/schedule/instructions` | `{text, date, rebuild}`: steer the day in prose ("end by 13:00", "grant report first thing"); the app re-plans with the instructions appended to your rules, and shows them on the plan page. `DELETE` drops them |
| `POST /api/agent/schedule/rebuild` | `{date, from_now, until}`: let the app plan the day (this also unpins it) |
| `POST /api/agent/schedule/unpin?date=` | hand a pinned day back to the automatic re-planner |

```bash
A="http://127.0.0.1:8791/api/agent"; H=(-H "Content-Type: application/json" -H "X-Agent-Name: EA" -H "Authorization: Bearer $AGENT_API_TOKEN")
curl -s $A/tree "${H[@]}" | jq '.projects[] | {id, path}'
curl -s -X POST $A/tasks "${H[@]}" -d '{"title": "Book flights to Boston #p2", "project_path": ["Admin", "Travel"], "deadline": "2026-11-20", "note": "Dates confirmed with Sam on Slack"}'
curl -s -X POST $A/notes "${H[@]}" -d '{"task_id": 42, "body": "The venue replied: deposit is due Friday."}'
curl -s $A/schedule "${H[@]}" | jq '{free_windows, candidates: [.candidates[:5][] | {id, title, expected_minutes}]}'
curl -s -X PUT $A/schedule "${H[@]}" -d '{"items": [{"kind": "task", "task_id": 42, "start": "10:30", "end": "11:30"}, {"kind": "break", "title": "Break", "start": "11:30", "end": "11:40"}], "rationale": "Deposit first; rest of the day as planned."}'
curl -s -X PUT $A/schedule/instructions "${H[@]}" -d '{"text": "Eli has a dentist appointment at 14:00 and wants the afternoon light."}'
```

## Files

| File | Purpose |
| --- | --- |
| `tracker/__main__.py` | CLI entry point: `app`, `web`, `plan`, `snapshot`, `sync-linear`, `open`, `check`, `quick-add`; sets up logging |
| `tracker/app.py` | the native macOS app: WKWebView window + menu bar item (rumps / PyObjC) over the local server |
| `tracker/web.py` | FastAPI app: the JSON API, the dashboard page, the Settings endpoints and the background loop |
| `tracker/agent_api.py` | the executive-assistant agent's API under `/api/agent` |
| `tracker/dashboard.html` | the whole UI, one file: outline, feed, stats, snapshots, settings; drafts kept in `localStorage` |
| `tracker/db.py` | SQLite schema and queries (projects, tasks, notes, plans, timers, settings) |
| `tracker/scheduler.py` | free windows, priorities, the deterministic packer, drift detection, schedules set from outside |
| `tracker/llm.py` | the one Claude call site (`structured()`), every prompt and JSON schema; LLM-inbox wiring |
| `tracker/config.py` | `config.toml` + defaults, Keychain / env secrets, the log file, `save()` for the Settings page |
| `tracker/tags.py` | `#p2 #due:fri #blocking:Sam …` parsing |
| `tracker/estimates.py` | actual-vs-estimate calibration from tracked time |
| `tracker/exercise.py` | the exercise rules engine (cadence, gaps, never-back-to-back, fill-ins) and Claude's review |
| `tracker/calendar_feed.py` | iCal feeds: recurring events expanded, busy windows |
| `tracker/sheets.py` | checklist and exercise sheets via the Apps Script (read and tick) or CSV export (read-only); nightly prefetch |
| `tracker/toggl.py` | Toggl Track: timers, filing by project, quota-aware history fetches, true-up |
| `tracker/linear.py` | Linear mirror: pull assigned issues, push edits and completions |
| `tracker/journal.py` | the Journal button: open the app, switch desktop, start the timer |
| `tracker/snapshot.py` | the daily Markdown snapshot of the outline |
| `apps_script/Code.gs`, `apps_script/appsscript.json` | the Google Apps Script web app deployed on the checklist spreadsheet, and its manifest (current-spreadsheet-only scope) |
| `build_app.py` | builds `dist/Project Tracker.app` (native launcher in C, icon, Info.plist, codesign) |
| `tests/` | pytest suite: scheduler, agent API, LLM-inbox wiring (no network, temp database) |
| `config.toml` | your settings (untracked: it holds the calendar's secret address); written by the Settings page |
| `config.example.toml` | every key with its default, to copy |
| `Rules and heuristics for scheduling.example.md` | starter prose rules for the day plan; copied to the data folder on first run, where Claude reads your copy on every plan build (edited on the Settings page) |
| `Exercise plan.example.md` | starter exercise rules; copied to the data folder on first run (edited in the Exercise box on the Settings page) |
| `Project Tracker spec.md` | the original brief the app was built from |
| `requirements.txt` | Python dependencies (anthropic, fastapi, uvicorn, pydantic, icalendar, rumps, pyobjc, pytest…) |
| `.venv/` | the project's Python 3.12 virtualenv; the app bundle runs this interpreter |
| `dist/` | the built app bundle (untracked) |

## Runtime locations

- **Config**: `config.toml` in this folder (or `$TASK_TRACKER_CONFIG`), edited from the app's Settings page.
  The rules and exercise documents are `Rules and heuristics for scheduling.md` and `Exercise plan.md` in the data
  folder below (yours, never in the repo); the repo's `*.example.md` copies seed them on first run.
- **Secrets**: the login Keychain, service `TaskTracker`, accounts `anthropic`, `toggl`, `linear`, `sheets`, `agent`
  (`security add-generic-password -s TaskTracker -a toggl -w`); or the environment (`ANTHROPIC_API_KEY`,
  `TOGGL_API_TOKEN`, `LINEAR_API_KEY`, `SHEETS_TOKEN`, `AGENT_API_TOKEN`); or `NAME=value` lines in
  `~/Library/Application Support/TaskTracker/env`. Env wins over Keychain.
- **Data**: `~/Library/Application Support/TaskTracker/` (or `$TASK_TRACKER_DATA`): `tracker.sqlite3` (+ `-wal`/`-shm`),
  `snapshots/YYYY-MM-DD.md`, `launcher.log`, `env`.
- **Logs**: `~/Library/Logs/project-tracker.log` (timestamped, rotated at 5 MB) for everything the Python side logs;
  `~/Library/Application Support/TaskTracker/launcher.log` for the native launcher (starts, crashes, restarts).
- **Installed copy**: `dist/Project Tracker.app` in this folder (or wherever you dragged it, e.g. `/Applications`);
  the bundle always runs `.venv/bin/python -m tracker app` from this folder.
- **Server**: `http://127.0.0.1:8791` (`[general] host`/`port`); the agent API at `/api/agent`, API docs at `/docs`.
- **LLM inbox** (shared across projects): `~/Library/Application Support/LLMInbox/messages.jsonl`, log
  `~/Library/Logs/llm-inbox.log`.

## Common tasks

```bash
cd ~/Documents/agent-coding/project-tracker-and-organizer
# .venv already created with Python 3.12; otherwise:
#   /opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...     # quick-add filing + intelligent scheduling (or the Keychain / Settings page)
export TOGGL_API_TOKEN=...              # Toggl Track -> Profile -> API token
export LINEAR_API_KEY=lin_api_...       # Linear -> Settings -> API -> personal API key (then [linear] enable = true)
export SHEETS_TOKEN=...                 # the token you set in the Apps Script (see Integrations)
.venv/bin/python -m tracker check       # shows what is configured and reachable (and whether Claude is paused)
.venv/bin/python -m tracker             # run the app from the terminal (window + menu bar item)

# build and run the standalone app (recommended)
.venv/bin/python build_app.py           # -> dist/Project Tracker.app
open "dist/Project Tracker.app"         # or drag to /Applications; add to Login Items
.venv/bin/python -m tracker open        # bring the running app's window to the front (starts it if needed)

# stop / restart the running app (never pkill by name: a silently failed pkill leaves the old code running)
pgrep -fl "tracker app"                 # the python process; its parent is .../Project Tracker.app/Contents/MacOS/ProjectTracker
kill <launcher pid> <python pid>        # quit both (killing only python makes the launcher restart it with the current code)
open "dist/Project Tracker.app"

# test and develop
.venv/bin/python -m pytest -q           # the end-to-end proof without the real triggers: scheduler, agent API,
                                        # LLM-inbox wiring, all against a temporary database, no network
.venv/bin/python -m tracker plan --from-now   # print the plan the scheduler would build now (calls Claude if configured)
.venv/bin/python -m tracker quick-add "Book flights to Boston by Friday #p2"   # one quick-add through Claude, printed as JSON
.venv/bin/python -m tracker snapshot    # write today's Markdown snapshot now
.venv/bin/python -m tracker sync-linear # pull Linear issues now
.venv/bin/python -m tracker web --reload   # serve the UI to a browser, reloading on code changes (headless / development)

# logs
tail -f ~/Library/Logs/project-tracker.log
tail -f ~/Library/Application\ Support/TaskTracker/launcher.log
llm-inbox list                          # messages Claude sent Eli from any app (see ../shared/README.md)
```

`dashboard.html` is read from disk on every page load, but the app's window loads the page once per process (closing
and reopening the window does not reload it), so UI changes, like Python changes, show after the app is restarted
as above; `python -m tracker web --reload` in a browser is the quick way to see them while developing.

## Settings that matter

- **Where the keys live**: a Finder-launched app does not see your shell environment, so store the keys where the
  app can find them: the **Settings** page in the app (API keys go to the login Keychain, service "TaskTracker";
  everything else is written to `config.toml`, and the scheduling rules document is edited there too, with a "Test
  connections" button), the menu bar item's **Set … Key…** entries, or `~/Library/Application Support/TaskTracker/env`
  as `NAME=value` lines. Secrets can also be put in the Keychain by hand (`security add-generic-password -s TaskTracker
  -a toggl -w`; accounts `toggl`, `linear`, `anthropic`, `sheets`). Everything else is in `config.toml`; scheduling
  preferences go in `Rules and heuristics for scheduling.md` in the data folder. `config.toml` is yours and stays out of git (it holds
  your calendar's secret address); `config.example.toml` shows every key.
- **Google Calendar**: Settings -> your calendar -> *Secret address in iCal format* -> `[calendar] ics_urls`.
- **Google Sheets**: the Apps Script deployed on the checklist spreadsheet as a web app (Execute as me, access
  Anyone) with the Script Properties listed under Integrations; its `/exec` URL in `[sheets] apps_script_url` and the
  token in `SHEETS_TOKEN`. The exercise app's own sync script `/exec` URL goes in "Exercise log script URL".
- **Linear**: a personal API key (Settings -> API) in `LINEAR_API_KEY` and `[linear] enable = true`.
- **Toggl Track**: Profile -> API token. Toggl allows 30 user-scoped API calls an hour on plans below Enterprise.
- **Claude**: an Anthropic API key (Keychain account `anthropic`), `[llm] model` (currently `claude-opus-5-5`),
  `[llm] effort` for plans and `[llm] quick_add_effort`. Also works with an `ant auth login` profile. The shared LLM
  inbox needs `~/Library/Application Support/LLMInbox/config.env` for phone push and email (`../shared/README.md`).
- **macOS**: for the Journal button, the *Switch to Desktop N* shortcut enabled in System Settings -> Keyboard ->
  Shortcuts -> Mission Control, and Accessibility permission for the terminal (or the app). To start at login, add
  `Project Tracker.app` to Login Items. The bundle is signed with the first codesigning identity found (or ad hoc) and
  has `NSAppleEventsUsageDescription` for the desktop switch.
- **Assistant agent**: an optional bearer token in Settings -> Assistant agent (`AGENT_API_TOKEN`); without one any
  local process may call `/api/agent`.

## Gotchas learned

- A Finder-launched app does not see your shell environment; `ANTHROPIC_API_KEY` exported in `.zshrc` does nothing
  for the bundle. Use the Keychain (Settings page), the menu bar item, or the `env` file.
- The bundle references this project directory (its venv and `config.toml`): move the project and the app breaks
  until `build_app.py` is rerun.
- Until 2026-11-07 the Python side logged only to stdout, which goes nowhere under a Finder launch; everything now also
  goes to `~/Library/Logs/project-tracker.log`.
- The Keychain may not answer in the first second after launch, so a failed credential check is never remembered:
  only a positive answer is cached (`llm._has_credentials`).
- macOS window-state restoration after a crash blocks launch with a modal "reopen windows?" alert before the run loop
  starts; the app registers `ApplePersistenceIgnoreState` to avoid it.
- `codesign` sometimes fails with "resource fork, Finder information, or similar detritus not allowed" right after
  the files were written; `build_app.py` runs `xattr -cr` and retries up to five times.
- Toggl meters API calls per sliding hour (30 user-scoped `/me/...` calls on plans below Enterprise) and answers 402
  when a bucket is used up; the app spaces its reads and backs off, and "Test connections" shows the calls left.
- The Apps Script must ask only for `spreadsheets.currentonly` (via the manifest), so the exercise sheet has to be a
  tab of the same spreadsheet or get its own copy of the script.
- Quitting the Python process alone makes the native launcher restart it (that is its job); to stop the app kill the
  launcher too, or quit from the menu bar item. Never `pkill` by name without checking it worked.
- The LLM inbox's `@ELI:` marker is matched anywhere in a text block, so a task title containing `@ELI:` echoed back
  in Claude's JSON would be taken as a contact and pause Claude until Test connections. Unlikely, but known.
- Quick add and the planner used to hold the model's answer only in memory; the task-kind backlog classifier runs
  every 5 minutes, so after a contact from the model it would have asked again and again. That is why a contact
  pauses Claude app-wide rather than only failing the one call.
