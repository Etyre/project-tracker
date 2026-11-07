"""Claude calls: structured JSON for quick-add placement and day scheduling."""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

import anthropic

from . import config

log = logging.getLogger("llm")


class LLMError(RuntimeError):
    pass


_creds_ok = False


def _has_credentials() -> bool:
    """Only a positive answer is remembered: a check that fails (e.g. the Keychain not answering in the first
    second after launch) is tried again next time."""
    global _creds_ok
    if _creds_ok:
        return True
    ok = bool(config.secret("anthropic") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if not ok:
        config.secret.cache_clear()
        try:                      # an `ant auth login` profile - the SDK's zero-arg client resolves it
            c = anthropic.Anthropic()
            ok = bool(c.api_key or getattr(c, "auth_token", None) or getattr(c, "credentials", None))
        except Exception:
            ok = False
    _creds_ok = ok
    return ok


def enabled() -> bool:
    return bool(config.load()["llm"]["enable"] and _has_credentials())


@lru_cache(maxsize=1)
def client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def structured(system: str, user: str, schema: dict, effort: str, max_tokens: int = 16000) -> dict:
    """One call, JSON-schema constrained output. The system prompt (rules doc etc.) is cached as a prefix."""
    cfg = config.load()["llm"]
    kw = dict(
        model=cfg["model"],
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}, "effort": effort},
    )
    try:
        try:
            # Server-side refusal fallback: if the safeguards decline, the same request re-runs on a fallback model.
            resp = client().beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
        except anthropic.BadRequestError as e:
            if "fallback" not in str(e).lower():
                raise
            resp = client().messages.create(**kw)
    except anthropic.AuthenticationError as e:
        raise LLMError("Anthropic API key rejected") from e
    except anthropic.RateLimitError as e:
        raise LLMError("Anthropic rate limit; try again in a minute") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Anthropic unreachable: {e}") from e
    if resp.stop_reason == "refusal":
        det = getattr(resp, "stop_details", None)
        raise LLMError(f"model declined ({getattr(det, 'category', None)})")
    if resp.stop_reason == "max_tokens":
        raise LLMError("model output truncated (max_tokens)")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise LLMError("no text in response")
    log.info("llm %s in=%s out=%s cached=%s", cfg["model"], resp.usage.input_tokens, resp.usage.output_tokens,
             getattr(resp.usage, "cache_read_input_tokens", 0))
    return json.loads(text)


# ------------------------------------------------------------------ quick add
QUICK_ADD_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "project_path": {"type": "array", "items": {"type": "string"},
                                     "description": "Path of project names from the top level down. Reuse existing names exactly; a new trailing name creates a sub-project."},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "deadline": {"type": ["string", "null"], "description": "YYYY-MM-DD or null"},
                    "blocking": {"type": "boolean"},
                    "blocking_for": {"type": "string"},
                    "importance": {"type": ["integer", "null"]},
                    "estimate_minutes": {"type": ["integer", "null"]},
                    "chunkable": {"type": "boolean"},
                    "work_kind": {"type": "string", "enum": ["task", "focus"]},
                    "reading": {"type": "boolean"},
                    "writing": {"type": "boolean"},
                },
                "required": ["project_path", "title", "description", "deadline", "blocking", "blocking_for",
                             "importance", "estimate_minutes", "chunkable", "work_kind", "reading", "writing"],
                "additionalProperties": False,
            },
        },
        "note": {"type": "string", "description": "One short sentence on where/why you filed it, for the user."},
    },
    "required": ["tasks", "note"],
    "additionalProperties": False,
}

QUICK_ADD_SYSTEM = """You file tasks into a personal GTD project hierarchy.

Given the user's existing project tree (with a few open tasks per project for flavour) and a free-text note,
produce one or more tasks, each placed in the best-fitting existing project. Prefer existing projects; create a
new sub-project only when the note clearly concerns a distinct project that has no home yet. Never invent a new
top-level project: the first name of every project_path must be one of the top-level names in the tree, copied
exactly, and the path must list every level down to the project (parent names included, spelled as the tree
spells them). Only the last name may be new, and only for a genuinely new sub-project.

Field guidance:
- title: short imperative phrase, as the user would write it. Keep their wording where it is already a title.
- description: context, links, names, anything in the note that is not the title. Empty string if nothing.
- deadline: only if the note states or strongly implies a date ("by Friday", "before the 3rd"). Resolve relative dates
  against today's date given below. Otherwise null.
- blocking: true only if someone else is waiting on this ("X needs this", "send to Y so they can start").
- importance: null unless the note signals it (default: no particular priority). 1 trivial, 2 low, 3 normal,
  4 high, 5 critical. Words like "urgent", "important", "ASAP" and the presence of a blocked person move it up.
- estimate_minutes: your honest guess; null if you really cannot tell.
- chunkable: false only for things that must happen in one sitting (a call, a deploy, a review that must be coherent).
- work_kind: "focus" for work that needs a stretch of real concentration (writing, analysis, design, coding, careful
  reading, preparing something substantial); "task" for anything done in minutes or between other things (a message,
  a call, an errand, admin, a quick check, buying something). When unsure, "task" for short things, "focus" for long.
- reading: true when the task is mainly reading something (a paper, a report, a doc, a book, a thread) rather than
  writing, deciding or doing; the user's daily reading slot offers these.
- writing: true when the task is mainly writing (a draft, a memo, a post, a doc, a long reply); false for a quick
  message, and for reading, deciding or doing.
Split the note into several tasks only when it plainly lists separate actions."""

SCHEDULE_SYSTEM_PREFIX = """You build a realistic, prioritised plan for the user's work day.

You receive: the work-day window, free windows (after calendar meetings), candidate tasks with priority inputs and
calibrated time estimates, the user's daily routines (from their checklist), their exercise brief (their own instructions on what
exercise to do, how long and when), recent exercise history, and their own rules and heuristics document. Produce an ordered list of blocks that exactly fit inside the free
windows (meetings are added by the system; do not emit them).

Hard constraints:
1. Every block lies inside one free window; blocks never overlap; times are HH:MM in the user's local time.
2. Prioritise (a) tasks that block other people, (b) deadlines at risk, (c) high importance. Then age.
3. Everything must fit: never schedule more than the time available. Leave the rest for another day.
4. A task marked not-chunkable needs a single block at least as long as its estimate; chunkable tasks may be split
   or given a partial block (say so in the note).
5. `routines_today` are daily routines from the user's checklist that are not yet done: give EACH its own "routine"
   block, titled exactly with its title and about its minutes long; they are short and should all be placed.
6. Today's exercise was decided this morning by the user's own rules and is given as `exercise_today` (kind, title,
   minutes, reason): schedule exactly one "exercise" block of that length, titled with the title, at the time of day
   the EXERCISE NOTES below prefer. No block only when exercise is already done today; every day has a session.
7. Short "break" blocks are welcome between long focus blocks, but keep them few.
   Each task carries `work_kind`: "focus" (focused work) wants a long uninterrupted block at the user's best hours and should
   not be chopped into small pieces; "task" items fit short gaps, the slots before meetings and low-energy times.
8. Follow the user's rules and heuristics below; they override these defaults where they conflict.

Output JSON only, matching the schema. `rationale` is 2-4 sentences the user will read on the plan page.

===== USER'S RULES AND HEURISTICS =====
"""
EXERCISE_HEADER = "\n\n===== USER'S EXERCISE NOTES (rules are applied by the app; read these for timing) =====\n"

SCHEDULE_SCHEMA = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["task", "routine", "exercise", "journal", "break"]},
                    "task_id": {"type": ["integer", "null"]},
                    "title": {"type": "string"},
                    "start": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"},
                    "end": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"},
                    "note": {"type": "string"},
                },
                "required": ["kind", "task_id", "title", "start", "end", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["rationale", "items"],
    "additionalProperties": False,
}


WORK_KIND_GUIDE = """work_kind is "focus" for work that needs a stretch of real concentration (writing, analysis, design,
coding, careful reading, preparing something substantial) and "task" for anything done in minutes or between other
things (a message, a call, an errand, admin, a quick check, buying something). When unsure: "task" for short things,
"focus" for long ones. reading is true when the task is mainly reading something (a paper, a report, a doc, a book,
a thread) rather than writing, deciding or doing. writing is true when the task is mainly writing (a draft, a memo, a
post, a doc, a long reply), not for a quick message."""
ESTIMATE_SCHEMA = {
    "type": "object",
    "properties": {"estimate_minutes": {"type": "integer"}, "work_kind": {"type": "string", "enum": ["task", "focus"]},
                   "reading": {"type": "boolean"}, "writing": {"type": "boolean"}},
    "required": ["estimate_minutes", "work_kind", "reading", "writing"],
    "additionalProperties": False,
}
ESTIMATE_SYSTEM = """You forecast how many minutes of focused work a personal task will take, whether it is a quick
task or focused work, and whether it is reading or writing. Tasks are short imperative phrases from a GTD list; the project path gives context. Recent completed
tasks with the time they actually took are provided when available: calibrate to them. estimate_minutes is a single
realistic number of minutes (round to 5), not a best case. """ + WORK_KIND_GUIDE


def forecast_estimate(title: str, path: str, description: str, examples: list[dict]) -> dict:
    """Learned/forecast time estimate, and the kind of work, for a task the user typed without them:
    {estimate_minutes, work_kind}."""
    ex = "\n".join(f"- {e['title']}: {round(e['actual'])} min (forecast was {e['est']})" for e in examples[:12])
    user = (f"## Task\n{title}\nProject: {path or '(none)'}\n{('Notes: ' + description) if description else ''}\n\n"
            f"## Recent completed tasks\n{ex or '(none yet)'}")
    res = structured(ESTIMATE_SYSTEM, user, ESTIMATE_SCHEMA, "low", max_tokens=2000)
    return {"estimate_minutes": max(5, min(600, int(res["estimate_minutes"]))),
            "work_kind": res.get("work_kind") if res.get("work_kind") in ("task", "focus") else None,
            "reading": None if res.get("reading") is None else int(bool(res["reading"])),
            "writing": None if res.get("writing") is None else int(bool(res["writing"]))}


WORK_KINDS_SCHEMA = {
    "type": "object",
    "properties": {"kinds": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "work_kind": {"type": "string", "enum": ["task", "focus"]}, "reading": {"type": "boolean"},
                       "writing": {"type": "boolean"}},
        "required": ["id", "work_kind", "reading", "writing"], "additionalProperties": False}}},
    "required": ["kinds"],
    "additionalProperties": False,
}
WORK_KINDS_SYSTEM = """For each task in the user's list, judge whether it is a quick task or focused work, and whether it is
mainly reading, and whether it is mainly writing; answer one entry per task id. """ + WORK_KIND_GUIDE


def classify_work_kinds(tasks: list[dict]) -> dict[int, dict]:
    """{task id: {work_kind: "task" | "focus", reading: 0 | 1, writing: 0 | 1}} for tasks the user never marked (a
    batch, so the backlog is judged in one call)."""
    lines = [f"- id {t['id']}: {t['title']} (in {t.get('path') or 'no project'})" + (f" - {t['description'][:200]}" if t.get("description") else "") for t in tasks]
    res = structured(WORK_KINDS_SYSTEM, "## Tasks\n" + "\n".join(lines), WORK_KINDS_SCHEMA, "low", max_tokens=4000)
    return {int(k["id"]): {"work_kind": k["work_kind"], "reading": int(bool(k.get("reading"))), "writing": int(bool(k.get("writing")))}
            for k in res.get("kinds") or [] if k.get("work_kind") in ("task", "focus")}


def quick_add(text: str, tree_text: str, today: str) -> dict:
    user = f"Today is {today}.\n\n## Project tree\n{tree_text}\n\n## Note from the user\n{text}"
    return structured(QUICK_ADD_SYSTEM, user, QUICK_ADD_SCHEMA, config.load()["llm"]["quick_add_effort"])


EXERCISE_REVIEW_SYSTEM = """The user's exercise for today was chosen by their own rules (cadences, gaps, never-back-to-back pairs)
applied to their exercise log. You review that choice. Keep it unless the user's notes below, or something plain in the
recent log, clearly call for a different session today (an injury or soreness they mention, travel, a session they
asked for explicitly, a rule the notes state that the app's rule lines cannot express). Overriding should be rare; when
you do, name a session in their own vocabulary and give one short reason they will read. `keep` true means no change."""
EXERCISE_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"keep": {"type": "boolean"}, "title": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["keep", "title", "reason"],
    "additionalProperties": False,
}


def review_exercise(pick: dict, notes: str, context_text: str) -> dict:
    user = (f"## The rules' choice for today\n{pick['title']}: {pick['reason']}\n\n## The user's notes\n{notes.strip() or '(none)'}\n\n"
            f"## Log and context\n{context_text}")
    return structured(EXERCISE_REVIEW_SYSTEM, user, EXERCISE_REVIEW_SCHEMA, "low", max_tokens=1000)


def schedule_system(rules: str, exercise_brief: str = "") -> str:
    return (SCHEDULE_SYSTEM_PREFIX + (rules.strip() or "(no rules document yet)") + EXERCISE_HEADER
            + (exercise_brief.strip() or "(none)"))


def schedule(context_text: str, rules: str, exercise_brief: str = "") -> dict:
    return structured(schedule_system(rules, exercise_brief), context_text, SCHEDULE_SCHEMA, config.load()["llm"]["effort"], max_tokens=16000)


# ------------------------------------------------------------------ toggl projects
TOGGL_PROJECT_SYSTEM = """You file the user's time tracking into their Toggl Track projects.

You get their Toggl projects (each with its client and the descriptions of recent entries filed under it), their
clients, their tags (most used first), and a subject: a project from their task list (its path from the top level down, notes and a few open
tasks) or a recurring routine (a checklist row, an exercise session, journaling). Decide which Toggl project the
subject's time belongs to.

- Prefer an existing project strongly; the example descriptions show how the user actually sorts their time.
  Read the names as they do: a client or employer, a side project, "Personal", "Admin", "Exercise" and the like.
- Set `create` true, with a new `project` name, only when the subject is plainly a distinct line of work that no
  existing project covers. Name it the way the user names theirs (same style, short). Give it a `client` from the
  existing clients when one fits, a new client only when the subject is clearly for a new employer or customer,
  and null when the user's projects mostly have no client.
- `tags`: for a routine, the one to three existing tags that plainly fit it (read the tag list as the user's own
  habit: a tag for exercise, for learning, for admin and so on); a new tag only when the routine clearly needs one
  the list lacks. Never a tag that only says the item is recurring ("routine", "daily"): every routine here is one.
  For a project from the task list, leave `tags` empty: those entries are tagged by the app.
- `reason`: one short sentence the user will read."""
TOGGL_PROJECT_SCHEMA = {
    "type": "object",
    "properties": {"project": {"type": "string"}, "client": {"type": ["string", "null"]}, "create": {"type": "boolean"},
                   "tags": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}},
    "required": ["project", "client", "create", "tags", "reason"],
    "additionalProperties": False,
}


def _toggl_context(projects: list[dict], clients: list[str], tags: list[str]) -> str:
    lines = []
    for p in projects:
        ex = "; ".join(p.get("examples") or [])
        lines.append(f"- {p['name']}" + (f" (client: {p['client']})" if p.get("client") else "") + (" [archived]" if not p.get("active", True) else "")
                     + (f": {ex}" if ex else ": (no recent entries)"))
    return (f"## Toggl projects\n{chr(10).join(lines) or '(none yet)'}\n\n## Clients\n{', '.join(clients) or '(none)'}"
            f"\n\n## Tags\n{', '.join(tags) or '(none)'}")


def pick_toggl_project(subject: str, context: str, projects: list[dict], clients: list[str], tags: list[str]) -> dict:
    user = f"{_toggl_context(projects, clients, tags)}\n\n## Subject\n{subject}\n{context}".rstrip()
    return structured(TOGGL_PROJECT_SYSTEM, user, TOGGL_PROJECT_SCHEMA, "low", max_tokens=1500)


ROUTINE_MAP_SYSTEM = TOGGL_PROJECT_SYSTEM.replace("and a subject:", "and the user's recurring routines:").replace(
    "subject's time belongs to.", "each routine's time belongs to: one mapping per routine, label copied exactly.") + """
`note`: one or two sentences on how you sorted them, for the user, who will review and can edit every line."""
ROUTINE_MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "mappings": {"type": "array", "items": {
            "type": "object",
            "properties": {"label": {"type": "string"}, "project": {"type": "string"}, "client": {"type": ["string", "null"]},
                           "create": {"type": "boolean"}, "tags": {"type": "array", "items": {"type": "string"}}},
            "required": ["label", "project", "client", "create", "tags"], "additionalProperties": False}},
        "note": {"type": "string"},
    },
    "required": ["mappings", "note"],
    "additionalProperties": False,
}


def map_routines(labels: list[str], projects: list[dict], clients: list[str], tags: list[str]) -> dict:
    user = f"{_toggl_context(projects, clients, tags)}\n\n## Routines\n" + "\n".join(f"- {l}" for l in labels)
    return structured(ROUTINE_MAP_SYSTEM, user, ROUTINE_MAP_SCHEMA, "medium", max_tokens=4000)
