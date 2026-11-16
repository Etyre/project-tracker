# Project Tracker

A personal GTD system with a project hierarchy, a Claude-powered quick-add, and a **feed** that serves you one
card at a time from a stack of your open tasks and daily routines, ordered by priority and filtered by how much
time you have before your next meeting. Built from 2026-10-29 (the original brief is `Project Tracker spec.md`)
because no todo app combined a Workflowy-style outline with a feed that decides *what now* from calendar,
checklist, exercise and priorities, files time into Toggl and mirrors Linear, all on this Mac with the data in a
local SQLite file. (Until 2026-11-13 the feed followed a day schedule the app built and re-planned; that was
replaced by the stack, and the planner is now an on-demand plan page.)

A standalone macOS app: a native window plus a menu bar item (current card, start/stop timer, complete,
quick add). Under the hood a local Python server on 127.0.0.1:8791 renders the UI inside the window; data is
SQLite in `~/Library/Application Support/TaskTracker/`.

## How it works

**The app** - `python -m tracker app` starts a FastAPI server on 127.0.0.1:8791 and opens a native window (a
WKWebView over it) plus a menu bar item. `dist/Project Tracker.app` is a tiny native launcher that runs this
project's `.venv` Python with `-m tracker app` and restarts it if it crashes (logged to `launcher.log` in the data
folder). The bundle references this project directory (its venv and `config.toml`), so rebuild after moving the
project. A background loop in the server (every minute) reconciles Toggl, syncs Linear, decides the day's exercise,
prefetches the sheets at night, writes the daily snapshot and has Claude judge the kind of work of tasks nobody
marked. Nothing in it plans the day: the feed is computed on each request. `python -m tracker web` serves the same UI to a browser instead
(headless / development).

**Projects** - one outline, modelled on Workflowy and Roam. Every project header and task is a line; projects
are the bold lines. Every project heading gets a box, with sub-projects as boxes inside their parent's box;
nested tasks are simply indented. A project's own tasks, the ones not in any sub-project, come first, above its
sub-projects, both in the list and inside the opened project. There are no buttons
on the lines: you navigate with the arrow keys and type.

- Click a task line, or arrow onto it, and it is open for editing (click on the text to put the caret there).
  Project lines and completed tasks are only highlighted; a project is renamed only inside the opened project.
  Double-click a task to open its notes box and double-click a project to open just that project (Back button
  and breadcrumb at the top).
- Enter opens a new line below (the first child when the line shows children; always a sibling right below
  when the line is ticked off); on a project line it opens the
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
  a project line), Archive and Delete. Archive puts the line away without deleting it: a task goes with its
  sub-items, finished or not, and an unfinished one is not counted as completed, it just leaves the outline
  and the schedule (Cmd+Z brings it back; the Archived tasks filter shows it in grey italic, where right-click
  offers Restore). From the menu a task is deleted together with its sub-items (Cmd+Z brings the
  whole subtree back); a project can always be archived, and deleted only when it is empty.
- Text is Markdown everywhere: **bold**, *italic*, `code`, ~~strike~~ and [links](url) render in titles, names
  and notes, and a line that starts with one to six hashes (`## Big step`) is shown as a heading of that level:
  `#` is large and heavy with a solid rule under it, `##` bold with a thin rule, `###`, `####` and `#####`
  semibold in decreasing sizes, `######` small capitals in grey. No heading is in the accent colour: blue
  reads as a link.
  The raw text shows only while you edit it. Lines wrap while you type, so a long line stays fully visible.
- Tags typed in a line set its fields and disappear from the title: `#p1` to `#p5` for priority (`#p1`
  highest), `#today`, `#tomorrow` or `#due:fri` / `#due:nov3` / `#due:2026-11-03` for the hard deadline,
  `#soft:fri` / `#aim:nov3` / `#ideally:2026-11-03` for the soft one, `#blocking` or `#blocking:Sam`,
  `#onesitting`, and `#focus` or `#task` for the kind of work. `#nodue`, `#nosoft`, `#p0`,
  `#notblocking` and `#nokind` clear those again. They
  work in pasted outlines and the quick-add bar too; any other `#tag` stays in the text.
- Shift+Up / Shift+Down (or Shift+click) select several lines; Cmd+C copies them, with everything under
  them, as an indented bulleted list that pastes back into the app or anywhere else. With several lines
  selected, typing `#` opens a small field whose tags (`#p2 #due:fri`) apply to every selected task at once,
  and Cmd+E opens a dates-and-priority dialog for all of them; project lines in the selection are left alone.
  Dragging the handle of one selected line moves every selected task at once, each with what is under it, to
  where you drop it, in their original order; the lines stay selected afterwards, and Cmd+Z puts them all back.
  "Edit multiple" at the top switches to a selection mode where clicking any lines (not only a run) adds and
  removes them; the Tags… and Dates & priority… buttons beside the count apply to them, Done or Escape leaves.
- Cmd+Z undoes the last change on the page, as many steps back as you like. The feed has its own history:
  there Cmd+Z reverses the last completion, skip, "done for now" or checklist tick.
- Paste a bulleted or indented list into any line and it lands in the outline with its nesting: the first
  line goes into the text at the cursor, the rest become lines below it.
- Drag the handle to reorder; rows move live under the pointer, and dragging to the right over a row nests
  under it.
- Who last set a task's priority is recorded, though not shown: `tasks.importance_by` is `user` (the page: a
  typed `#p2`, the tag line, the dialog), `agent` (the assistant agent, through `/api/agent`), `claude` (quick
  add read it from the note) or `linear` (the mirrored issue's priority), with the time in `importance_at`.
  It changes only when the priority itself changes, so saving a dialog or a sync repeating the same value takes
  no credit; clearing a priority counts as setting it. Empty means it was set before 2026-11-14, when tracking
  began. The agent sees both fields on every task it reads.
- A task's title line carries only what bears on urgency: its deadlines, a "blocking" tag and importance dots
  when set (there is no priority by default). The descriptive tags (focused work or quick task, reading,
  writing, added by the agent) appear on the line only while the task is open. The opened task (double-click, or Notes in the menu) shows its notes, then its **tag
  line** and its two deadlines, all editable in place. The tag line is the task's fields written as tags
  (`#p2 #due:2026-11-14 #blocking:Sam #onesitting #focus #reading`): edit it and press Enter, and the task has
  exactly those tags, so a tag you delete clears that field (Escape puts the line back; words that are not
  tags are refused with a toast and the text stays to fix; a "who is waiting" name with a space shows as plain
  `#blocking` and is kept). The "due" and "aim" date fields beside it save as soon as they change. Saving
  updates the line in place and is undone with Cmd+Z. The "dates & priority…" link still opens the full dialog.
  Time estimates are never typed: the app forecasts one when a task is created and learns from tracked time.
- **Two kinds of deadline.** A *hard* deadline ("due Fri") cannot move: someone else's date, a meeting, a filing.
  A *soft* deadline ("aim Fri") is your own ideal or preference. A task may have either or both (aim for Wednesday,
  must be in by Friday). The planner treats them differently: a hard deadline is met with margin even at the cost
  of other work, a soft one is aimed for but yields to hard deadlines, blocking work and higher importance. In the
  priority score a hard date counts in full plus a step, a soft date for about half; with both, the more pressing
  one counts. Quick add asks Claude to tell them apart ("by Friday" is hard, "ideally this week" is soft).
- Every task is also judged to be a quick **task** (done in minutes, fits a gap) or **focused work** (needs a
  stretch of concentration), shown as a small tag on the line. Claude makes the call when the task is created
  (quick add, the outline, the agent) and works through older tasks in the background; `#focus` / `#task` in the
  title, the edit dialog or the dates-and-priority dialog set it by hand, `#nokind` hands it back to Claude. The
  day plan uses it: focused work gets long uninterrupted blocks, tasks fill the gaps. The prompt carries Eli's
  own rules of thumb: agent coding (directing Claude Code) is almost always a task, writing is almost always
  focused work, and reading is focused work only when the material is hard to parse. Claude also marks tasks that
  are mainly **reading** (`#reading` / `#notreading` by hand), which the daily reading card draws on, and mainly
  **writing** (`#writing` / `#notwriting`); a timer on such a task carries the "reading" or "writing" Toggl tag
  (`[reading] tag`, `[writing] tag`) whatever project it is filed under.
- **Someday / maybe** (its own tab) is a plain nested list of ideas you are not committing to: items with
  sub-items if you like, but no projects and no checkboxes, nothing there is ever completed (Enter or a click on
  the empty space adds one). Right-click a task anywhere for
  "Someday / maybe" and it moves there with its sub-tasks, dropping its deadline and priority; right-click an
  item there for "Make active…" and pick the project it goes into. Nothing on that list is scheduled or filed
  into by quick add, and its items carry no deadlines, priorities or blocking tags (typed tags stay as text).
- Every project can have a subtitle: one line under its name. Inside an opened project it always shows, and a
  click on it (or on "Add a subtitle…") edits it in place; Enter or clicking away saves, an empty line removes
  it. In the main list it shows under the project's name when Settings > Projects > "Subtitles in the list" is
  on (`[general] show_subtitles`, on by default).
- Inside an opened project its name and notes are edited in place and it can be archived, which takes its
  sub-projects and open tasks with it (restore brings them back). The Filters button (hidden by default)
  opens a small panel to show completed tasks and archived projects alongside the active ones.

**Drafts are never lost** - every text field in the UI (the quick-add bar, a new or edited line, a notes box, the
edit and bulk dialogs, the "what are you reading?" box on the feed) saves what
you type, as you type, to the web view's `localStorage` under a key naming the field and the record it belongs to,
and shows it again the next time that field opens, after a re-render, a crash or a restart, until it is saved or
deliberately discarded (Escape, Cancel). Password fields (the API keys) are never kept.

**Settings save themselves** - the Settings page has no Save button. A tick or a menu choice is saved at once, typing
after a 0.7 s pause or on leaving the field; the line at the bottom says "Saved 14:03", or why a save failed. Each
save sends the whole form (`PUT /api/settings`), collected at the moment of the change, so leaving the page inside
the pause still saves. Text fields also keep a draft until their save succeeds: text typed just before a restart or
a failed save comes back when Settings is next opened and is saved then. So restarting the app loses nothing typed
there. The API keys are the exception: each has its own Save button, because a half-pasted key must not go to the
Keychain. The calendar feeds are fetched again only when a calendar setting changed, not on every save.

**Focus monitor** - through the work day the feed prompts "Review your focus monitor" at a random interval of
`[focus_monitor] min_minutes` to `max_minutes` (60 to 120), with a macOS notification; Done on the feed starts the
next interval and ticks the "Review Focus Monitor" row in the sheet the first time each day.

**Nightly prefetch** - at `[sheets] prefetch_time` (23:50) the checklist, the exercise log and the calendar feeds
are read afresh and kept, so a midnight decision (the day's exercise, the recurring cards) uses that copy when the internet
is off at night. A session decided while the exercise log cannot be read is provisional and is decided again as soon
as the log is readable.

**Time** - everything is in your Mac's local time: dates, the work window, plan and feed times, the midnight
decisions and the calendar. Timestamps sent to Toggl and Linear are UTC, as their APIs require, and are converted
back to local time when read. There is no timezone setting; change the Mac's clock and the app follows.

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

**The stack** (`tracker/feed.py`) is what the feed serves, computed afresh on every request, never stored. It is
today's recurring cards first (the checklist rows with a card, the daily reading and writing), then every open
task in a live project (a task with open sub-tasks is a container and stays out; Someday items stay out) ordered
by priority score (blocking > hard deadline > soft deadline > importance > age). Each card has an expected length:
the routine's minutes, or the task's estimate multiplied by your measured actual/estimate ratio. Then the **gap**
is measured: the minutes until the next meeting on your calendar, or until the end of the work day when no meeting
is left (during a meeting, until the one after it; in the evening nothing bounds it). A card is offered only if it
fits the gap: its length is within it, or the task can be split (`chunkable`, the default; `#onesitting` turns it
off) and at least `min_block_minutes` are left; focused work (`#focus`, or judged so by Claude) is split only into
a stretch of 45 minutes or more, since it needs a run of concentration. Writing and reading have a floor of
their own, whatever the task's length: writing (a task marked `#writing`, or the daily writing card) is started
only with at least an hour free, reading (`#reading`, or the daily reading card) only with at least 20 minutes
(`[writing] min_gap_minutes`, `[reading] min_gap_minutes`; Settings > Writing / Reading). With 15 minutes before a meeting the top
card is the best task of 15 minutes or less, or the best quick task that can be done in pieces; the rest are
"waiting for a longer gap" and come back when it opens. "Next" on a card moves it to the bottom of the stack for the rest of the day (undo brings it
back); a card whose timer is running stays on top until it is stopped or done.

**The plan page** still builds a day schedule, but only when you ask (Build / Re-plan on the Plan page, or the
agent's `POST /api/agent/schedule/rebuild`): the free windows between meetings, the same prioritised tasks, the
checklist, the exercise sheet and the rules doc, packed by Claude when configured and by a deterministic packer
otherwise. Nothing builds or re-plans it by itself any more, and the feed does not read it, with one exception
below. Completing a card marks the matching block done, so the page stays roughly true when a plan exists.

**The agent's schedule orders the stack.** A schedule your assistant agent sends through the agent API is stored
as the day's plan and shown on the plan page, and the tasks it lists go to the top of the feed's stack in the
agent's order (still subject to the gap: a block that does not fit before the next meeting waits). Its times are
not followed: the feed has no schedule to keep. The one thing that is fixed once set is the **day's exercise**: the
morning decision stands for the day, and an exercise block the agent sends only names the session when nothing is
decided yet for that day (for a later day the choice waits in the plan until that morning); otherwise the block is
retitled to the decided session and the reply says so.

**The agent's priorities steer the stack.** `PATCH /api/agent/tasks/{id}` sets a task's importance, deadline,
blocking (and for whom), kind of work, reading/writing and whether it may be split, with a `why`. The change is
written as the agent's signed note on the task, and those are exactly the inputs the stack's order and the gap
test are made from, so the feed follows the agent's priorities and urgency. Text about what to keep in mind goes
through `PUT /api/agent/schedule/instructions`: for one day, or with `standing: true` for every day until replaced.
Both are appended to your rules document for the plan page's planner and shown there.

**The planning log** records every planning decision so the agent can see everything the app did on its own the
next time it looks: each plan built on request (who asked, the free windows, the candidates considered with their
priorities, the exercise and routines it had, the agent's schedule it adjusted and the blocks it produced), each
exercise decision, each schedule the agent set, each instruction, guidance and priority change.
`GET /api/agent/schedule` returns the day's log and everything logged since that agent (by name) last looked;
`GET /api/agent/schedule/log` queries it by day or since a moment.

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
The shared confirm step (`llm_contact.with_confirm`, added 2026-11-10 because small models use the tool for routine
remarks) is wired in before that check, but it only runs for models in the shared `llm_contact.CONFIRM_MODELS` list
(today only `claude-haiku-4-5`), so it is **inactive for the current `[llm] model`, `claude-opus-5-5`**: no question
is asked and a `contact_eli` call is sent at once. On a listed model a `contact_eli` tool call is followed by one
forced-choice question, "send to Eli and stop" or "it was a remark about the task, keep working". Only an explicit "task remark" continues: the model then
answers in the normal format and what it had said is kept in `LLMInbox/unconfirmed.jsonl`. Send, an unclear answer or
any error in the question sends the original message as above. An `@ELI:` line is never questioned.

**Toggl entries are gerunds** - a task's timer is called "Responding to JDPressman" in Toggl, not "Respond to
JDPressman". Claude phrases the title that way in the same call that forecasts the estimate when the task is created
(`toggl_title` on the task; the backlog pass fills it for older tasks, and a title edit has it re-phrased), and a
rule-based fallback in `tracker/toggl.py` (`gerund`) covers a task Claude has not reached yet or Claude being off.
The card in the app keeps the task's own title; only the Toggl description changes. Recurring cards, exercise and
journaling keep their own names ("going to do squats", "Journaling").

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
in Toggl is stopped here at that time, and starts or stops that drifted by more than
a minute take Toggl's values, so tracked time and the estimates learned from it stay right.

**The feed** shows one card, the top of the stack; completing it flashes it green and slides it away, and the next
card takes its place. Above the deck, from `meeting_alert_minutes` (45) before a meeting, or all day with
`meeting_alert_all_day`, a line says "12 minutes until Daily regather", counting down in whole minutes (hours and
minutes when it is over an hour away). The card has Start (a Toggl timer tagged `task-tracker`, named after the
task in the present participle), a completion checkbox (completes the task, pushes to Linear; ticks a routine in
the sheet), and Next (not now: the card goes to the bottom of today's stack, its timer stops); at the bottom the
alternatives: today's exercise and Journal. The card does not show the task's time estimate; when the task is longer than
the gap but can be split, it says how long the gap is ("can be split: 25 min now"); a line under the deck says how many cards are in the stack, how
long until the next meeting, and how many cards are waiting for a longer gap. When nothing fits the gap the deck
says so and lists what is waiting. The agent's notes are not on the card at all, only on the Projects page. A task
card's "open in tree" link opens the Projects page zoomed into the task's own project, with the task selected and
its notes open. Once the day's session is done the exercise button offers the extra session from Settings instead
(`[exercise] after_done`, "zone 2 cardio" by default), and reads "(done)" once that has been tracked too.

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
attaches to any item is a separate signed note (never merged into your own text), a move or a priority it sets
leaves such a note on the item saying what changed and why, a schedule it sets is shown as the agent's on the plan
page and puts its tasks at the top of the feed's stack in its order, and the priorities and guidance it gives steer
the stack and any plan built on request (see *The agent's schedule orders the stack* above). Every plan the app
builds is written to the planning log for the agent to read back. Interactive documentation of
every call is at `/docs`; `GET /api/agent` returns the map.

**Access.** Optional bearer token: paste one into Settings -> Assistant agent (or `AGENT_API_TOKEN`, or the menu bar
item's *Set Agent API Token…*) and the agent sends `Authorization: Bearer <token>`. Without one, any process on this
Mac may call the agent API (the server only listens on 127.0.0.1). The agent names itself with `X-Agent-Name`
(default `assistant`); that name is what you see next to its notes.

| Call | What it does |
| --- | --- |
| `GET /api/agent/tree` | projects (with id, path, open-task count) and the full tree of open tasks, including the notes the agent added |
| `GET /api/agent/tasks?q=&project_id=` / `GET /api/agent/tasks/{id}` | find open tasks; one task with its path, tracked minutes and notes |
| `POST /api/agent/tasks/{id}/move` | `{project_id | project_path | parent_id, create_project, after_id, why}`: move a task, sub-tasks included, to another project or under another task (its project is used). Into Someday / maybe it drops its deadline, priority and blocking tag. Recorded as the agent's note on the task ("moved here from Home: …") |
| `POST /api/agent/projects/{id}/move` | `{parent_id | parent_path | top_level: true, why}`: move a project with everything in it under another project or to the top level; never under itself, never into Someday / maybe. Recorded as the agent's note on the project |
| `PATCH /api/agent/tasks/{id}` | `{importance 1-5, deadline ("2026-11-20", "fri", "" clears: the hard one), soft_deadline (ideally by, same forms), blocking, blocking_for, chunkable, work_kind ("task" / "focus" / "" back to Claude), reading, writing, why}`: the agent's priority and urgency for a task. Applied to the fields every plan is built from and recorded as the agent's note on the task (the `why` first) |
| `POST /api/agent/projects` | `{name, parent_id | parent_path, notes}`; the parent must exist |
| `POST /api/agent/tasks` | `{title, project_id | project_path | parent_id, description, deadline (hard), soft_deadline (ideally by), blocking, blocking_for, importance, chunkable, note}`; hashtags in the title work (`#p2 #due:fri`); `note` is attached as the agent's note straight away; with no project given Claude files it like the quick-add bar (Inbox without Claude); `create_project: true` creates a missing path |
| `POST /api/agent/quick_add` | `{text}`: Claude turns free text into tasks in the right projects |
| `POST /api/agent/notes` | `{task_id | project_id, body}`: add context to any item; `GET /api/agent/notes?task_id=` lists, `DELETE /api/agent/notes/{id}` takes one back |
| `GET /api/agent/schedule?date=` | what the agent needs to set a day: the current plan with statuses (and `agent_plan`, the schedule the agent last set for it), free windows between meetings, prioritised candidate tasks with calibrated estimates and the agent's notes, routines, the day's exercise, your rules document, the day's instructions and the `standing_guidance`, the day's `planning_log`, and `since_your_last_visit`: every planning event (any day) since this agent, by name, last called this |
| `GET /api/agent/schedule/log?date=&since=&limit=` | the planning log, oldest first: `date` for one day, `since` (ISO local date-time or epoch seconds) for everything after a moment. Each entry: `at_local`, `event` (`built`, `replanned`, `agent_set`, `exercise`, `instructions`, `guidance`, `task`), `project`, `actor` (`app`, `user`, `agent:<name>`), a one-line `summary` and a `detail` object (for builds: reason, source, rationale, free windows, candidates considered with their priorities, exercise, routines, the agent's schedule it adjusted and every block produced) |
| `PUT /api/agent/schedule` | `{items: [{kind, task_id, title, start, end, note}], date, work_start, work_end, rationale}`: set the day's schedule block by block (kinds `task`, `routine`, `exercise`, `journal`, `break`, `meeting`). It is stored as the day's plan (shown on the plan page) and its tasks go to the top of the feed's stack in its order; the feed keeps no times. Blocks are clipped to the free windows (`validate_windows: false` to skip that) and what did not fit is returned in `dropped`; calendar meetings are added; done, skipped and active blocks keep their status when a schedule is re-sent. The app then re-plans it on drift as usual, from this schedule as the baseline. An `exercise` block names the day's session only while none is decided; otherwise it is retitled and `exercise_note` says so |
| `PUT /api/agent/schedule/instructions` | `{text, date, standing, rebuild}`: what the planner should keep in mind, in prose. For one day ("end by 13:00", "grant report first thing"), or with `standing: true` for every day until replaced ("Eli is recovering from a cold: nothing heavy before 11"). Appended to your rules for every build and shown on the plan page; `rebuild` (default) re-plans the day with it now. `DELETE ...?date=` drops the day's, `DELETE ...?standing=true` the standing guidance |
| `POST /api/agent/schedule/rebuild` | `{date, from_now, until}`: let the app plan the day now |

```bash
A="http://127.0.0.1:8791/api/agent"; H=(-H "Content-Type: application/json" -H "X-Agent-Name: EA" -H "Authorization: Bearer $AGENT_API_TOKEN")
curl -s $A/tree "${H[@]}" | jq '.projects[] | {id, path}'
curl -s -X POST $A/tasks "${H[@]}" -d '{"title": "Book flights to Boston #p2", "project_path": ["Admin", "Travel"], "deadline": "2026-11-20", "note": "Dates confirmed with Sam on Slack"}'
curl -s -X POST $A/notes "${H[@]}" -d '{"task_id": 42, "body": "The venue replied: deposit is due Friday."}'
curl -s $A/schedule "${H[@]}" | jq '{free_windows, candidates: [.candidates[:5][] | {id, title, expected_minutes}]}'
curl -s -X PUT $A/schedule "${H[@]}" -d '{"items": [{"kind": "task", "task_id": 42, "start": "10:30", "end": "11:30"}, {"kind": "break", "title": "Break", "start": "11:30", "end": "11:40"}], "rationale": "Deposit first; rest of the day as planned."}'
curl -s -X PUT $A/schedule/instructions "${H[@]}" -d '{"text": "Eli has a dentist appointment at 14:00 and wants the afternoon light."}'
curl -s -X PUT $A/schedule/instructions "${H[@]}" -d '{"text": "The grant is the priority all week; email can wait until 16:00.", "standing": true}'
curl -s -X PATCH $A/tasks/42 "${H[@]}" -d '{"importance": 5, "deadline": "fri", "blocking_for": "Sam", "why": "Sam needs it before the board meeting."}'
curl -s -X POST $A/tasks/42/move "${H[@]}" -d '{"project_path": ["Admin", "Travel"], "why": "It is a travel item, not general admin."}'
curl -s -X POST $A/projects/7/move "${H[@]}" -d '{"parent_path": ["Palisade Projects"]}'
curl -s "$A/schedule/log?date=$(date +%F)" "${H[@]}" | jq '.[] | {at_local, event, actor, summary}'
```

## Files

| File | Purpose |
| --- | --- |
| `tracker/__main__.py` | CLI entry point: `app`, `web`, `plan`, `snapshot`, `sync-linear`, `open`, `check`, `quick-add`; sets up logging |
| `tracker/app.py` | the native macOS app: WKWebView window + menu bar item (rumps / PyObjC) over the local server |
| `tracker/web.py` | FastAPI app: the JSON API, the dashboard page, the Settings endpoints and the background loop |
| `tracker/agent_api.py` | the executive-assistant agent's API under `/api/agent` |
| `tracker/dashboard.html` | the whole UI, one file: outline, feed, stats, snapshots, settings; drafts kept in `localStorage` |
| `tracker/db.py` | SQLite schema and queries (projects, tasks, notes, plans, the planning log, timers, settings) |
| `tracker/feed.py` | the feed's stack: the gap before the next meeting, which cards fit it, the order, Next (skipped for the day) |
| `tracker/scheduler.py` | priorities and calibrated estimates (the stack uses these), free windows, the deterministic packer and the on-demand plan page's builder, schedules, instructions and guidance set from outside, the planning log entries |
| `tracker/llm.py` | the one Claude call site (`structured()`), every prompt and JSON schema; LLM-inbox wiring |
| `tracker/config.py` | `config.toml` + defaults, Keychain / env secrets, the log file, `save()` for the Settings page |
| `tracker/tags.py` | `#p2 #due:fri #soft:wed #blocking:Sam …` parsing, the inverse (`render`: a task's fields as a tag line) and `replace_fields` (the edited tag line, what it drops cleared) behind `PUT /api/tasks/{id}/tags` |
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

- The WKWebView window has no `confirm()` or `alert()` (nothing implements the WKUIDelegate panels, and a blocking
  dialog would freeze the page), so anything that needs a yes from the user has to be a toast with a button or an
  inline control, never a dialog.
- The confirm step for `contact_eli` does nothing on this app's model. It runs only for models in
  `llm_contact.CONFIRM_MODELS` (`../shared/README.md`, Gotchas: measured 2026-11-10, only `claude-haiku-4-5` is safe;
  `claude-opus-5-5` rejects the forced tool choice the question needs). Other models are never questioned and no
  extra request is made, so a `contact_eli` call here is sent exactly as before the step existed. It would start
  working if `[llm] model` were set to a listed model.
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
- An editor that closes on blur changes the layout: a title being edited makes its row about 24px taller, and a
  notes box shrinks back to text. Doing that the moment the mouse is pressed elsewhere moved everything below
  out from under the pointer before the button came up, so the click landed on the wrong thing (a click on an
  opened task's tag line opened the notes editor or another line instead, which closed the box). Blurs caused
  by a mouse press are now acted on after the button is released (`afterMouse` in `dashboard.html`). Chromium
  with instant synthetic clicks does not show this; a click held for ~90 ms in WebKit does.
