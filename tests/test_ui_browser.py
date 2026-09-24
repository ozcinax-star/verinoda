"""`verinoda ui` in a real browser: the page, driven through the DevTools protocol.

Chrome, Edge or Chromium runs headless (``VERINODA_BROWSER`` names one; otherwise the usual
install places are tried) and the tests skip when none is found. The protocol is spoken over a
WebSocket with the standard library, so no test dependency is added. Every test also fails on
a script error, a failed request or a Content-Security-Policy violation the page reported.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

import pytest

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from verinoda import workflow
from verinoda.paths import graph_path
from verinoda.store import open_store
from verinoda.ui import export
from verinoda.ui import server as uiserver

ROOT = Path(__file__).resolve().parents[1]
GLOW = ROOT / "examples" / "glow_mod"

pytestmark = pytest.mark.browser


def _find_browser() -> str | None:
    named = os.environ.get("VERINODA_BROWSER")
    if named:
        return named if Path(named).exists() or shutil.which(named) else None
    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge",
                 "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    places = []
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                places += [Path(base) / "Google/Chrome/Application/chrome.exe", Path(base) / "Microsoft/Edge/Application/msedge.exe"]
    elif sys.platform == "darwin":
        places += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                   Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    return next((str(p) for p in places if p.exists()), None)


class _WebSocket:
    """Just enough of RFC 6455 for the DevTools protocol: text frames, masked from the client."""

    def __init__(self, url: str, timeout: float = 30):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        got = b""
        while b"\r\n\r\n" not in got:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("no WebSocket handshake")
            got += chunk
        head, self.buf = got.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.decode(errors="replace"))

    def _frame(self, op: int, data: bytes) -> None:
        n, mask = len(data), os.urandom(4)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
                                    if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        body = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        self.sock.sendall(head + mask + body)

    def send(self, text: str) -> None:
        self._frame(0x1, text.encode("utf-8"))

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("the browser closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> str:
        parts = []
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            if op == 0x8:
                raise ConnectionError("the browser closed the connection")
            if op == 0x9:
                self._frame(0xA, data)
                continue
            if op in (0x0, 0x1, 0x2):
                parts.append(data)
                if b0 & 0x80:
                    return b"".join(parts).decode("utf-8")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Browser:
    """One headless browser with one page; ``js`` evaluates in it (promises awaited)."""

    def __init__(self, exe: str, profile: Path):
        args = [exe, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}", "--no-first-run",
                "--no-default-browser-check", "--disable-gpu", "--disable-extensions", "--disable-sync",
                "--window-size=1400,900", "--lang=en-US", "about:blank"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.insert(1, "--no-sandbox")  # a root CI container
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file, end = profile / "DevToolsActivePort", time.monotonic() + 30
        while not (port_file.exists() and port_file.read_text().strip()):
            if self.proc.poll() is not None or time.monotonic() > end:
                self.close()
                raise RuntimeError("the browser did not start")
            time.sleep(0.1)
        port = int(port_file.read_text().split()[0])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as r:
            page = next(t for t in json.load(r) if t["type"] == "page")
        self.ws, self.n, self.events = _WebSocket(page["webSocketDebuggerUrl"]), 0, []
        for domain in ("Runtime", "Log", "Page", "Network"):
            self.call(f"{domain}.enable")

    def call(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise AssertionError(f"{expr[:80]}: {d.get('exception', {}).get('description') or d.get('text')}")
        return r["result"].get("value")

    def wait(self, expr: str, timeout: float = 15) -> None:
        end = time.monotonic() + timeout
        while True:
            if self.js(f"!!({expr})"):  # an element comes back as {}: ask for the truth value
                return
            if time.monotonic() > end:
                shown = self.js("(document.querySelector('#main') || document.body).innerText.slice(0, 400)")
                raise AssertionError(f"timed out waiting for: {expr}\npage: {shown!r}\nproblems: {self.problems()}")
            time.sleep(0.1)

    def goto(self, url: str) -> None:
        self.events.clear()
        self.call("Page.navigate", url=url)
        self.wait("document.readyState === 'complete' && location.href !== 'about:blank'")

    def problems(self) -> list[str]:
        """What the page reported going wrong: script errors, failed requests, CSP violations."""
        out = []
        for e in self.events:
            m, p = e["method"], e.get("params", {})
            if m == "Runtime.exceptionThrown":
                d = p["exceptionDetails"]
                out.append(d.get("exception", {}).get("description") or d.get("text", "exception"))
            elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
                out.append(" ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
            elif m == "Log.entryAdded" and p["entry"]["level"] == "error":
                out.append(f"{p['entry'].get('source')}: {p['entry']['text']} {p['entry'].get('url', '')}")
            elif m == "Network.loadingFailed" and not p.get("canceled"):
                out.append(f"request failed: {p.get('errorText')}")
        return out

    def close(self) -> None:
        if getattr(self, "ws", None) is not None:
            self.ws.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    exe = _find_browser()
    if not exe:
        pytest.skip("no Chrome, Edge or Chromium found (VERINODA_BROWSER names one)")
    b = Browser(exe, tmp_path_factory.mktemp("profile"))
    yield b
    b.close()


@pytest.fixture(scope="module")
def glow(tmp_path_factory):
    repo = tmp_path_factory.mktemp("uib") / "glow"
    shutil.copytree(GLOW, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__"))
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    return repo


@pytest.fixture(scope="module")
def site(glow):
    server, url = uiserver.start(glow, port=0)
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture
def page(browser):
    yield browser
    assert browser.problems() == []


def _open(b: Browser, url: str) -> None:
    b.goto(url)
    b.wait("document.querySelector('#tree') && document.querySelector('#tree').children.length > 0")


def _search_open(b: Browser, q: str, title: str) -> None:
    """Type ``q`` into the search box and open the result called ``title``."""
    b.js(f"document.querySelector('#search').value = {json.dumps(q)};"
         " document.querySelector('#search').dispatchEvent(new Event('input', {bubbles: true}))")
    pick = f"[...document.querySelectorAll('#results .result')].find((r) => r.querySelector('.t') && r.querySelector('.t').textContent === {json.dumps(title)})"
    b.wait(pick)
    b.js(pick + ".click()")
    b.wait(f"document.querySelector('#note h1') && document.querySelector('#note h1').textContent.includes({json.dumps(title)})")


def test_the_start_page_shows_the_project_and_its_tree(page, site):
    _open(page, site)
    page.wait("document.querySelector('#note .home h1')")
    info = page.js("({h1: document.querySelector('#note h1').textContent, cards: document.querySelectorAll('#note .card').length,"
                   " tree: [...document.querySelectorAll('#tree .tfolder')].map((f) => f.textContent)})")
    assert info["h1"] == "glow" and info["cards"] == 4
    assert {"src", "datapack"} <= set(info["tree"])
    page.js("[...document.querySelectorAll('#tree .tfolder')].find((f) => f.textContent === 'src').click()")
    page.wait("document.querySelector('#tree .tchildren:not([hidden]) .tnode')")


def test_a_note_shows_its_code_links_and_the_lines_they_are_on(page, site):
    _open(page, site + "#/")
    _search_open(page, "spawn", "Wisp.spawn()")
    n = page.js("""({
      code: document.querySelectorAll('#note .code td.ln').length,
      sections: [...document.querySelectorAll('#note h2.sec')].map((h) => h.id),
      snips: document.querySelectorAll('#note .snip').length,
      editor: !!document.querySelector('#note a.chip.editor[href^="vscode://"]'),
      local: document.querySelector('#local').width > 0,
      title: document.title,
    })""")
    assert "sec-called_by" in n["sections"] and n["snips"] > 0 and n["editor"] and n["local"], n
    assert n["code"] > 0 and "Wisp.spawn()" in n["title"]


def test_hovering_a_link_previews_its_note(page, site):
    _open(page, site + "#/")
    _search_open(page, "spawn", "Wisp.spawn()")
    target = page.js("""(() => {
      const a = document.querySelector('#note ul.links a[href^="#/n/"]');
      a.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
      return a.textContent;
    })()""")
    page.wait("!document.querySelector('#preview').hidden", timeout=5)
    card = page.js("document.querySelector('#preview strong').textContent")
    assert card and card in target
    page.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
    page.wait("document.querySelector('#preview').hidden", timeout=5)


def test_a_note_of_your_own_is_written_from_the_page(page, site, glow):
    _open(page, site + "#/")
    _search_open(page, "spawn", "Wisp.spawn()")
    page.wait("[...document.querySelectorAll('#note button.addnote')].length >= 2")
    page.js("[...document.querySelectorAll('#note button.addnote')][0].click()")  # the first: add a note
    page.wait("document.querySelector('#sec-mynote textarea')")
    page.js("document.querySelector('#sec-mynote textarea').value = 'Spawns **one** wisp.';"
            " document.querySelector('#sec-mynote button.primary').click()")
    page.wait("document.querySelector('#sec-mynote .unote-body strong')")
    assert "st-fresh" in page.js("document.querySelector('#sec-mynote .unote-st').className")
    files = list((glow / ".verinoda" / "notes").rglob("*.md"))
    assert len(files) == 1 and "Spawns **one** wisp." in files[0].read_text(encoding="utf-8")
    page.js("document.querySelector('#sec-mynote button.danger').click()")  # delete: two clicks, no dialog
    page.js("document.querySelector('#sec-mynote button.danger').click()")
    page.wait("!document.querySelector('#sec-mynote')")
    assert not list((glow / ".verinoda" / "notes").rglob("*.md"))


def test_impact_lists_what_uses_a_note(page, site):
    _open(page, site + "#/")
    _search_open(page, "spawn", "Wisp.spawn()")
    button = "[...document.querySelectorAll('#note button.addnote')].find((b) => /impact/i.test(b.textContent))"
    page.wait(button)
    page.js(button + ".click()")
    page.wait("document.querySelector('#note .panel a[href^=\"#/n/\"]')")


def test_the_graph_view_draws_the_project(page, site):
    _open(page, site + "#/graph")
    page.wait(r"!document.querySelector('#graphview').hidden && /\d/.test(document.querySelector('#graph-info').textContent)")
    time.sleep(0.5)  # a few frames of the layout
    info = page.js("""(() => {
      const c = document.querySelector('#global'), g = c.getContext('2d');
      const px = g.getImageData(0, 0, c.width, c.height).data;
      const first = [px[0], px[1], px[2]];
      let other = 0;
      for (let i = 0; i < px.length; i += 4 * 53) if (px[i] !== first[0] || px[i + 1] !== first[1] || px[i + 2] !== first[2]) other++;
      return {w: c.width, h: c.height, other};
    })()""")
    assert info["w"] > 300 and info["h"] > 200 and info["other"] > 0, info
    page.js("document.querySelector('#graph-close').click()")
    page.wait("document.querySelector('#graphview').hidden")


def test_a_question_is_answered_in_the_page(page, site):
    _open(page, site + "#/q/" + quote("how does a wisp spawn?"))
    page.wait("document.querySelectorAll('#note .code').length > 0", timeout=30)


def test_a_slow_start_page_does_not_replace_a_note_opened_meanwhile(page, site):
    _open(page, site + "#/")
    _search_open(page, "spawn", "Wisp.spawn()")
    note = page.js("location.hash")
    # the start page's numbers come late; a note is opened before they do
    page.js("""(() => { const real = window.fetch; window.__fetch = real;
      window.fetch = (u, o) => String(u).includes('/api/stats') ? new Promise((ok) => setTimeout(() => ok(real(u, o)), 1200)) : real(u, o); })()""")
    page.js("location.hash = '#/'")
    time.sleep(0.2)
    page.js(f"location.hash = {json.dumps(note)}")
    page.wait("document.querySelector('#note h1') && document.querySelector('#note h1').textContent.includes('Wisp.spawn()')")
    time.sleep(1.6)  # the numbers have come
    try:
        assert page.js("!!document.querySelector('#note h1') && !document.querySelector('.home')")
    finally:
        page.js("window.fetch = window.__fetch")


def test_the_page_redraws_when_the_index_changes(page, site, glow):
    _open(page, site + "#/")
    _search_open(page, "spawn", "Wisp.spawn()")
    page.js("window.__before = document.querySelector('#note h1')")
    time.sleep(0.5)  # the page has read the index's key
    gp = graph_path(glow)
    st = gp.stat()
    os.utime(gp, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    page.wait("document.querySelector('#toast') && !document.querySelector('#toast').hidden", timeout=10)
    page.wait("document.querySelector('#note h1') !== window.__before && document.querySelector('#note h1').textContent.includes('spawn')")


def test_the_exported_file_works_from_disk(page, glow, tmp_path):
    out = export.write(glow, tmp_path / "glow-graph.html")
    _open(page, Path(out["path"]).as_uri())  # it opens on the graph view
    page.wait(r"!document.querySelector('#graphview').hidden && /\d/.test(document.querySelector('#graph-info').textContent)")
    if not page.js("document.querySelector('#graph-3d').classList.contains('on')"):  # the 3D view works from the file too
        page.js("document.querySelector('#graph-3d').click()")
    page.wait("window.__verinoda.g3.active && window.__verinoda.g3.frames > 5")
    page.js("document.querySelector('#graph-3d').click()")
    page.js("document.querySelector('#graph-close').click()")
    page.wait("document.querySelector('#note .home h1')")
    _search_open(page, "Wisp", "Wisp")  # a symbol opens the note of its file
    page.wait("document.querySelector('#note h2.sec')")
    assert page.js("document.querySelectorAll('#note .code').length") == 0  # no code in the file


# -- the graph in three dimensions, the command bar, the tour ------------------------------------------

def _in_3d(page, site):
    _open(page, site + "#/graph")
    page.wait("window.__verinoda.g3 && (window.__verinoda.global.nodes.length > 0 || window.__verinoda.g3.nodes.length > 0)")
    if not page.js("document.querySelector('#graph-3d').classList.contains('on')"):
        page.js("document.querySelector('#graph-3d').click()")
    page.wait("window.__verinoda.g3.active && window.__verinoda.g3.nodes.length > 0 && window.__verinoda.g3.frames > 5")


def _key(page, key, **mods):
    flags = ", ".join(f"{k}: true" for k in mods)
    page.js(f"document.dispatchEvent(new KeyboardEvent('keydown', {{key: {json.dumps(key)}, bubbles: true{', ' + flags if flags else ''}}}))")


def test_the_3d_view_flies_to_a_file_walks_its_links_and_follows_it(page, site):
    _in_3d(page, site)
    hub = page.js("""(() => {
      const g = window.__verinoda.g3, n = [...g.nodes].sort((a, b) => b.deg - a.deg)[0], p = g.screenOf(n.id);
      const c = document.querySelector('#global3d'), r = c.getBoundingClientRect();
      for (const type of ['pointerdown', 'pointerup'])
        c.dispatchEvent(new PointerEvent(type, {clientX: r.left + p.x, clientY: r.top + p.y, bubbles: true, button: 0, pointerId: 1}));
      return {id: n.id, title: n.title};
    })()""")
    page.wait(f"!document.querySelector('#hud').hidden && document.querySelector('#hud strong').textContent === {json.dumps(hub['title'])}")
    assert page.js("window.__verinoda.g3.selected.id") == hub["id"]
    # a dot in front of the hub, just off its middle: a click on the middle is still the hub's
    assert page.js(f"""(() => {{
      const g = window.__verinoda.g3, hub = g.byId.get({json.dumps(hub['id'])});
      const dot = [...g.nodes].find((m) => m !== hub && g.visible(m));
      g.project();
      dot.sx = hub.sx + g.rad(dot) + 1; dot.sy = hub.sy; dot.depth = hub.depth - 1; dot.sv = true;
      return g.hit(hub.sx, hub.sy).id;
    }})()""") == hub["id"]
    _key(page, "1")  # the first linked file in the panel
    page.wait(f"window.__verinoda.g3.selected.id !== {json.dumps(hub['id'])}")
    assert page.js(f"window.__verinoda.g3.adj.get({json.dumps(hub['id'])}).has(window.__verinoda.g3.selected.id)")
    _key(page, "f")
    page.wait("window.__verinoda.g3.follow === window.__verinoda.g3.selected")
    _key(page, "Backspace")  # back along the trail
    page.wait(f"window.__verinoda.g3.selected.id === {json.dumps(hub['id'])}")
    _key(page, "Enter")
    page.wait(f"location.hash === '#/n/' + encodeURIComponent({json.dumps(hub['id'])})")
    page.wait("document.querySelector('#note h1')")


def test_the_command_bar_frames_a_region_shows_an_impact_and_asks(page, site):
    _in_3d(page, site)
    region = page.js(r"document.querySelector('#legend .it span:last-child').textContent.replace(/ \(\d+\)$/, '')")

    def say(text):
        _key(page, "k", ctrlKey=True)
        page.wait("!document.querySelector('#palette').hidden")
        page.js(f"(() => {{ const i = document.querySelector('#pal-input'); i.value = {json.dumps(text)}; i.dispatchEvent(new Event('input')); }})()")
        page.wait("document.querySelector('#pal-list .pal-it.active')")
        time.sleep(0.3)  # the list for this text, not the one before it
        page.js("document.querySelector('#pal-input').dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}))")
        page.wait("document.querySelector('#palette').hidden")

    say(f"region {region}")
    page.wait(f"window.__verinoda.g3.region && document.querySelector('#hud strong').textContent === {json.dumps(region)}")
    say("impact spawn")
    page.wait("document.querySelector('#hud strong') && /Impact/.test(document.querySelector('#hud strong').textContent)")
    assert page.js("window.__verinoda.g3.region.size") > 1
    say("how does a wisp spawn?")
    page.wait("location.hash.startsWith('#/q/')")


def test_the_tour_goes_round_the_regions_and_every_key_is_listed(page, site):
    _in_3d(page, site)
    _key(page, "t")
    page.wait("!document.querySelector('#hud').hidden && document.querySelector('#hud .hud-progress')")
    first = page.js("document.querySelector('#hud strong').textContent")
    _key(page, "]")
    page.wait(f"document.querySelector('#hud strong').textContent !== {json.dumps(first)}")
    _key(page, "Escape")  # the tour stops; the region stays lit
    page.wait("!document.querySelector('#hud .hud-progress')")
    _key(page, "Escape")  # then what is lit clears
    page.wait("document.querySelector('#hud').hidden && !window.__verinoda.g3.region")
    _key(page, "?")
    page.wait("!document.querySelector('#keys').hidden && document.querySelectorAll('#keys kbd').length > 20")
    _key(page, "Escape")
    page.wait("document.querySelector('#keys').hidden")
    _key(page, "v")  # and back to the flat graph
    page.wait("!document.querySelector('#global').hidden && document.querySelector('#global3d').hidden")


def test_a_watched_file_that_changes_is_announced(page, site, glow):
    _in_3d(page, site)
    hub = page.js("""(() => {
      const g = window.__verinoda.g3, n = [...g.nodes].filter((m) => m.file).sort((a, b) => b.deg - a.deg)[0], p = g.screenOf(n.id);
      const c = document.querySelector('#global3d'), r = c.getBoundingClientRect();
      for (const type of ['pointerdown', 'pointerup'])
        c.dispatchEvent(new PointerEvent(type, {clientX: r.left + p.x, clientY: r.top + p.y, bubbles: true, button: 0, pointerId: 1}));
      return {id: n.id, title: n.title, file: n.file};
    })()""")
    page.wait(f"window.__verinoda.g3.selected && window.__verinoda.g3.selected.id === {json.dumps(hub['id'])}")
    _key(page, "p")  # watch it
    page.wait("!document.querySelector('#watched').hidden && document.querySelector('#watched .chip')")
    assert hub["title"] in page.js("document.querySelector('#watched').textContent")
    assert json.loads(page.js("localStorage.getItem('vn.watched')"))[0]["id"] == hub["id"]
    f = glow / hub["file"]
    original = f.read_bytes()
    f.write_bytes(original + b"\n// changed while watched\n")
    try:
        _key(page, "p")  # unwatch and watch again: the watch list looks at once
        _key(page, "p")
        page.wait("document.querySelector('#watched .chip.changed')", timeout=10)
        page.wait("document.querySelector('#toast') && !document.querySelector('#toast').hidden", timeout=10)
        assert hub["title"] in page.js("document.querySelector('#toast').textContent")
    finally:
        f.write_bytes(original)
    _key(page, "p")  # unwatch
    page.wait("document.querySelector('#watched').hidden")
    # a watched folder is still found when the graph is coloured by community
    _key(page, "]")
    page.wait("window.__verinoda.g3.region && !window.__verinoda.g3.selected")
    region = page.js("document.querySelector('#hud strong').textContent")
    _key(page, "p")
    page.wait("document.querySelector('#watched .chip')")
    page.js("(() => { const s = document.querySelector('#graph-color'); s.value = 'community'; s.dispatchEvent(new Event('change')); })()")
    _key(page, "Escape")
    page.wait("!window.__verinoda.g3.region")
    page.js("document.querySelector('#watched .chip').click()")
    page.wait(f"document.querySelector('#graph-color').value === 'folder' && window.__verinoda.g3.region "
              f"&& document.querySelector('#hud strong').textContent === {json.dumps(region)}")
    _key(page, "p")
    page.wait("document.querySelector('#watched').hidden")
