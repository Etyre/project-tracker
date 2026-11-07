"""Project Tracker: a personal GTD system (project outline, Claude-powered quick add, a feed that serves one card at
a time from a day plan rebuilt from the calendar, checklist sheet, exercise log and open tasks), with Toggl, Linear
and Google Sheets integrations. Entry point: `python -m tracker [app|web|plan|snapshot|sync-linear|check|quick-add TEXT]`
(the app bundle built by build_app.py runs `-m tracker app`).

Config:  config.toml in the project folder (or $TASK_TRACKER_CONFIG), edited from the app's Settings page;
         secrets in the login Keychain, service "TaskTracker" (accounts anthropic, toggl, linear, sheets, agent),
         or ~/Library/Application Support/TaskTracker/env.
Data:    ~/Library/Application Support/TaskTracker/ (tracker.sqlite3, snapshots/).
Logs:    ~/Library/Logs/project-tracker.log (the launcher's own log is launcher.log in the data folder).
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser

from . import config


def main(argv=None) -> int:
    config.setup_logging()
    ap = argparse.ArgumentParser(prog="tracker")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("app", help="the standalone app: native window + menu bar item (default)")
    w = sub.add_parser("web", help="headless: serve the dashboard for a browser")
    w.add_argument("--no-open", action="store_true")
    w.add_argument("--reload", action="store_true")
    p = sub.add_parser("plan", help="build today's plan and print it")
    p.add_argument("--from-now", action="store_true")
    sub.add_parser("sync-linear", help="pull Linear issues now")
    sub.add_parser("snapshot", help="write today's Markdown snapshot of the outline now")
    sub.add_parser("open", help="bring the running app's window to the front (starts the app if needed)")
    sub.add_parser("check", help="show which integrations are configured and reachable")
    q = sub.add_parser("quick-add", help="file a task via Claude")
    q.add_argument("text", nargs="+")
    a = ap.parse_args(argv)
    cmd = a.cmd or "app"

    if cmd == "app":
        from .app import run_app
        run_app()
        return 0
    if cmd == "web":
        import uvicorn
        cfg = config.load()["general"]
        url = f"http://{cfg['host']}:{cfg['port']}"
        if not a.no_open:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        print(f"Project Tracker at {url}")
        uvicorn.run("tracker.web:create_app", factory=True, host=cfg["host"], port=cfg["port"],
                    reload=a.reload, log_level="info")
        return 0
    if cmd == "plan":
        from . import scheduler
        plan = scheduler.build(from_now=a.from_now)
        print(plan["rationale"], "\n")
        for it in plan["items"]:
            print(f"{it['start']}-{it['end']}  {it['kind']:9s} {it['title']}  {('(' + it['note'] + ')') if it['note'] else ''}")
        print("\nExercise today:", plan["exercise"])
        return 0
    if cmd == "open":
        import urllib.request
        cfg = config.load()["general"]
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://{cfg['host']}:{cfg['port']}/api/window", method="POST"), timeout=5).close()
            print("window shown")
        except Exception:
            import subprocess
            subprocess.run(["open", "-a", "Project Tracker"])
            print("app launched")
        return 0
    if cmd == "snapshot":
        from . import snapshot
        print(snapshot.write())
        return 0
    if cmd == "sync-linear":
        from . import linear
        print(json.dumps(linear.pull(), indent=1))
        return 0
    if cmd == "quick-add":
        from . import db, llm, scheduler
        text = " ".join(a.text)
        res = llm.quick_add(text, scheduler.tree_text(), db.today())
        print(json.dumps(res, indent=1))
        return 0
    if cmd == "check":
        from . import calendar_feed, linear, llm, sheets, toggl
        import datetime as dt
        print("config:", config.CONFIG_PATH, "(exists)" if config.CONFIG_PATH.exists() else "(missing - using defaults)")
        print("db:", config.DB_PATH)
        print("Claude:", "ok (key found)" if llm.enabled() else llm.paused() or "not configured (ANTHROPIC_API_KEY)")
        if toggl.enabled():
            try:
                me = toggl.me()
                print(f"Toggl: ok ({me.get('fullname')}, workspace {me.get('default_workspace_id')})")
            except toggl.TogglError as e:
                print("Toggl: ERROR", e)
        else:
            print("Toggl: not configured (TOGGL_API_TOKEN)")
        if linear.enabled():
            try:
                print("Linear: ok,", len(linear.fetch_my_issues()), "issues assigned to you")
            except linear.LinearError as e:
                print("Linear: ERROR", e)
        else:
            print("Linear: not configured (LINEAR_API_KEY + [linear] enable = true)")
        evs = calendar_feed.events_for(dt.date.today())
        print(f"Calendar: {len(config.load()['calendar']['ics_urls'])} feed(s), {len(evs)} event(s) today")
        cl = sheets.daily_checklist()
        print(f"Checklist sheet: source={cl['source']} items={len(cl['items'])} writable={sheets.writable()}")
        print("Exercise sheet:", "ok" if sheets.exercise_sheet() else "empty / not configured")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
