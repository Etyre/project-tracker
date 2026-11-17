"""The Mac app when the tracker's server runs on another machine (TASK_TRACKER_SERVER=http://host:8791 in the data
folder's `env` file): instead of the real server, the app runs this relay on the usual local address. Every
request is passed to the server and its answer passed back, so the window, the menu bar item and anything else
on this Mac that talks to 127.0.0.1:8791 work as before, and nothing on this Mac opens the local database.

Three things stay local because they are about this Mac, not the data:
  POST /api/window          brings the app's window forward (the launcher and `python -m tracker open` use it)
  POST /api/journal/start   is passed on (the timer starts there), then the notes app is opened here
  an unreachable server     the page gets a plain explanation that retries; API calls get a 502 with the reason
"""
from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from . import journal

log = logging.getLogger("remote")
show_window_hook = None        # set by the native app, as web.show_window_hook is when the server is local
DROP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length", "host", "upgrade",
        "proxy-authenticate", "proxy-authorization", "te", "trailers"}
AWAY = """<!doctype html><meta charset="utf-8"><title>Project Tracker</title>
<body style="font: 15px -apple-system, sans-serif; max-width: 34em; margin: 18vh auto; color: #333; line-height: 1.5">
<h2 style="font-weight: 600">The tracker's server is not reachable</h2>
<p>Your tasks live on <code>%s</code>. This Mac cannot reach it right now: it may be offline, Tailscale may be
off, or the night block (00:00 to 06:00) may be refusing the connection.</p>
<p style="color: #888">%s</p><p>Trying again every 10 seconds.</p>
<script>setTimeout(() => location.reload(), 10000)</script></body>"""


def create_relay(server: str, transport: httpx.BaseTransport | None = None) -> FastAPI:
    """`transport`: tests hand in a fake server."""
    server = server.rstrip("/")
    app = FastAPI(title="Project Tracker relay", docs_url=None, redoc_url=None, openapi_url=None)
    # trust_env=False: straight to the server, never through a system proxy
    client = httpx.Client(base_url=server, timeout=httpx.Timeout(200, connect=8), trust_env=False, transport=transport)

    @app.post("/api/window")
    def show_window(fragment: str | None = None):
        if show_window_hook is None:
            return {"ok": False, "error": "not running as the native app"}
        show_window_hook(fragment)
        return {"ok": True}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def relay(path: str, request: Request):
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP}
        try:
            r = await _run(lambda: client.request(request.method, "/" + path, params=request.query_params, content=body, headers=headers))
        except httpx.HTTPError as e:
            log.warning("%s /%s: server not reachable: %s", request.method, path, e)
            if request.method == "GET" and not path.startswith("api/"):
                return HTMLResponse(AWAY % (server, type(e).__name__), status_code=502)
            return Response('{"detail": "The tracker\'s server (%s) is not reachable from this Mac: %s"}' % (server, type(e).__name__),
                            status_code=502, media_type="application/json")
        content = r.content
        if request.method == "POST" and path == "api/journal/start" and r.status_code == 200:
            content = await _run(lambda: _open_notes(client, r))
        out = {k: v for k, v in r.headers.items() if k.lower() not in DROP}
        return Response(content, status_code=r.status_code, headers=out)

    return app


async def _run(fn):
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(fn)


def _open_notes(client: httpx.Client, r: httpx.Response) -> bytes:
    """The timer was started on the server; open the notes app here, with the [journal] settings kept there."""
    import json
    try:
        cfg = client.get("/api/settings").json()["config"]["journal"]
        out = r.json()
        out["open_error"] = journal.open_notes_app(cfg)
        return json.dumps(out).encode()
    except Exception as e:  # noqa: BLE001 - the timer did start; say why the app did not open
        log.warning("journal: could not open the notes app: %s", e)
        return r.content
