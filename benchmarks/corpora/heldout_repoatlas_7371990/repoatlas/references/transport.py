"""Offline-first HTTP transport for reference resolution (docs/DESIGN.md D15).

Three implementations of ``get(url, *, accept=None) -> Response``:

``LiveTransport``
    The vendored SSRF-guarded opener (``project_index.security``), a host
    policy (minimum interval between requests per host, rate-limit headers
    honoured: a host that answered ``x-ratelimit-remaining: 0`` is not called
    again before its reset time) and a User-Agent that names RepoAtlas. No
    credentials are ever added.
``CacheTransport``
    On disk under ``.repoatlas/research/http-cache/<key>.json``. Modes: ``off``
    (cache only - a miss raises :class:`Offline`), ``cache`` (cache first,
    network on a miss), ``on`` (network first, the cache when the network
    fails).
``CassetteTransport``
    Replays recorded JSON files (the cache format - a recorded run *is* the
    fixture). A miss raises :class:`CassetteMiss`, so a test that would reach
    the network fails loudly instead of silently going live.

Entries (schema ``repoatlas.http_cassette/1``) keep only the request method,
URL (credential-like query parameters removed), ``Accept`` and a redacted
``User-Agent``, plus a subset of response headers; ``Authorization``,
``Cookie`` and ``Set-Cookie`` are never stored. Bodies are stored as text (or
base64) with their sha256; recordings may be trimmed and say so.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "repoatlas.http_cassette/1"
RECORDER = "repoatlas.references.transport/1"
USER_AGENT = "repoatlas/0.1 (+https://github.com/; reference resolver)"
KEEP_RESPONSE_HEADERS = ("content-type", "etag", "last-modified", "location", "retry-after", "link")
SECRET_PARAMS = re.compile(r"^(?:access_token|token|private_token|api_key|apikey|key|sig|signature|client_secret|"
                           r"password|auth)$", re.I)
MAX_BODY = 256 * 1024
# Minimum seconds between two requests to a host (anonymous budgets; see research-intent-refs P1).
HOST_INTERVAL = {"export.arxiv.org": 3.0, "api.github.com": 1.0, "app.readthedocs.org": 12.0,
                 "readthedocs.org": 12.0, "crates.io": 1.0, "api.crossref.org": 0.1,
                 "archive.softwareheritage.org": 1.0, "api.stackexchange.com": 1.0}


class TransportError(RuntimeError):
    """Base class: the request could not be answered."""


class Offline(TransportError):
    """Network mode ``off`` and the response is not cached."""


class CassetteMiss(TransportError):
    """A test asked for a request that no cassette recorded (never falls through to the network)."""


class RateLimited(TransportError):
    def __init__(self, host: str, reset: float | None):
        self.host, self.reset = host, reset
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset)) if reset else "unknown"
        super().__init__(f"rate limit of {host} exhausted until {when}")


@dataclass
class Response:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    url: str = ""
    final_url: str = ""
    from_cache: bool = False
    retrieved_at: str | None = None

    def json(self):
        return json.loads(self.body.decode("utf-8", errors="replace"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def clean_url(url: str) -> str:
    """The URL without credential-like query parameters (what keys and cassettes store)."""
    p = urllib.parse.urlsplit(url)
    if not p.query:
        return url
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not SECRET_PARAMS.match(k)]
    return urllib.parse.urlunsplit(p._replace(query=urllib.parse.urlencode(q)))


def cache_key(method: str, url: str, accept: str | None) -> str:
    return hashlib.sha1(f"{method.upper()} {clean_url(url)} {accept or ''}".encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def to_entry(resp: Response, *, method: str = "GET", accept: str | None = None, trimmed: bool = False,
             recorder: str = RECORDER) -> dict:
    body = resp.body or b""
    entry_body: dict = {"body_sha256": hashlib.sha256(body).hexdigest(), "body_text": None, "body_b64": None}
    try:
        entry_body["body_text"] = body.decode("utf-8")
    except UnicodeDecodeError:
        entry_body["body_b64"] = base64.b64encode(body).decode("ascii")
    headers = {k.lower(): v for k, v in (resp.headers or {}).items()
               if k.lower() in KEEP_RESPONSE_HEADERS or k.lower().startswith(("x-ratelimit-", "x-rate-limit-",
                                                                              "x-api-pool"))}
    return {"schema": SCHEMA, "key": cache_key(method, resp.url, accept),
            "request": {"method": method.upper(), "url": clean_url(resp.url),
                        "headers": {**({"Accept": accept} if accept else {}), "User-Agent": "<redacted>"}},
            "response": {"status": resp.status, "headers": headers, "final_url": clean_url(resp.final_url or resp.url),
                         **entry_body, "trimmed": trimmed},
            "recorded_at": resp.retrieved_at or _now_iso(), "recorder": recorder}


def from_entry(entry: dict, *, from_cache: bool = True) -> Response:
    r = entry["response"]
    if r.get("body_text") is not None:
        body = r["body_text"].encode("utf-8")
    elif r.get("body_b64"):
        body = base64.b64decode(r["body_b64"])
    else:
        body = b""
    return Response(status=int(r["status"]), headers=dict(r.get("headers") or {}), body=body,
                    url=entry["request"]["url"], final_url=r.get("final_url") or entry["request"]["url"],
                    from_cache=from_cache, retrieved_at=entry.get("recorded_at"))


class Transport:
    """Counts what it did; subclasses implement :meth:`_get`."""

    def __init__(self):
        self.network_calls = 0
        self.cache_hits = 0
        self.log: list[dict] = []

    def get(self, url: str, *, accept: str | None = None) -> Response:
        resp = self._get(url, accept=accept)
        if resp.from_cache:
            self.cache_hits += 1
        self.log.append({"url": clean_url(url), "status": resp.status, "from_cache": resp.from_cache})
        return resp

    def _get(self, url: str, *, accept: str | None = None) -> Response:  # pragma: no cover - abstract
        raise NotImplementedError


class LiveTransport(Transport):
    def __init__(self, *, user_agent: str = USER_AGENT, timeout: float = 20.0, max_bytes: int = 20 * 1024 * 1024,
                 intervals: dict | None = None, state_path: Path | None = None):
        super().__init__()
        self.user_agent, self.timeout, self.max_bytes = user_agent, timeout, max_bytes
        self.intervals = {**HOST_INTERVAL, **(intervals or {})}
        self.state_path = state_path
        self._last: dict[str, float] = {}
        self._blocked: dict[str, float] = self._load_state()

    def _load_state(self) -> dict[str, float]:
        if self.state_path and self.state_path.is_file():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                return {h: float(t) for h, t in (data.get("blocked_until") or {}).items() if float(t) > time.time()}
            except (OSError, ValueError):
                return {}
        return {}

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_bytes(json.dumps({"blocked_until": self._blocked}, indent=1).encode("utf-8"))
        except OSError:
            pass

    def _get(self, url: str, *, accept: str | None = None) -> Response:
        import urllib.request

        from repoatlas.project_index import security as sec

        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        until = self._blocked.get(host)
        if until and until > time.time():
            raise RateLimited(host, until)
        wait = self.intervals.get(host, 0.0) - (time.monotonic() - self._last.get(host, -1e9))
        if wait > 0:
            time.sleep(min(wait, 15.0))
        try:
            sec.validate_url(url)
        except ValueError as exc:
            raise TransportError(str(exc)[:300]) from exc
        build = getattr(sec, "_build_opener", None)
        opener = build() if build else urllib.request.build_opener()
        headers = {"User-Agent": self.user_agent}
        if accept:
            headers["Accept"] = accept
        req = urllib.request.Request(url, headers=headers)
        self.network_calls += 1
        self._last[host] = time.monotonic()
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode() or 200
                body = resp.read(self.max_bytes + 1)
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                final = resp.geturl() if hasattr(resp, "geturl") else url
        except urllib.error.HTTPError as exc:
            status, hdrs, final = exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, url
            try:
                body = exc.read(self.max_bytes)
            except Exception:  # noqa: BLE001
                body = b""
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise TransportError(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"[:300]) from exc
        if len(body) > self.max_bytes:
            raise TransportError(f"response from {host} exceeds {self.max_bytes // 1048576} MB")
        remaining = hdrs.get("x-ratelimit-remaining")
        if remaining is not None and str(remaining).strip() == "0":
            reset = hdrs.get("x-ratelimit-reset")
            self._blocked[host] = float(reset) if reset and reset.isdigit() else time.time() + 3600
            self._save_state()
        if status == 429 or (status == 403 and remaining == "0"):
            retry = hdrs.get("retry-after")
            self._blocked[host] = time.time() + (float(retry) if retry and retry.isdigit() else 60.0)
            self._save_state()
        return Response(status=status, headers=hdrs, body=body, url=url, final_url=final, from_cache=False,
                        retrieved_at=_now_iso())


class CacheTransport(Transport):
    def __init__(self, cache_dir: Path, *, mode: str = "cache", inner: Transport | None = None):
        super().__init__()
        if mode not in ("off", "cache", "on"):
            raise ValueError(f"network mode must be off, cache or on, not {mode!r}")
        self.cache_dir, self.mode = Path(cache_dir), mode
        self.inner = inner if inner is not None else (LiveTransport(state_path=self.cache_dir / "hosts.json")
                                                      if mode != "off" else None)

    def _path(self, url: str, accept: str | None) -> Path:
        return self.cache_dir / f"{cache_key('GET', url, accept)}.json"

    def _read(self, url: str, accept: str | None) -> Response | None:
        p = self._path(url, accept)
        if not p.is_file():
            return None
        try:
            return from_entry(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return None

    def _write(self, resp: Response, accept: str | None) -> None:
        if resp.status >= 500 or resp.status == 429:
            return  # transient: never cached
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            data = json.dumps(to_entry(resp, accept=accept), ensure_ascii=False, indent=1)
            self._path(resp.url, accept).write_bytes(data.encode("utf-8"))
        except OSError:
            pass

    def _get(self, url: str, *, accept: str | None = None) -> Response:
        if self.mode in ("off", "cache"):
            hit = self._read(url, accept)
            if hit is not None:
                return hit
            if self.mode == "off" or self.inner is None:
                raise Offline(f"network is off and {clean_url(url)} is not cached")
        try:
            resp = self.inner.get(url, accept=accept)
            self.network_calls += 1
        except TransportError:
            if self.mode == "on":
                hit = self._read(url, accept)
                if hit is not None:
                    return hit
            raise
        self._write(resp, accept)
        return resp


class CassetteTransport(Transport):
    """Replays ``*.json`` cassette entries from one or more directories (recursively)."""

    def __init__(self, *dirs: Path, record_to: Path | None = None, inner: Transport | None = None):
        super().__init__()
        self.entries: dict[str, dict] = {}
        for d in dirs:
            for p in sorted(Path(d).rglob("*.json")):
                try:
                    e = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(e, dict) and e.get("schema") == SCHEMA:
                    accept = ((e.get("request") or {}).get("headers") or {}).get("Accept")
                    self.entries[cache_key("GET", e["request"]["url"], accept)] = e
        self.record_to, self.inner = record_to, inner
        self.misses: list[str] = []

    def _get(self, url: str, *, accept: str | None = None) -> Response:
        e = self.entries.get(cache_key("GET", url, accept))
        if e is not None:
            return from_entry(e)
        if self.record_to is not None and self.inner is not None:
            resp = self.inner.get(url, accept=accept)
            self.network_calls += 1
            entry = to_entry(resp, accept=accept, recorder="tools/record_reference_cassettes.py")
            host = (urllib.parse.urlsplit(url).hostname or "host").replace(".", "_")
            self.record_to.mkdir(parents=True, exist_ok=True)
            n = len(list(self.record_to.glob("*.json"))) + 1
            (self.record_to / f"{n:02d}-{host}-{entry['key'][:12]}.json").write_bytes(
                json.dumps(entry, ensure_ascii=False, indent=1).encode("utf-8"))
            self.entries[entry["key"]] = entry
            return resp
        self.misses.append(clean_url(url))
        raise CassetteMiss(f"no cassette entry for GET {clean_url(url)} (accept={accept!r}); record it with "
                           "tools/record_reference_cassettes.py")


def for_mode(repo: Path, network: str) -> Transport:
    """The transport :func:`repoatlas.references.resolve` uses for a network mode."""
    from repoatlas.paths import research_dir

    return CacheTransport(research_dir(Path(repo)) / "http-cache", mode=network)
