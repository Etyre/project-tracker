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
                },
                "required": ["project_path", "title", "description", "deadline", "blocking", "blocking_for",
                             "importance", "estimate_minutes", "chunkable"],
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
top-level project unless nothing fits at all.

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
Split the note into several tasks only when it plainly lists separate actions."""

SCHEDULE_SYSTEM_PREFIX = """You build a realistic, prioritised plan for the user's work day.

You receive: the work-day window, free windows (after calendar meetings), candidate tasks with priority inputs and
calibrated time estimates, the user's daily checklist sheet, their exercise brief (their own instructions on what
exercise to do, how long and when), recent exercise history, and their own rules and heuristics document. Produce an ordered list of blocks that exactly fit inside the free
windows (meetings are added by the system; do not emit them).

Hard constraints:
1. Every block lies inside one free window; blocks never overlap; times are HH:MM in the user's local time.
2. Prioritise (a) tasks that block other people, (b) deadlines at risk, (c) high importance. Then age.
3. Everything must fit: never schedule more than the time available. Leave the rest for another day.
4. A task marked not-chunkable needs a single block at least as long as its estimate; chunkable tasks may be split
   or given a partial block (say so in the note).
5. Checklist items from the sheet: one "checklist" block (or a few) sized from the sheet's minutes if present,
   otherwise ~30 min; skip items already done today. Use the item text as the title list in the note.
6. Today's exercise was decided this morning by the user's own rules and is given as `exercise_today` (kind, title,
   minutes, reason): schedule exactly one "exercise" block of that length, titled with the title, at the time of day
   the EXERCISE NOTES below prefer. No block only when exercise is already done today; every day has a session.
7. Short "break" blocks are welcome between long focus blocks, but keep them few.
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
                    "kind": {"type": "string", "enum": ["task", "checklist", "exercise", "journal", "break"]},
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


ESTIMATE_SCHEMA = {
    "type": "object",
    "properties": {"estimate_minutes": {"type": "integer"}},
    "required": ["estimate_minutes"],
    "additionalProperties": False,
}
ESTIMATE_SYSTEM = """You forecast how many minutes of focused work a personal task will take. Tasks are short
imperative phrases from a GTD list; the project path gives context. Recent completed tasks with the time they
actually took are provided when available: calibrate to them. Answer with a single realistic number of minutes
(round to 5), not a best case."""


def forecast_estimate(title: str, path: str, description: str, examples: list[dict]) -> int:
    """Learned/forecast time estimate for a task the user typed without one."""
    ex = "\n".join(f"- {e['title']}: {round(e['actual'])} min (forecast was {e['est']})" for e in examples[:12])
    user = (f"## Task\n{title}\nProject: {path or '(none)'}\n{('Notes: ' + description) if description else ''}\n\n"
            f"## Recent completed tasks\n{ex or '(none yet)'}")
    return max(5, min(600, int(structured(ESTIMATE_SYSTEM, user, ESTIMATE_SCHEMA, "low", max_tokens=2000)["estimate_minutes"])))


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
