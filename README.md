# Task Tracker

A personal GTD system with a project hierarchy, a Claude-powered quick-add, and a **feed** that serves you one
card at a time, chosen from an internal schedule that is rebuilt from your calendar, daily checklist, exercise
sheet, open tasks and your own rules document, and re-planned whenever the day drifts off it.

A standalone macOS app: a native window plus a menu bar item (current card, start/stop timer, complete,
quick add). Under the hood a local Python server on 127.0.0.1:8791 renders the UI inside the window; data is
SQLite in `~/Library/Application Support/TaskTracker/`.

## Setup

```bash
cd task-tracker-and-organizer
# .venv already created with Python 3.12; otherwise:
#   /opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...     # quick-add filing + intelligent scheduling
export TOGGL_API_TOKEN=...              # Toggl Track -> Profile -> API token
export LINEAR_API_KEY=lin_api_...       # Linear -> Settings -> API -> personal API key (then [linear] enable = true)
export SHEETS_TOKEN=...                 # the token you set in the Apps Script (see below)
.venv/bin/python -m tracker check       # shows what is configured and reachable
.venv/bin/python -m tracker             # run the app from the terminal (window + menu bar item)
```

### Run as a standalone app (recommended)

```bash
.venv/bin/python build_app.py           # -> dist/Task Tracker.app
open "dist/Task Tracker.app"            # or drag to /Applications; add to Login Items
```

A Finder-launched app does not see your shell environment, so store the keys where the app can find them:
the **Settings** page in the app (API keys go to the login Keychain, service "TaskTracker"; everything else is
written to `config.toml`, and the scheduling rules document is edited there too, with a "Test connections"
button), the menu bar item's **Set … Key…** entries, or `~/Library/Application Support/TaskTracker/env` as
`NAME=value` lines. The bundle references this project
directory (its venv and `config.toml`), so rebuild after moving the project. The launcher restarts the app if it
crashes and logs to `launcher.log` in the data folder.

Secrets can also live in the Keychain (`security add-generic-password -s TaskTracker -a toggl -w`; accounts
`toggl`, `linear`, `anthropic`, `sheets`) or in `~/Library/Application Support/TaskTracker/env`.
Everything else is in `config.toml`; scheduling preferences go in `Rules and heuristics for scheduling.md`.

### Calendar
Google Calendar -> Settings -> your calendar -> *Secret address in iCal format*. Paste one or more URLs into
`[calendar] ics_urls`. Recurring events are expanded; all-day and "free" events do not block time.

### Daily checklist and exercise sheets (read **and** write)
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
daily routines: each gets its own feed card every day, and marking it done ticks it in the sheet (today's date
column, a "Last done" column, or a "Done" checkbox, per `MODE`). The rest of the checklist is left to the sheet. The scheduler reads every other column (frequency, minutes, when...) as-is,
so you can lay the sheet out however you like: date headers may sit below other rows, real date cells work,
hidden rows are skipped, and a row is checkable only where today's cell is a checkbox (a formula row shows
its computed state, a heading or dropdown row is shown as text). Without the script, `daily_checklist_csv` /
`exercise_csv` CSV-export URLs work read-only.

### Linear
Set `LINEAR_API_KEY` and `[linear] enable = true`. Issues assigned to you are mirrored as tasks under the
"Linear" project (one sub-project per Linear project or team). Edits to title, description, due date and
importance, and completing a task, are pushed back; changes made in Linear arrive on the next sync
(every 10 min, or **Stats -> sync**, or `python -m tracker sync-linear`).

### Journal
`[journal] app` is opened and the Mac switches to `[journal] desktop` (needs the *Switch to Desktop N*
shortcut enabled in System Settings -> Keyboard -> Shortcuts -> Mission Control, and Accessibility permission
for the terminal). A "Journaling" Toggl timer starts.

## How it works

**Projects** - one outline, modelled on Workflowy and Roam. Every project header and task is a line; projects
are the bold lines. Every project heading gets a box, with sub-projects as boxes inside their parent's box;
nested tasks are simply indented. Inside an opened project its own tasks sit below its sub-projects; in the list they come first. There are no buttons
on the lines: you navigate with the arrow keys and type.

- Click a line, or arrow onto it, and it is open for editing (click on the text to put the caret there). Completed
  lines are only highlighted. Double-click a task to open its notes box and double-click a project to open just
  that project (Back button and breadcrumb at the top).
- Enter opens a new line below (the first child when the line shows children); on a project line it opens the
  project's first task. Enter in the middle of a line splits it. Enter on an empty new line steps it out one
  level: out of a sub-task, then out of the project as a new project line beside it (so a top-level project is
  one Enter away from any list). Tab / Shift+Tab nest and un-nest, including a new line at a project's root,
  which Shift+Tab turns into a project line. Cmd+B makes a task bold, i.e. a project, and a sub-project back
  into a task. The "+ Project" button at the top ("+ Sub-project" inside an opened project) adds one at the
  top of the list. Whatever you have typed in a line is kept when you click away; a split line undoes in one step.
- Up / Down move between lines, editor and all. Cmd+Up / Cmd+Down collapse and expand (Left / Right do the
  same on a highlighted completed line). Cmd+Shift+Up / Down move a line among its siblings.
- Cmd+Enter (or the checkbox) completes a task: it stays in the outline, ticked and struck through, until the
  small "archive N done" button at the top of its project puts the completed tasks away (Cmd+Z brings them
  back; the Archived tasks filter shows them). Shift+Enter opens its notes and, pressed again on the line or inside the box,
  closes them (the box also closes by itself when you move to another line), Cmd+E opens its dates and priority, Alt+Right /
  Alt+Left zoom into and out of a project, Cmd+Shift+Backspace deletes a line (Backspace on an emptied line
  does too). Backspace at the start of a line joins it onto the line above, which undoes an accidental split.
  Deleting is refused, with a message, for anything with sub-items, tracked time or a Linear link.
- Right-click a line for a small menu: complete, notes, dates & priority, make project (or open / make task on
  a project line) and Delete. From the menu a task is deleted together with its sub-items (Cmd+Z brings the
  whole subtree back); a project with content is archived rather than deleted.
- Text is Markdown everywhere: **bold**, *italic*, `code`, ~~strike~~ and [links](url) render in titles, names
  and notes, and a line that starts with one to six hashes (`## Big step`) is shown as a heading of that size.
  The raw text shows only while you edit it. Lines wrap while you type, so a long line stays fully visible.
- Tags typed in a line set its fields and disappear from the title: `#p1` to `#p5` for priority (`#p1`
  highest), `#today`, `#tomorrow` or `#due:fri` / `#due:nov3` / `#due:2026-11-03` for the deadline,
  `#blocking` or `#blocking:Sam`, and `#onesitting`. `#nodue`, `#p0` and `#notblocking` clear those again. They
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
- Inside an opened project its name and notes are edited in place and it can be archived, which takes its
  sub-projects and open tasks with it (restore brings them back). The Filters button (hidden by default)
  opens a small panel to show completed tasks and archived projects alongside the active ones.

**Snapshots** - every day at `[snapshots] time` (05:00) the whole outline is written to
`~/Library/Application Support/TaskTracker/snapshots/YYYY-MM-DD.md` (or `[snapshots] dir`): projects as a bold
nested list, open tasks as checkboxes with their tags, notes underneath, and the tasks completed since the last
snapshot at the end. If the app was asleep at that hour the file is written when it next runs that day.
The **Snapshots** tab lists them by day and shows any of them; its **Snapshot now** button (or
`python -m tracker snapshot`) writes one on demand.

**Exercise** - the exercise logging sheet written by your exercise app is the record. Its own sync script answers
`?action=export` with the logged sessions; paste that script's /exec URL as "Exercise log script URL" in Settings
and each session's category, start time and duration become the history (read-only), with the app's timers and
Toggl filling in only sessions the sheet lacks. `Exercise plan.md` (the Exercise box on the Settings page) holds your rules, one per line:
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
inferred. Without an API key it files into "Inbox".

**The schedule** is internal. It is built at the start of the work day (and on demand) from the free windows
between meetings, the prioritised open tasks (blocking > deadline > importance > age, with estimates
multiplied by your measured actual/estimate ratio), the checklist, the exercise sheet and the rules doc. Claude
does the packing when configured; a deterministic packer is the fallback and the validator (anything outside
the free windows is clamped or dropped).

**Toggl filing** - every timer the app starts is filed under the right Toggl project and tags a moment after it
starts: a description you have tracked before gets the project and tags it had last time; otherwise Claude
chooses from your own projects (with the descriptions you filed under each) and says "none" when it is not clear.
Exercise timers are named "going to do squats", "going to do cardio interval training": the session Claude
picked for the day, with travel time inside the timer. Off with `[toggl] auto_project = false`.

**The feed** shows one card; completing it flashes it green and slides it away, and the next card takes its place. Above the deck, from `meeting_alert_minutes` (45) before a meeting, a line says "Daily
regather in 12 minutes", counting down in whole minutes. The top card is the one the schedule says is next: Start (a Toggl timer tagged `task-tracker`, named
after the task), a completion checkbox (completes the task, pushes to Linear), "Done for now" for a block of a
task that continues later, Skip, and at the bottom the alternatives: today's exercise and Journal.
The checklist card lists the sheet's items with checkboxes.

**Drift** - every minute (and on every feed load) the schedule is compared with reality. If the next block is
more than `replan_drift_minutes` late or early, the active block has overrun by `overrun_minutes`, the day
finished early, or the calendar changed, the rest of the day is re-planned from now in the background; done,
skipped and active blocks are kept. Never more often than `replan_min_interval_minutes`.

**Estimates** - never user-entered. New tasks get a forecast from Claude (grounded in your recent actuals) and
time tracked per task (from the app's timers; if you stop the timer in Toggl the app notices) is compared with
it; the median ratio feeds back into scheduling. See the Stats tab. A task with open sub-tasks is treated as a
container and is not scheduled itself.

## Development

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m tracker plan --from-now   # print the plan the scheduler would build now
.venv/bin/python -m tracker web --reload
```

Layout: `tracker/app.py` (native window + menu bar item), `db.py` (schema), `scheduler.py` (windows,
priorities, packing, drift), `llm.py` (Claude prompts and schemas), `web.py` (API + background loop),
`dashboard.html` (UI), one module per integration. `python -m tracker web` serves the UI to a browser instead
(headless / development).
