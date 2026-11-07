"""The standalone macOS app: a native window (WKWebView over the local server) plus a menu bar item
with the current card and quick actions. `python -m tracker app`, or the bundle from build_app.py."""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request

import objc
import rumps
from AppKit import (NSAlert, NSAlertFirstButtonReturn, NSApp, NSApplication, NSBackingStoreBuffered,
                    NSClosableWindowMask, NSMakeRect, NSMenu, NSMenuItem, NSMiniaturizableWindowMask,
                    NSResizableWindowMask, NSSecureTextField, NSTitledWindowMask, NSWindow, NSWorkspace)
from Foundation import NSObject, NSURL, NSURLRequest, NSUserDefaults
from PyObjCTools import AppHelper
from WebKit import WKNavigationActionPolicyAllow, WKNavigationActionPolicyCancel, WKWebView, WKWebViewConfiguration

from . import config

log = logging.getLogger("app")
APP_NAME = "Project Tracker"


class _WebDelegate(NSObject, protocols=[objc.protocolNamed("WKUIDelegate")]):
    """Links with target=_blank (every external link in the dashboard) open in the default browser."""
    base = ""

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(self, webview, cfg, action, feat):
        url = action.request().URL()          # target="_blank"
        if url is not None:
            NSWorkspace.sharedWorkspace().openURL_(url)
        return None


class MainWindow:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.window = None
        self.webview = None
        self.delegate = _WebDelegate.alloc().init()
        self.delegate.base = base_url

    def _install_menus(self):
        main = NSMenu.alloc().init()
        app_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(APP_NAME, None, "")
        app_menu = NSMenu.alloc().initWithTitle_(APP_NAME)
        app_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Hide " + APP_NAME, "hide:", "h"))
        app_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit " + APP_NAME, "terminate:", "q"))
        main.addItem_(app_item)
        main.setSubmenu_forItem_(app_menu, app_item)
        edit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
        edit = NSMenu.alloc().initWithTitle_("Edit")
        for title, sel, key in [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("Cut", "cut:", "x"),
                                ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")]:
            edit.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, key))
        main.addItem_(edit_item)
        main.setSubmenu_forItem_(edit, edit_item)
        win_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Window", None, "")
        win = NSMenu.alloc().initWithTitle_("Window")
        win.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Close", "performClose:", "w"))
        win.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Minimize", "performMiniaturize:", "m"))
        main.addItem_(win_item)
        main.setSubmenu_forItem_(win, win_item)
        NSApp.setMainMenu_(main)

    def show(self, fragment: str | None = None):
        if self.window is None:
            self._install_menus()
            rect = NSMakeRect(0, 0, 920, 760)
            style = NSTitledWindowMask | NSClosableWindowMask | NSMiniaturizableWindowMask | NSResizableWindowMask
            self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(rect, style, NSBackingStoreBuffered, False)
            self.window.setTitle_(APP_NAME)
            self.window.setReleasedWhenClosed_(False)
            self.window.setFrameAutosaveName_("TaskTrackerMain")
            self.window.setRestorable_(False)
            self.window.setCollectionBehavior_(1 << 1)      # open on the active Space
            self.window.setMinSize_((520, 420))
            if not self.window.setFrameUsingName_("TaskTrackerMain"):
                self.window.center()
            self.webview = WKWebView.alloc().initWithFrame_configuration_(rect, WKWebViewConfiguration.alloc().init())
            self.webview.setAutoresizingMask_(18)
            self.webview.setUIDelegate_(self.delegate)
            self.window.setContentView_(self.webview)
            self.webview.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(f"{self.base_url}/#{fragment or 'projects'}")))
        elif fragment:
            self.webview.evaluateJavaScript_completionHandler_(f"location.hash='{fragment}'", None)
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def is_open(self) -> bool:
        return self.window is not None and bool(self.window.isVisible())


class _AppDelegate(rumps.rumps.NSApp):
    """rumps' delegate, plus: clicking the Dock icon re-opens the window."""
    @objc.typedSelector(b"Z@:@Z")
    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_visible):
        tracker = getattr(self, "_tracker", None)
        if tracker is not None:
            AppHelper.callAfter(tracker.main_window.show)
        return True


rumps.rumps.NSApp = _AppDelegate


class ProjectTrackerApp(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, title="✓", quit_button=None)
        cfg = config.load()["general"]
        self.base_url = f"http://{cfg['host']}:{cfg['port']}"
        self.main_window = MainWindow(self.base_url)
        self._refreshing = threading.Lock()
        self.card_item = rumps.MenuItem("Starting…", callback=lambda _: self.main_window.show("feed"))
        self.timer_item = rumps.MenuItem("Start timer", callback=self.toggle_timer)
        self.complete_item = rumps.MenuItem("Complete current card", callback=self.complete_current)
        self.menu = [
            rumps.MenuItem("Open " + APP_NAME, callback=lambda _: self.main_window.show()),
            rumps.MenuItem("Quick Add…", callback=self.quick_add),
            None,
            self.card_item, self.timer_item, self.complete_item,
            rumps.MenuItem("Exercise instead", callback=lambda _: self._post("/api/exercise/start")),
            rumps.MenuItem("Journal instead", callback=lambda _: self._post("/api/journal/start")),
            None,
            rumps.MenuItem("Re-plan rest of day", callback=lambda _: self._post("/api/plan/rebuild", {"from_now": True})),
            rumps.MenuItem("Sync Linear now", callback=self.sync_linear),
            None,
            rumps.MenuItem("Set Anthropic API Key…", callback=lambda _: self._set_secret("anthropic", "Anthropic API key", "sk-ant-…")),
            rumps.MenuItem("Set Toggl API Token…", callback=lambda _: self._set_secret("toggl", "Toggl Track API token", "32 hex characters")),
            rumps.MenuItem("Set Linear API Key…", callback=lambda _: self._set_secret("linear", "Linear personal API key", "lin_api_…")),
            rumps.MenuItem("Set Sheets Token…", callback=lambda _: self._set_secret("sheets", "Apps Script SHEET_TOKEN", "")),
            rumps.MenuItem("Set Agent API Token…", callback=lambda _: self._set_secret("agent", "Token your assistant agent sends as a bearer token", "")),
            rumps.MenuItem("Open Config File", callback=lambda _: subprocess.run(["open", str(config.CONFIG_PATH)])),
            rumps.MenuItem("Open Rules & Heuristics", callback=lambda _: subprocess.run(["open", str(config.RULES_PATH)])),
            rumps.MenuItem("Open Data Folder", callback=lambda _: subprocess.run(["open", str(config.APP_SUPPORT)])),
            None,
            rumps.MenuItem("Quit " + APP_NAME, callback=lambda _: rumps.quit_application()),
        ]
        from . import web
        web.show_window_hook = lambda fragment=None: AppHelper.callAfter(self.main_window.show, fragment)
        web.notify_hook = lambda title, message: AppHelper.callAfter(rumps.notification, APP_NAME, title, message)
        threading.Thread(target=self._serve, daemon=True, name="web").start()
        rumps.Timer(self.refresh, 30).start()
        self._open_when_ready()

    # -- server -----------------------------------------------------------
    def _serve(self):
        import uvicorn
        from .web import create_app
        uvicorn.run(create_app(), host=config.load()["general"]["host"], port=config.load()["general"]["port"],
                    log_level="warning")

    def _open_when_ready(self):
        deadline = time.time() + 30

        def poll(timer):
            try:
                urllib.request.urlopen(self.base_url + "/api/status", timeout=1).close()
            except Exception:
                if time.time() < deadline:
                    return
                log.error("server did not start within 30 s")
            else:
                log.info("server up; opening window")
                try:
                    self._nsapp._tracker = self
                    self.main_window.show("projects")       # the projects page is the home page
                except Exception:
                    log.exception("opening the main window failed")
                self.refresh(None)
            timer.stop()

        rumps.Timer(poll, 0.3).start()

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base_url + path, timeout=90) as r:
            return json.load(r)

    def _post(self, path: str, body: dict | None = None, quiet: bool = False) -> dict | None:
        def run():
            req = urllib.request.Request(self.base_url + path, data=json.dumps(body or {}).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    out = json.load(r)
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")
                try:
                    msg = json.loads(msg).get("detail", msg)
                except ValueError:
                    pass
                if not quiet:
                    AppHelper.callAfter(lambda: rumps.alert(APP_NAME, str(msg)))
                return
            except Exception as e:
                if not quiet:
                    AppHelper.callAfter(lambda: rumps.alert(APP_NAME, str(e)))
                return
            self.refresh(None)
            if out and out.get("toggl_error") and not quiet:
                AppHelper.callAfter(lambda: rumps.alert("Toggl", out["toggl_error"]))
        threading.Thread(target=run, daemon=True).start()
        return None

    # -- menu actions -----------------------------------------------------
    def quick_add(self, _):
        w = rumps.Window("Describe the task; Claude files it in the right project.", "Quick add", default_text="",
                         ok="Add", cancel="Cancel", dimensions=(420, 90))
        NSApp.activateIgnoringOtherApps_(True)
        r = w.run()
        if r.clicked and r.text.strip():
            self._post("/api/quick_add", {"text": r.text.strip()})

    def toggle_timer(self, _):
        try:
            f = self._get("/api/feed")
        except Exception as e:
            rumps.alert(APP_NAME, str(e))
            return
        if f["timer"]["running"]:
            self._post("/api/timer/stop")
        elif f.get("current"):
            self._post("/api/feed/start", {"item_id": f["current"]["id"]})

    def complete_current(self, _):
        try:
            f = self._get("/api/feed")
        except Exception as e:
            rumps.alert(APP_NAME, str(e))
            return
        if f.get("current"):
            self._post("/api/feed/complete", {"item_id": f["current"]["id"]})

    def sync_linear(self, _):
        self._post("/api/linear/sync")

    def _set_secret(self, account: str, title: str, placeholder: str):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_('Stored in your login Keychain (service "TaskTracker").')
        field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 24))
        field.setPlaceholderString_(placeholder)
        alert.setAccessoryView_(field)
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        alert.window().setInitialFirstResponder_(field)
        NSApp.activateIgnoringOtherApps_(True)
        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        value = str(field.stringValue()).strip()
        if not value:
            return
        config.keychain_set(account, value)
        from . import llm, toggl
        llm._creds_ok = False
        toggl.me.cache_clear()
        rumps.notification(APP_NAME, "Saved", f"{title} stored in the Keychain.")
        self.refresh(None)

    # -- status -----------------------------------------------------------
    def refresh(self, _):
        if self._refreshing.locked():
            return
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        with self._refreshing:
            try:
                f = self._get("/api/feed")
            except Exception:
                AppHelper.callAfter(self._apply, "Server not responding", "Start timer", "✓")
                return
        c = f.get("current")
        if c:
            card = f"Now: {c['title'][:50]}  ({c['start']}–{c['end']})"
        elif not f.get("plan"):
            card = "No plan yet - open the app to build one"
        else:
            card = "All blocks done" if f.get("outside_hours") is None else "Outside work hours"
        timer = f"Stop timer ({f['timer']['label'][:30]})" if f["timer"]["running"] else "Start timer on current card"
        title = "▶" if f["timer"]["running"] else "✓"
        AppHelper.callAfter(self._apply, card, timer, title)

    def _apply(self, card: str, timer: str, title: str):
        self.card_item.title = card
        self.timer_item.title = timer
        if self.title != title:
            self.title = title


def run_app():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # No AppKit window-state restoration: after a crash it would block launch with a modal "reopen windows?"
    # alert before our run loop even starts (the web view and window are rebuilt from scratch anyway).
    NSUserDefaults.standardUserDefaults().registerDefaults_({"ApplePersistenceIgnoreState": True,
                                                              "NSQuitAlwaysKeepsWindows": False})
    app = ProjectTrackerApp()
    app._nsapp = None   # set by rumps.run; we attach ourselves in _open_when_ready
    app.run()
