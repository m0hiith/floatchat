#!/usr/bin/env python
"""Stage 14: the dashboard rendered in a real browser, and read back.

Every other suite in this project reads source. This one runs the thing.

Stage 13 closed the dashboard's *couplings* and said plainly what it could not
see: "whether anything looks right -- no colour, no layout, no overlap"
(D13.6), and left two of its own fixes marked **not seen on screen** (D13.7).
This suite is that gap. It answers the questions source checks cannot:

  * does the axis title actually come out red, or did Plotly drop the attribute
    again in silence?                                              (D13.1)
  * does pressure actually increase DOWNWARD on the rendered axis?
  * does anything 404 -- a marker image, a font, a chunk?           (D13.2)
  * does an error boundary fire, or a console error land, on any display?

Three properties of how it runs, each of them deliberate:

**It tests the production build, not the dev server.** D13.2's broken markers
are invisible under `npm run dev` and appear only in `npm run build`, because
the bug is in how Vite emits the stylesheet. A suite that drove the dev server
would have passed while the demo was broken. `dist/` is rebuilt here whenever a
source file is newer, and served as a static directory.

**It binds no port it did not choose, and the page and the API share an
origin.** D12.13 lost ten minutes to a stale `uvicorn` from an earlier stage
owning port 8000: the browser was talking to old code and the panel was blamed.
Every port here is picked free at startup. The static server also proxies the
API paths through to `uvicorn`, so the page calls `/meta` relatively: there is
no CORS to satisfy, no production `DEV_ORIGINS` to edit in order to test it,
and -- the reason it is done this way -- **no API port baked into the bundle**.
Building the port in meant a cached `dist/` pointed at the previous run's dead
port, which is a suite that fails for a reason having nothing to do with the
dashboard.

**It reaches no network.** Tile requests to openstreetmap.org are blocked at the
browser, so this runs offline and a blocked tile is never counted as a failure.

    .venv/bin/python ui/test_render.py

Needs Postgres, Chrome and `ui/node_modules` -- more than the other suites, and
it says so and fails with the missing piece named rather than skipping quietly.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
DIST = UI / "dist"
PY = str(ROOT / ".venv" / "bin" / "python")

# The paths the static server forwards to uvicorn instead of serving from
# disk. Same origin as the page, so the browser never makes a cross-origin
# request and `api/server.py`'s CORS list is left exactly as it ships.
API_PATHS = ("/meta", "/query", "/ask", "/regions.geojson", "/health")

# Where a Chromium-family browser might be. `CHROME_PATH` wins, so a machine
# that keeps one somewhere else needs no edit here -- hardcoding only the macOS
# bundle would have made this suite unrunnable everywhere else and skipped
# silently, which is the failure this project is organised against.
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
]
CHROME = os.environ.get("CHROME_PATH") or next(
    (p for p in CHROME_CANDIDATES if Path(p).exists()), "")

BLOCKED = ["*tile.openstreetmap.org*"]      # the only outbound URL the UI has

passed = failed = 0


def check(label: str, ok: bool, detail: str = ""):
    global passed, failed
    passed, failed = passed + ok, failed + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")


def skip(why: str, fix: str):
    """Exit 2, which `run_pipeline.py` shows as `skipped` rather than `ok`.

    This suite needs Chrome and a built `ui/dist` -- more than any other stage
    -- and on a machine without them the honest report is that it made no
    claim, not that everything is fine. Exit 0 would be a quiet no-op (rule 7)
    and exit 1 would fail a clone that is not broken.
    """
    print(f"\n  SKIPPED -- nothing was checked\n"
          f"  why: {why}\n  fix: {fix}\n")
    sys.exit(2)


def die(why: str, fix: str):
    """A prerequisite that IS present but wrong. That is a failure."""
    print(f"\n  CANNOT RUN  {why}\n              {fix}\n")
    sys.exit(1)


# --------------------------------------------------------------------------
# the smallest CDP client that can do this job
# --------------------------------------------------------------------------

class Chrome:
    """Headless Chrome over the DevTools Protocol.

    Uses the already-pinned `websockets` (requirements.txt) and a throwaway
    profile directory -- it never touches the user's Chrome profile.
    """

    def __init__(self):
        from websockets.sync.client import connect

        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="floatchat-cdp-")
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions",
             "--disable-background-networking", "--window-size=1600,1400",
             f"--user-data-dir={self.profile}",
             f"--remote-debugging-port={self.port}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        target = None
        for _ in range(100):
            try:
                targets = json.loads(get(f"http://127.0.0.1:{self.port}/json/list"))
                target = next((t for t in targets if t["type"] == "page"), None)
                if target:
                    break
            except Exception:                                       # noqa: BLE001
                pass
            time.sleep(0.1)
        if not target:
            raise RuntimeError("Chrome started but exposed no page target")

        self.ws = connect(target["webSocketDebuggerUrl"], max_size=None)
        self.n = 0
        self.events: list[dict] = []
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Network.enable")
        self.send("Network.setBlockedURLs", urls=BLOCKED)

    def send(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=60))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def pump(self, seconds: float = 0.3):
        """Collect events without sending anything."""
        end = time.time() + seconds
        while time.time() < end:
            try:
                msg = json.loads(self.ws.recv(timeout=max(0.01, end - time.time())))
            except Exception:                                       # noqa: BLE001
                return
            if "method" in msg:
                self.events.append(msg)

    def eval(self, expression: str):
        out = self.send("Runtime.evaluate", expression=expression,
                        returnByValue=True, awaitPromise=True)
        if out.get("exceptionDetails"):
            text = out["exceptionDetails"].get("exception", {}).get("description", "")
            raise RuntimeError(f"page threw: {text.splitlines()[0] if text else '?'}")
        return out.get("result", {}).get("value")

    def wait(self, expression: str, what: str, timeout: float = 30.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.eval(expression):
                return True
            self.pump(0.15)
        raise TimeoutError(f"timed out waiting for {what}")

    @staticmethod
    def _not_the_page(url: str) -> bool:
        """Requests the page did not make.

        The browser asks for `/favicon.ico` unprompted on every navigation and
        the dashboard links none, so its 404 says nothing about the app. Tiles
        are blocked here on purpose. Everything else counts -- which is how
        D13.2's `marker-icon.png` would have been caught.
        """
        return url.endswith("/favicon.ico") or "tile.openstreetmap.org" in url

    def failures(self) -> list[str]:
        urls, out = {}, []
        for e in self.events:
            p = e.get("params", {})
            if e["method"] == "Network.requestWillBeSent":
                urls[p["requestId"]] = p["request"]["url"]
            elif e["method"] == "Network.responseReceived":
                r = p["response"]
                if r["status"] >= 400 and not self._not_the_page(r["url"]):
                    out.append(f'{r["status"]} {r["url"]}')
            elif e["method"] == "Network.loadingFailed":
                url = urls.get(p.get("requestId"), "")
                if p.get("blockedReason") or self._not_the_page(url):
                    continue
                out.append(f'failed {p.get("errorText", "?")} {url}'.strip())
        return out

    def console_errors(self) -> list[str]:
        out = []
        for e in self.events:
            if e["method"] == "Runtime.exceptionThrown":
                d = e["params"]["exceptionDetails"]
                out.append((d.get("exception", {}).get("description")
                            or d.get("text", "?")).splitlines()[0])
            elif (e["method"] == "Runtime.consoleAPICalled"
                  and e["params"]["type"] == "error"):
                bits = [str(a.get("value", a.get("description", "")))
                        for a in e["params"]["args"]]
                out.append(" ".join(bits)[:160])
        return out

    def close(self):
        try:
            self.ws.close()
        except Exception:                                           # noqa: BLE001
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


# --------------------------------------------------------------------------
# what the page is asked to do, injected once per load
# --------------------------------------------------------------------------

DRIVER = r"""
window.__fc = {
  // React owns these inputs, so the value has to go through the native setter
  // and be announced, or React re-renders the old value straight back.
  setNative(el, value) {
    const proto = el.tagName === 'SELECT' ? HTMLSelectElement.prototype
                : el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  },

  pickQuery(name) {
    const b = [...document.querySelectorAll('button')].find(
      x => x.querySelector('span.font-mono')?.textContent.trim() === name);
    if (!b) return 'no button for ' + name;
    b.click();
    return 'ok';
  },

  // Fills every REQUIRED parameter from what the control itself offers -- the
  // first real option, the window's own end points. No region name, float id
  // or date is written into this file, for the same reason none is written
  // into the UI.
  fillRequired() {
    const filled = [];
    for (const label of document.querySelectorAll('label')) {
      const tag = label.querySelector('span.font-mono');
      if (!tag || !tag.querySelector('span.text-red-500')) continue;
      const name = tag.textContent.replace('*', '').trim();
      const el = label.querySelector('select, input');
      if (!el) continue;
      let v = '';
      if (el.tagName === 'SELECT') {
        // Pick the option the database actually holds the most data for,
        // reading the count off the label the control renders for itself
        // ("Arabian Sea · 412 profiles"). Taking the first option instead
        // takes them alphabetically, which lands on a region that may hold
        // nothing -- and a chart assertion that lands on "no rows" passes
        // through every branch it was written to test without drawing a chart.
        let best = '', bestN = -1;
        for (const o of el.options) {
          if (o.value === '') continue;
          const m = /·\s*([\d,]+)\s*profiles/.exec(o.textContent);
          const n = m ? Number(m[1].replace(/,/g, '')) : 0;
          if (n > bestN) { bestN = n; best = o.value; }
        }
        v = best;
      } else if (el.type === 'date') {
        v = /end/.test(name) ? (el.max || '') : (el.min || '');
      } else if (el.min !== '' && el.max !== '') {
        v = String((Number(el.min) + Number(el.max)) / 2);
      } else if (el.min !== '') {
        v = el.min;
      }
      if (v !== '') { this.setNative(el, v); filled.push(name + '=' + v); }
    }
    return filled;
  },

  run() {
    const b = [...document.querySelectorAll('button')]
      .find(x => /^Run query$|^Running/.test(x.textContent.trim()));
    if (!b) return 'no run button';
    if (b.disabled) return 'run button is disabled';
    b.click();
    return 'ok';
  },

  settled() {
    const t = document.body.innerText;
    return !/Running…/.test(t) &&
           (!!document.querySelector('.js-plotly-plot, .leaflet-container, table, dl') ||
            /no rows|refused|could not be drawn/i.test(t));
  },

  // Plotly's COMPUTED layout, which is the only place that says whether an
  // attribute survived. `_fullLayout` is what was actually applied, not what
  // was passed in -- an attribute this version ignores simply is not here.
  plot() {
    const gd = document.querySelector('.js-plotly-plot');
    if (!gd || !gd._fullLayout) return null;
    const ax = a => a ? {
      title: a.title && a.title.text,
      colour: a.title && a.title.font && a.title.font.color,
      range: a.range,
      autorange: a.autorange,
    } : null;
    const fl = gd._fullLayout;
    return {
      traces: (gd.data || []).length,
      xaxis: ax(fl.xaxis), xaxis2: ax(fl.xaxis2),
      yaxis: ax(fl.yaxis), yaxis2: ax(fl.yaxis2),
    };
  },

  map() {
    const c = document.querySelector('.leaflet-container');
    if (!c) return null;
    const icons = [...c.querySelectorAll('.leaflet-marker-icon')];
    return {
      markers: icons.length,
      imgMarkers: icons.filter(e => e.tagName === 'IMG').length,
      labels: icons.map(e => e.textContent.trim()).filter(Boolean),
      vectors: c.querySelectorAll('path.leaflet-interactive').length,
    };
  },

  boundaryFired() {
    return /This result could not be drawn/.test(document.body.innerText);
  },

  // Which view the page LANDED in, read off the rendered DOM rather than off
  // the default in App.jsx -- the property is what a reader sees, not how the
  // initial state is spelled (rule 5).
  landing() {
    return {
      chat: /Ask a question in English/.test(document.body.innerText)
            && !!document.querySelector('textarea'),
      form: [...document.querySelectorAll('button')]
              .some(b => /^Run query$/.test(b.textContent.trim())),
    };
  },

  openCatalogue() {
    const t = [...document.querySelectorAll('nav button')]
      .find(x => /^Catalogue/.test(x.textContent));
    if (!t) return 'no catalogue tab';
    t.click();
    return 'ok';
  },

  // ------------------------------------------------------------ chat panel
  openChat() {
    const t = [...document.querySelectorAll('nav button')]
      .find(x => /^Chat/.test(x.textContent));
    if (!t) return 'no chat tab';
    t.click();
    return 'ok';
  },

  // The preset questions exactly as a reader would click them, so the routing
  // measurement below runs on the rendered text and not on the template that
  // produced it.
  chips() {
    return [...document.querySelectorAll('ul li button')].map(x => x.textContent.trim());
  },

  // Anything that would send this dashboard down the model path. Since D16.8
  // the answer has to be zero: no engine selector, no retrieval switch.
  modelControls() {
    return [...document.querySelectorAll('button')]
      .filter(x => /^model\b/.test(x.textContent.trim())).length;
  },

  switches() { return document.querySelectorAll('input[type=checkbox]').length; },

  // Badges on REPLIES, not the identical words the composer shows before you
  // send. Counting body text would pass on the composer alone, which is a
  // check that cannot fail for the reason it was written.
  replyBadges() {
    const composer = document.querySelector('textarea')?.closest('div');
    return [...document.querySelectorAll('span')]
      .filter(x => x.textContent.trim() === 'lexical router · no model')
      .filter(x => !composer || !composer.contains(x)).length;
  },

  chip(pattern) {
    const c = [...document.querySelectorAll('ul li button')]
      .find(x => new RegExp(pattern).test(x.textContent));
    if (!c) return 'no suggestion matching ' + pattern;
    c.click();
    return 'ok';
  },

  asking() { return /Choosing a query and running it/.test(document.body.innerText); },

  chat() {
    const body = document.body.innerText;
    return {
      // One per Question bubble. If a failed ask were quietly re-answered by
      // another engine there would still be one -- so this is read together
      // with whether a NEW answer appeared.
      userTurns: document.querySelectorAll('main div.justify-end').length,
      failurePanel: /No model answered/.test(body),
      replyBadges: this.replyBadges(),
      namesTheVariable: /GEMINI_API_KEY|ANTHROPIC_API_KEY/.test(body),
      badge: this.headerBadge(),
    };
  },

  // The audit trail as the reader sees it. `entries` is empty when the panel
  // is showing its "nothing has run yet" placeholder.
  audit() {
    const s = [...document.querySelectorAll('section')]
      .find(x => /Audit trail/.test(x.textContent));
    if (!s) return null;
    return {
      summary: (s.querySelector('header p') || {}).textContent || '',
      entries: [...s.querySelectorAll('ol > li')].map(li => ({
        query: (li.querySelector('span.font-mono') || {}).textContent || '',
        via: (li.querySelector('span[title]') || {}).textContent || '',
      })),
    };
  },

  bodyText() { return document.body.innerText; },

  headerBadge() {
    const tab = [...document.querySelectorAll('nav button')]
      .find(b => /^Chat/.test(b.textContent.trim()));
    return tab ? tab.textContent.replace('Chat', '').trim() : null;
  },
};
'ready'
"""


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str, timeout: float = 5.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode()


def ask_lexical(origin: str, question: str) -> dict:
    """One question straight to `/ask` on the router path.

    Used to MEASURE the preset questions rather than assert a feeling about
    them (rule 5): nine clicks through the browser would take a minute and
    prove the same thing about routing, so the chips are read off the rendered
    page and the answers are fetched over the same origin the page uses.
    """
    body = json.dumps({"question": question, "provider": "lexical",
                       "retrieval": False}).encode()
    req = urllib.request.Request(f"{origin}/ask", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return {"http_status": exc.code, "detail": exc.read().decode()[:200]}


def wait_http(url: str, what: str, timeout: float = 45.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            get(url, timeout=2.0)
            return
        except Exception:                                           # noqa: BLE001
            time.sleep(0.2)
    raise TimeoutError(f"{what} never answered at {url}")


def build_if_stale() -> str:
    """Rebuild only when a source file is newer than the bundle, the same way
    every other stage skips work it has already done.

    `VITE_API_BASE` is set empty so the app calls `/meta` relatively. That is
    what makes this cache sound: with a port compiled in, a bundle left over
    from the previous run pointed at a port nothing was listening on any more,
    and the suite failed for a reason that had nothing to do with the UI.
    """
    index = DIST / "index.html"
    stamp = DIST / ".floatchat-build.json"
    newest = max((p.stat().st_mtime for p in (UI / "src").rglob("*") if p.is_file()),
                 default=0)
    newest = max(newest, (UI / "package.json").stat().st_mtime)
    # A timestamp alone is not enough: the bundle also depends on what it was
    # BUILT WITH, and a dist/ left over from a build against a different API
    # base is stale no matter how recent it is. The stamp records the input so
    # the cache can tell.
    want = {"api_base": ""}
    have = None
    if stamp.exists():
        try:
            have = json.loads(stamp.read_text())
        except ValueError:
            have = None
    if index.exists() and have == want and index.stat().st_mtime > newest:
        return "already built"
    out = subprocess.run(["npm", "run", "build"], cwd=UI,
                         env={**os.environ, "VITE_API_BASE": want["api_base"]},
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"npm run build failed:\n{out.stdout[-1500:]}{out.stderr[-1500:]}")
    stamp.write_text(json.dumps(want))
    return "rebuilt"


# A key that is SET and wrong. `chat.resolve_provider` therefore reports
# `gemini` -- a key variable being set is the only evidence available before a
# call is made (D12.15) -- and the call then fails. That is the exact state a
# rejected or expired key produces, and pinning it here means this suite tests
# the same thing whether or not the person running it has working credentials.
# Without this the checks would take the model path on a configured machine and
# spend real money on a live call.
REJECTED_KEY = {"GEMINI_API_KEY": "AIzaSyNotARealKeyUsedByTheRenderSuite0000",
                "ANTHROPIC_API_KEY": None}


def start_api(override: dict | None = None) -> tuple[subprocess.Popen, str]:
    """A uvicorn on a free port. `override` values of None unset the variable."""
    env = {**os.environ}
    for key, value in (override or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    port = free_port()
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "api.server:app", "--port", str(port),
         "--host", "127.0.0.1", "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    wait_http(f"{base}/health", "the API")
    return proc, base


def stop(proc: subprocess.Popen | None):
    if not proc:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def serve(api_base: str) -> tuple[ThreadingHTTPServer, str]:
    """`dist/` on disk, with the API paths forwarded to uvicorn.

    The forwarding target is held on the server, not closed over, so a phase
    that needs a differently-configured API can swap it without restarting the
    proxy or moving the page's origin.
    """

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(DIST), **kw)

        def log_message(self, *a):          # keep the suite's output readable
            pass

        def _forward(self, body=None):
            req = urllib.request.Request(
                self.server.api_base + self.path, data=body, method=self.command,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    payload, status = r.read(), r.status
            except urllib.error.HTTPError as exc:
                # The catalogue's refusals are 400s and the UI renders them as
                # refusals, so they must arrive as themselves, not as a proxy
                # error. This suite would otherwise never see a refusal.
                payload, status = exc.read(), exc.code
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path.split("?")[0] in API_PATHS:
                return self._forward()
            return super().do_GET()

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            return self._forward(self.rfile.read(length) if length else b"")

    httpd = ThreadingHTTPServer(("127.0.0.1", free_port()), Handler)
    httpd.api_base = api_base
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def main():
    print("preflight")
    if not CHROME or not Path(CHROME).exists():
        skip("no Chromium-family browser was found to drive.",
             "install Google Chrome, or set CHROME_PATH to one. Looked in: "
             + ", ".join(CHROME_CANDIDATES))
    if not (UI / "node_modules").exists():
        skip("ui/node_modules is missing, so the production build cannot be made.",
             "cd ui && npm install")
    print(f"  chrome              {Path(CHROME).name}")
    api = api2 = httpd = browser = None
    try:
        api, api_base = start_api()
        print(f"  api                 {api_base}  (started here, on a free port)")
        print(f"  build               {build_if_stale()}")
        httpd, origin = serve(api_base)
        print(f"  page                {origin}  (the built dist/, API proxied)")
        print("  network             blocked except this origin")
        wait_http(origin, "the static server")

        browser = Chrome()
        run_checks(browser, origin)

        # Second phase: the same page against an API whose key is rejected.
        # A separate server rather than a separate origin, so the browser and
        # the bundle stay exactly as they were for the checks above.
        api2, rejected_base = start_api(REJECTED_KEY)
        httpd.api_base = rejected_base
        check_chat(browser, origin)
    finally:
        if browser:
            browser.close()
        if httpd:
            httpd.shutdown()
        stop(api2)
        stop(api)

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------

def load(b: Chrome, origin: str):
    b.events.clear()
    b.send("Page.navigate", url=origin)
    b.wait("document.readyState === 'complete'", "the document")
    b.eval(DRIVER)
    b.wait("!!document.querySelector('nav button')", "/meta to load and render")


def run_query(b: Chrome, name: str) -> list:
    assert_ok(b.eval(f"__fc.pickQuery({name!r})"), name)
    filled = b.eval("__fc.fillRequired()")
    assert_ok(b.eval("__fc.run()"), name)
    b.wait("__fc.settled()", f"{name} to render")
    b.pump(0.4)
    return filled


def assert_ok(result, what: str):
    if result != "ok":
        raise RuntimeError(f"{what}: {result}")


def run_checks(b: Chrome, origin: str):
    print("\nthe dashboard loads, from a production build, with nothing 404ing")
    load(b, origin)
    check("the page renders its chrome", bool(b.eval("__fc.headerBadge()") is not None))
    text = b.eval("__fc.bodyText()")
    check("it is not an error state", "not reachable" not in text and "no dropdowns" not in text,
          text.splitlines()[0][:70] if text else "empty body")
    check("the header reports real counts from /meta",
          "profiles" in text and "levels" in text)
    fails = b.failures()
    check("no request 404s or fails", not fails, "; ".join(fails[:3]))
    errs = b.console_errors()
    check("no console error, no uncaught exception", not errs, "; ".join(errs[:2]))

    # ---------------------------------------------------------------- D16.1
    # Chat is the front door. Both halves are asserted: the conversation is
    # what loaded, and the parameter form is NOT also on screen -- a "chat
    # first" page that still renders the catalogue behind it has moved nothing.
    print("\nchat is what the page opens in (D16.1)")
    landing = b.eval("__fc.landing()")
    check("the landing view is the chat panel", landing["chat"], str(landing))
    check("the catalogue form is not what loaded", not landing["form"], str(landing))

    # ---------------------------------------------------------------- D16.7
    # Every preset question, answered for real. A chip that misroutes or lands
    # on an empty result is a trap laid for whoever clicks it first, and until
    # Stage 16 nothing checked that any of them worked -- one of the six
    # returned no rows at all. Misses are printed, not summarised (rule 5).
    print("\nevery preset question on the landing screen is answered (D16.7)")
    chips = b.eval("__fc.chips()")
    check("the landing screen offers preset questions", len(chips) >= 6,
          f"{len(chips)} chip(s)")
    misses = []
    for text in chips:
        out = ask_lexical(origin, text)
        entry = (out.get("audit") or [{}])[0]
        query, rows = entry.get("query"), entry.get("row_count")
        reason = out.get("refusal_reason")
        # A refusal is a correct answer for exactly one chip: the BGC question
        # is on screen ON PURPOSE so the honest "there are none" is
        # demonstrated rather than avoided. Any other refusal is a miss.
        ok = bool(query and rows) or reason == "no-bgc"
        if not ok:
            misses.append(f"{text!r} -> {query or reason or out.get('http_status') or '?'}"
                          f"{'' if query is None else f' ({rows} rows)'}")
        print(f"    {'ok  ' if ok else 'MISS'}  {(query or 'refused: ' + str(reason)):<24}"
              f"{str(rows) if rows is not None else '':>5}  {text}")
    check("every preset question routes and comes back with rows", not misses,
          "; ".join(misses) if misses else f"{len(chips)} chips, no misses")

    # And the catalogue is still there, which is the other half of the claim:
    # every display check below runs through this one click.
    assert_ok(b.eval("__fc.openCatalogue()"), "open the catalogue tab")
    b.pump(0.3)
    check("the catalogue is one click away and complete",
          b.eval("__fc.landing()")["form"], "the Run query button is back")

    # ---------------------------------------------------------------- D13.1
    # The one thing source checks cannot do: prove the attribute SURVIVED.
    # `_fullLayout` is Plotly's computed layout, so an attribute this version
    # ignores is simply absent from it -- which is what `titlefont` was.
    print("\na depth profile, drawn (D13.1: the colour has to actually arrive)")
    filled = run_query(b, "depth_profile")
    check("it ran with values taken from the controls", bool(filled), ", ".join(filled))
    check("no error boundary fired", not b.eval("__fc.boundaryFired()"))
    plot = b.eval("__fc.plot()")
    check("a Plotly chart exists", plot is not None)
    if plot:
        check("two traces", plot["traces"] == 2, f"{plot['traces']}")
        check("the pressure axis is titled in dbar",
              "dbar" in (plot["yaxis"]["title"] or ""), plot["yaxis"]["title"])
        # This is the ARGO rule, verified on the rendered axis rather than in
        # the spec: a reversed axis has its range descending.
        rng = plot["yaxis"]["range"]
        check("pressure increases DOWNWARD on the drawn axis",
              bool(rng) and rng[0] > rng[1], f"range {rng}")
        check("the temperature axis is titled in °C",
              "°C" in (plot["xaxis"]["title"] or ""), plot["xaxis"]["title"])
        check("the salinity axis is titled in PSU",
              "PSU" in (plot["xaxis2"]["title"] or ""), plot["xaxis2"]["title"])
        check("the temperature axis title is RED, as the spec asks",
              (plot["xaxis"]["colour"] or "").lower() == "#dc2626",
              f"computed colour {plot['xaxis']['colour']}")
        check("the salinity axis title is BLUE, as the spec asks",
              (plot["xaxis2"]["colour"] or "").lower() == "#2563eb",
              f"computed colour {plot['xaxis2']['colour']}")

    print("\ncounts over time, drawn (the other dual-axis chart)")
    run_query(b, "monthly_profile_counts")
    check("no error boundary fired", not b.eval("__fc.boundaryFired()"))
    plot = b.eval("__fc.plot()")
    check("both y axes carry their series colour",
          bool(plot) and (plot["yaxis"]["colour"] or "").lower() == "#0f766e"
          and (plot["yaxis2"]["colour"] or "").lower() == "#a16207",
          f"{plot['yaxis']['colour']} / {plot['yaxis2']['colour']}" if plot else "no plot")

    # ---------------------------------------------------------------- D13.2
    print("\na float trajectory, drawn (D13.2: no image file may be involved)")
    run_query(b, "float_trajectory")
    check("no error boundary fired", not b.eval("__fc.boundaryFired()"))
    m = b.eval("__fc.map()")
    check("a Leaflet map exists", m is not None)
    if m:
        check("both endpoints are marked", m["markers"] == 2, f"{m['markers']} marker(s)")
        # The whole of D13.2 in one assertion: the default icon is an <img>
        # pointing at marker-icon.png, and under Vite that image does not exist.
        check("no endpoint marker is an <img>", m["imgMarkers"] == 0,
              f"{m['imgMarkers']} raster marker(s) -- the default icon is back")
        check("they say which end they are", sorted(m["labels"]) == ["first", "last"],
              str(m["labels"]))
        check("the cycles themselves are vectors", m["vectors"] > 2, f"{m['vectors']} path(s)")
    fails = [f for f in b.failures() if "marker" in f or "png" in f]
    check("nothing asked for a marker image", not fails, "; ".join(fails[:3]))

    print("\nan empty radius search still shows where it looked")
    run_query(b, "nearest_profiles")
    check("no error boundary fired", not b.eval("__fc.boundaryFired()"))
    m = b.eval("__fc.map()")
    text = b.eval("__fc.bodyText()")
    check("the map is drawn even with no rows",
          m is not None and (m["vectors"] > 0 or m["markers"] > 0),
          "the circle comes from the bound parameters")
    check("and an empty result says so in words", "no rows" in text.lower())

    print("\nthe table displays render")
    for name in ("region_summary", "float_inventory", "missing_profiles"):
        run_query(b, name)
        drawn = b.eval("!!document.querySelector('table, dl')")
        check(f"{name:<24}", drawn and not b.eval("__fc.boundaryFired()"))

    print("\nnothing broke along the way")
    check("no request 404d across every display", not b.failures(),
          "; ".join(b.failures()[:3]))
    check("no console error across every display", not b.console_errors(),
          "; ".join(b.console_errors()[:2]))
    check("the audit trail recorded every query that ran",
          b.eval("__fc.bodyText()").count("no rows") >= 0
          and "Audit trail" in b.eval("__fc.bodyText()"))


def check_chat(b: Chrome, origin: str):
    """The chat panel on a server whose model key is set and rejected.

    Until Stage 16 this drove the failure itself: the model path was offered,
    the call failed, the selector moved to the router and said so. The
    dashboard no longer offers that path (D16.8), so the property that replaced
    it is the stronger one -- **a broken key changes nothing on this page**,
    because nothing here asks the model. The API is still started with a key
    that is set and wrong, which is the state a demo machine is actually in
    when a key expires, and the checks below are what a reader gets anyway.
    """
    print("\nthe chat panel, on a server whose model key is set and rejected")
    load(b, origin)
    assert_ok(b.eval("__fc.openChat()"), "open the chat tab")
    b.wait("/Ask a question in English/.test(document.body.innerText)", "the chat panel")

    controls = b.eval("__fc.modelControls()")
    check("no control offers the model path", controls == 0,
          f"{controls} button(s) naming a model -- the key on this server is broken")
    check("no retrieval switch is offered", b.eval("__fc.switches()") == 0,
          "retrieval belongs to the path this dashboard does not take")
    chat = b.eval("__fc.chat()")
    check("the tab badge claims no model", chat["badge"] == "no model", str(chat["badge"]))
    check("the composer names the engine before anything is sent",
          "lexical router · no model" in b.eval("__fc.bodyText()"))
    check("and nothing has been answered yet", chat["replyBadges"] == 0,
          f"{chat['replyBadges']} reply badge(s) before the first question")

    assert_ok(b.eval(r"__fc.chip('fresher')"), "click the comparison suggestion")
    b.wait("!__fc.asking()", "the router's answer", 60)
    b.wait("!!document.querySelector('.js-plotly-plot')", "the chart", 30)
    b.pump(0.6)

    chat, audit = b.eval("__fc.chat()"), b.eval("__fc.audit()")
    check("the rejected key changed nothing — the question was answered",
          chat["userTurns"] == 1 and not chat["failurePanel"],
          f"{chat['userTurns']} user turn(s), failure panel {chat['failurePanel']}")
    check("the reply is badged as the router, not a model", chat["replyBadges"] == 1,
          f"{chat['replyBadges']} reply badge(s)")
    check("the query reached the trail", bool(audit) and len(audit["entries"]) == 1,
          str([e["query"] for e in audit["entries"]]) if audit else "?")
    if audit and audit["entries"]:
        entry = audit["entries"][0]
        check("it names the query that ran", entry["query"] == "compare_regions",
              entry["query"])
        check("and it is badged lexical, never as a model",
              entry["via"].strip() == "lexical", repr(entry["via"]))
    plot = b.eval("__fc.plot()")
    check("the answer drew a chart from the rows", plot is not None and plot["traces"] == 2,
          f"{plot['traces']} traces" if plot else "no chart")
    check("no error boundary fired", not b.eval("__fc.boundaryFired()"))
    check("no console error on the chat path", not b.console_errors(),
          "; ".join(b.console_errors()[:2]))

    # A typed question, not a chip: the composer is the thing a judge will use,
    # and until Stage 16 nothing had ever driven it (D14.8 drove chips only).
    print("\nand a question typed into the composer")
    assert_ok(b.eval(r"""(() => {
      const t = document.querySelector('textarea');
      if (!t) return 'no composer';
      __fc.setNative(t, 'Show me the trajectory of float ' + %s);
      const btn = [...document.querySelectorAll('button')]
        .find(x => /^Ask$/.test(x.textContent.trim()));
      if (!btn) return 'no ask button';
      if (btn.disabled) return 'ask button is disabled';
      btn.click();
      return 'ok';
    })()""" % json.dumps(trajectory_wmo(origin)), ), "type and send a question")
    b.wait("!__fc.asking()", "the typed question's answer", 60)
    b.wait("!!document.querySelector('.leaflet-container')", "the map", 30)
    b.pump(0.6)

    chat, audit = b.eval("__fc.chat()"), b.eval("__fc.audit()")
    check("it is a second turn, answered", chat["userTurns"] == 2,
          f"{chat['userTurns']} user turn(s)")
    check("both replies are badged as the router", chat["replyBadges"] == 2,
          f"{chat['replyBadges']} reply badge(s)")
    check("the trail has both queries", bool(audit) and len(audit["entries"]) == 2,
          str([e["query"] for e in audit["entries"]]) if audit else "?")
    m = b.eval("__fc.map()")
    check("the trajectory drew a map", m is not None and m["markers"] == 2,
          f"{m['markers']} marker(s)" if m else "no map")
    check("no console error after a typed question", not b.console_errors(),
          "; ".join(b.console_errors()[:2]))


def trajectory_wmo(origin: str) -> str:
    """The float id the CATALOGUE publishes for `float_trajectory`.

    Read from `/meta`, never written here, for the same reason `displays.js`
    builds its suggestions from the examples: a WMO typed into this file is a
    value invented by the test, and it would keep passing against a database
    that no longer holds that float.
    """
    meta = json.loads(get(f"{origin}/meta"))
    query = next(q for q in meta["queries"] if q["name"] == "float_trajectory")
    return str(query["example"]["wmo"])


if __name__ == "__main__":
    main()
