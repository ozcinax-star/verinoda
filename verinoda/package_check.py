"""Does a dependency exist in its registry, and does its name or record look like a squat?
(``verinoda check --deps --registry``, ``verinoda decide ask ... --registry``).

Opt-in and network-gated. The only thing sent anywhere is a package name, to its public registry, and only
when the network mode is ``on`` or ``cache`` (never by default): PyPI (the JSON API, and the Simple API for
the PEP 792 project status), npm (the registry document and the weekly download count), crates.io (the crate
API), Maven Central (``maven-metadata.xml``) and the Go module proxy (``@latest``). No credentials, no code,
no file contents. Never sent: a local, workspace, VCS or URL dependency (no registry holds it: skipped) and a
name behind a private registry the project configures (:func:`private_sources`: ``unknown``), so a private
name cannot leak to a public registry.

Per package, two kinds of signals:

* **registry facts**, ``observed`` at a time, each citing the URL it was read from: the registry answered 404
  (the name does not exist there - the main signal of a package name an assistant made up), the PyPI project
  status (quarantined, archived, deprecated), an npm security holding package (what npm publishes in place of
  a package removed for malware), a yanked or deprecated latest version, the first release date (age), the
  download count where the registry gives it in one cheap request (npm last week, crates.io last 90 days),
  known vulnerabilities PyPI lists;
* **name heuristics**, computed locally from a small bundled list of popular names
  (``data/popular_packages.json``): one or two edits from a popular name, the same letters with other
  separators, or a popular name plus a generic word (``requests-toolkit``, the style of names models
  invent). ``strong_inference`` at most: a near name may be a legitimate package.

Answers are cached on disk (``.verinoda/research/package-check.json``) with the time they were observed.
Network modes: ``off`` - nothing is sent, the registry facts are ``unknown`` (a cached answer is named,
not used); ``cache`` - a cached answer younger than the time to live (24 hours) is reused, else the registry is
asked; ``on`` - the registry is always asked, and a cached answer is shown only when it cannot be reached.
Timeouts, 5xx, 429 and unreadable answers are never cached and leave the package ``unknown``.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from verinoda.references.transport import CassetteMiss, RateLimited, TransportError

SCHEMA = "verinoda.package_check/1"
NETWORK_MODES = ("off", "cache", "on")
TTL_HOURS = 24.0
YOUNG_DAYS = 90            # first release less than this many days before the check
FEW_NPM_WEEKLY = 100       # npm downloads in the last week
FEW_CRATES_RECENT = 1000   # crates.io downloads in the last 90 days
TIMEOUT = 10.0
# the manifests' ecosystem name -> the registry asked
REGISTRY = {"python": "pypi", "pypi": "pypi", "npm": "npm", "cargo": "cargo", "go": "go", "golang": "go",
            "maven": "maven"}
REGISTRY_NAME = {"pypi": "PyPI", "npm": "npm", "cargo": "crates.io", "go": "the Go module proxy",
                 "maven": "Maven Central"}
FLAG = ("not_found", "quarantined", "security_holding")
CAUTION = ("no_release", "yanked", "deprecated", "archived", "young", "few_downloads", "known_vulnerabilities",
           "typo_of", "separator_confusable")
LOOKALIKE = ("typo_of", "separator_confusable")
# a near name used this much is a package of its own, not a squat: its look-alike signal is only weak
WIDELY_USED = {"last week": 50_000, "last 90 days": 500_000}
# words a made-up package name often adds to a real one
_GENERIC = {"py", "python", "python3", "js", "node", "lib", "libs", "utils", "util", "tools", "tool", "toolkit",
            "helper", "helpers", "sdk", "api", "client", "core", "easy", "simple", "plus", "pro", "extra",
            "extras", "ai", "wrapper", "kit", "lite", "fast", "official", "dev", "secure", "safe", "auth"}
_NODE_BUILTINS = {"assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants", "crypto",
                  "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http", "http2", "https",
                  "inspector", "module", "net", "os", "path", "perf_hooks", "process", "punycode", "querystring",
                  "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls", "trace_events", "tty",
                  "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib"}
_RUST_BUILTIN = {"std", "core", "alloc", "proc_macro", "proc-macro", "test", "crate", "self", "super"}
# Python import roots several distributions share: the import names no single distribution
_PY_NAMESPACES = {"google", "azure", "zope", "jaraco", "backports", "sphinxcontrib", "ruamel", "oslo", "plone",
                  "collective", "flufl", "pyannote", "opentelemetry"}


class PackageCheckError(ValueError):
    """A request that cannot be answered (bad network mode, no git for ``new``)."""


# -- names --------------------------------------------------------------------------------------------------

def registry_of(ecosystem: str) -> str | None:
    return REGISTRY.get(str(ecosystem or "").lower())


def normalize(registry: str, name: str) -> str:
    """The name as the registry compares it: PEP 503 for PyPI, ``-`` and ``_`` the same crate on crates.io,
    exact for npm (``JSONStream`` and ``jsonstream`` are two packages), Maven and Go."""
    n = str(name or "").strip()
    if registry == "pypi":
        return re.sub(r"[-_.]+", "-", n).lower()
    if registry == "cargo":
        return re.sub(r"[-_]", "-", n).lower()
    return n


def _fold(registry: str, name: str) -> str:
    """The name for comparing with the popular list (lower case everywhere)."""
    return normalize(registry, name).lower()


_DATA: dict | None = None


def _data() -> dict:
    global _DATA
    if _DATA is None:
        try:
            raw = json.loads((Path(__file__).parent / "data" / "popular_packages.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        _DATA = {"popular": {k: list(dict.fromkeys(_fold(k, x) for x in v)) for k, v in raw.items()
                             if isinstance(v, list)},
                 "legit": {k: {_fold(k, x) for x in v} for k, v in (raw.get("near_but_legitimate") or {}).items()
                           if isinstance(v, list)}}
    return _DATA


def popular(registry: str) -> list[str]:
    return _data()["popular"].get(registry, [])


def _distance(a: str, b: str, cap: int) -> int:
    """Optimal string alignment distance (insert, delete, substitute, swap two neighbours), ``cap + 1`` once
    it is known to exceed ``cap``."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


def _bare(name: str) -> str:
    """The name without ``-``, ``_`` and ``.`` (an npm scope and its ``/`` are kept: ``@types/express`` is not
    ``express``)."""
    return re.sub(r"[-_.]+", "", name)


def name_signals(registry: str, name: str) -> list[dict]:
    """Local, offline signals from the name alone (``strong_inference`` at most). Names on the bundled list,
    names it lists as legitimate near-names, and ``@types/`` packages of a popular name get none."""
    n = _fold(registry, name)
    pop = popular(registry)
    if not n or n in pop or n in _data()["legit"].get(registry, set()):
        return []
    if registry == "npm" and n.startswith("@types/"):
        inner = n[len("@types/"):]
        inner = "@" + inner.replace("__", "/") if "__" in inner else inner
        if inner in pop:
            return []
    out: list[dict] = []
    src = "the bundled list of popular names (verinoda/data/popular_packages.json)"
    bare = _bare(n)
    conf = [p for p in pop if _bare(p) == bare and p != n]
    if conf:
        out.append({"signal": "separator_confusable", "status": "strong_inference", "like": conf[0],
                    "claim": f"{name} is {conf[0]} with other separators (- _ .): a common squatting pattern",
                    "source": src})
    cap = 2 if len(n) >= 9 else 1 if len(n) >= 4 else 0
    if cap and not conf:
        near = sorted(((d, p) for p in pop if len(p) >= 4 and (d := _distance(n, p, cap)) <= cap))
        if near:
            d, p = near[0]
            out.append({"signal": "typo_of", "status": "strong_inference", "like": p, "edits": d,
                        "claim": f"{name} is {d} edit(s) from the popular package {p}: a typo or a squat of it",
                        "source": src})
    parts = [x for x in re.split(r"[-_./@]+", n) if x]
    if len(parts) >= 2 and not out:
        for i in range(len(parts)):
            rest = parts[:i] + parts[i + 1:]
            core = "-".join(rest) if registry == "pypi" else None
            if parts[i] in _GENERIC and rest and ((core and core in pop) or any(
                    _bare(p) == "".join(rest) for p in pop)):
                like = core if core and core in pop else next(p for p in pop if _bare(p) == "".join(rest))
                out.append({"signal": "hallucination_style", "status": "weak_inference", "like": like,
                            "claim": f"{name} is the popular name {like} plus the generic word {parts[i]!r}, the "
                                     "shape of names models invent; many such packages are legitimate",
                            "source": src})
                break
    return out


def skip_reason(registry: str, name: str, imported: bool = True) -> str | None:
    """Why a name is not asked about at all (an import of the standard library or a Node built-in, a relative
    or empty name). A declared name is always asked about: ``typing`` in a manifest is the PyPI backport."""
    n = str(name or "").strip()
    if not n or n.startswith((".", "/")):
        return "not a package name"
    if imported and registry == "pypi" and n.split(".")[0] in getattr(sys, "stdlib_module_names", ()):
        return "a module of the Python standard library"
    if imported and registry == "npm" and (n.startswith("node:") or n in _NODE_BUILTINS):
        return "a Node.js built-in module"
    if registry == "go" and "." not in n.split("/")[0]:
        return "a package of the Go standard library (no dot in the first path element)"
    if imported and registry == "cargo" and n in _RUST_BUILTIN:
        return "a crate built into Rust or a path keyword"
    return None


# -- registry lookups ---------------------------------------------------------------------------------------

def _get(transport, url: str, accept: str | None = None):
    """(response, None) or (None, failure dict); test transports that miss raise."""
    try:
        return transport.get(url, accept=accept), None
    except CassetteMiss:
        raise
    except RateLimited as exc:
        return None, {"reason": "rate_limited", "error": str(exc)[:300]}
    except TransportError as exc:
        return None, {"reason": "unreachable", "error": str(exc)[:300]}
    except (OSError, TimeoutError) as exc:   # a transport that does not wrap its errors
        return None, {"reason": "unreachable", "error": f"{type(exc).__name__}: {exc}"[:300]}


def _status(resp) -> str:
    if 200 <= resp.status < 300:
        return "ok"
    if resp.status in (404, 410):
        return "not_found"
    if resp.status == 429:
        return "rate_limited"
    return "unreachable"


def _json(resp):
    try:
        data = json.loads(resp.body.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


def _days(iso: str | None, now: float) -> int | None:
    if not iso or not isinstance(iso, str):
        return None
    try:
        from datetime import datetime, timezone

        t = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return int((now - t.timestamp()) // 86400)
    except ValueError:
        return None


def _answer(registry: str, name: str, url: str, resp, exists, facts: dict | None = None,
            observed: list[dict] | None = None) -> dict:
    return {"registry": registry, "name": name, "url": url, "exists": exists,
            "observed_at": getattr(resp, "retrieved_at", None) or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "http_status": getattr(resp, "status", None), "facts": facts or {}, "observed": observed or []}


def _fail(registry: str, name: str, url: str, reason: str, error: str) -> dict:
    return {"registry": registry, "name": name, "url": url, "exists": None, "failure": reason, "error": error}


def _bad(registry: str, name: str, url: str, what: str) -> dict:
    return _fail(registry, name, url, "bad_response", f"{REGISTRY_NAME[registry]} answered {what}")


def _not_found(registry: str, name: str, url: str, resp) -> dict:
    return _answer(registry, name, url, resp, False, observed=[{
        "signal": "not_found", "url": url,
        "claim": f"{REGISTRY_NAME[registry]} has no package named {name} (HTTP {resp.status})"}])


def _lookup_pypi(transport, name: str, now: float) -> dict:
    url = f"https://pypi.org/pypi/{urllib.parse.quote(name, safe='')}/json"
    simple_url = f"https://pypi.org/simple/{urllib.parse.quote(normalize('pypi', name), safe='')}/"
    resp, err = _get(transport, url)
    if err:
        return _fail("pypi", name, url, err["reason"], err["error"])
    st = _status(resp)
    if st not in ("ok", "not_found"):
        return _fail("pypi", name, url, st, f"PyPI answered HTTP {resp.status}")
    # the Simple API carries the PEP 792 project status (a quarantined project may answer 404 on the JSON API)
    sresp, serr = _get(transport, simple_url, accept="application/vnd.pypi.simple.v1+json")
    project_status = None
    if not serr and _status(sresp) == "ok":
        sdata = _json(sresp) or {}
        ps = sdata.get("project-status")
        project_status = ps.get("status") if isinstance(ps, dict) else None
    if st == "not_found":
        if project_status and project_status != "active":
            return _answer("pypi", name, url, resp, True, {"project_status": project_status},
                           _status_signals(project_status, name, simple_url))
        if not serr and _status(sresp) == "ok":
            # the project exists (the Simple API knows it) but the JSON API has no release to show
            return _answer("pypi", name, simple_url, sresp, True,
                           {"project_status": project_status, "installable_release": False},
                           [{"signal": "no_release", "url": simple_url,
                             "claim": f"PyPI knows the project {name} but the JSON API answered {resp.status}: it has "
                                      "no installable release"}])
        if serr or _status(sresp) not in ("ok", "not_found"):
            return _fail("pypi", name, url, "unreachable",
                         "the JSON API answered 404 but the Simple API could not be read to confirm it")
        return _not_found("pypi", name, url, resp)
    if st != "ok":
        return _fail("pypi", name, url, st, f"PyPI answered HTTP {resp.status}")
    data = _json(resp)
    if data is None or not isinstance(data.get("info"), dict):
        return _bad("pypi", name, url, "with JSON that is not a project")
    info = data["info"]
    releases = data.get("releases") if isinstance(data.get("releases"), dict) else {}
    times = []
    for files in releases.values():
        for f in files if isinstance(files, list) else []:
            t = isinstance(f, dict) and (f.get("upload_time_iso_8601") or f.get("upload_time"))
            if isinstance(t, str):
                times.append(t)
    first = min(times) if times else None
    latest = info.get("version") if isinstance(info.get("version"), str) else None
    latest_files = releases.get(latest) if latest else None
    yanked = bool(latest_files) and all(isinstance(f, dict) and f.get("yanked") for f in latest_files)
    vulns = data.get("vulnerabilities") if isinstance(data.get("vulnerabilities"), list) else []
    facts = {"latest": latest, "versions": len(releases), "first_release": first, "age_days": _days(first, now),
             "latest_yanked": yanked, "project_status": project_status, "known_vulnerabilities": len(vulns)}
    obs = _status_signals(project_status, name, simple_url)
    if yanked:
        why = next((f.get("yanked_reason") for f in latest_files if isinstance(f, dict) and f.get("yanked_reason")),
                   None)
        obs.append({"signal": "yanked", "url": url, "claim": f"the latest release of {name} ({latest}) is yanked"
                                                             + (f": {why}" if why else "")})
    if vulns:
        ids = [v.get("id") for v in vulns if isinstance(v, dict) and v.get("id")][:5]
        obs.append({"signal": "known_vulnerabilities", "url": url,
                    "claim": f"PyPI lists {len(vulns)} known vulnerabilit(ies) for {name} {latest}"
                             + (f" ({', '.join(ids)})" if ids else "")})
    return _answer("pypi", name, url, resp, True, facts, obs)


def _status_signals(status: str | None, name: str, url: str) -> list[dict]:
    if status == "quarantined":
        return [{"signal": "quarantined", "url": url,
                 "claim": f"PyPI marks {name} as quarantined (held by PyPI, typically on a malware report; not "
                          "installable)"}]
    if status in ("archived", "deprecated"):
        return [{"signal": status, "url": url, "claim": f"PyPI marks {name} as {status} by its maintainers"}]
    return []


def _lookup_npm(transport, name: str, now: float) -> dict:
    url = "https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@").replace("/", "%2F")
    resp, err = _get(transport, url, accept="application/json")
    if err:
        return _fail("npm", name, url, err["reason"], err["error"])
    st = _status(resp)
    if st == "not_found":
        return _not_found("npm", name, url, resp)
    if st != "ok":
        return _fail("npm", name, url, st, f"npm answered HTTP {resp.status}")
    data = _json(resp)
    if data is None or not isinstance(data.get("versions", {}), dict):
        return _bad("npm", name, url, "with JSON that is not a package document")
    versions = data.get("versions") or {}
    tags = data.get("dist-tags") if isinstance(data.get("dist-tags"), dict) else {}
    tm = data.get("time") if isinstance(data.get("time"), dict) else {}
    latest = tags.get("latest") if isinstance(tags.get("latest"), str) else None
    created = tm.get("created") if isinstance(tm.get("created"), str) else None
    lv = versions.get(latest) if latest else None
    deprecated = lv.get("deprecated") if isinstance(lv, dict) and isinstance(lv.get("deprecated"), str) else None
    descr = str(data.get("description") or (lv or {}).get("description") or "")
    holding = bool(latest and re.fullmatch(r"0\.0\.\d+-security(\.\d+)?", latest)) or \
        "security holding package" in descr.lower()
    unpublished = isinstance(tm.get("unpublished"), dict) and not versions
    facts = {"latest": latest, "versions": len(versions), "first_release": created, "age_days": _days(created, now),
             "deprecated": deprecated, "security_holding": holding, "unpublished": unpublished}
    obs = []
    if holding:
        obs.append({"signal": "security_holding", "url": url,
                    "claim": f"npm replaced {name} with a security holding package ({latest}): npm does this for a "
                             "package removed for malicious code"})
    if unpublished:
        obs.append({"signal": "not_found", "url": url, "claim": f"every version of {name} was unpublished from npm"})
    if deprecated:
        obs.append({"signal": "deprecated", "url": url,
                    "claim": f"the latest version of {name} ({latest}) is deprecated: {deprecated[:200]}"})
    if not holding and not unpublished:
        durl = "https://api.npmjs.org/downloads/point/last-week/" + urllib.parse.quote(name, safe="@/")
        dresp, derr = _get(transport, durl)
        dd = _json(dresp) if not derr and _status(dresp) == "ok" else None
        if dd is not None and isinstance(dd.get("downloads"), int):
            facts["downloads"] = {"count": dd["downloads"], "period": "last week", "url": durl}
    return _answer("npm", name, url, resp, not unpublished, facts, obs)


def _lookup_cargo(transport, name: str, now: float) -> dict:
    url = f"https://crates.io/api/v1/crates/{urllib.parse.quote(name, safe='')}"
    resp, err = _get(transport, url, accept="application/json")
    if err:
        return _fail("cargo", name, url, err["reason"], err["error"])
    st = _status(resp)
    if st == "not_found":
        return _not_found("cargo", name, url, resp)
    if st != "ok":
        return _fail("cargo", name, url, st, f"crates.io answered HTTP {resp.status}")
    data = _json(resp)
    crate = data.get("crate") if data else None
    if not isinstance(crate, dict):
        return _bad("cargo", name, url, "with JSON that is not a crate")
    latest = crate.get("max_stable_version") or crate.get("newest_version") or crate.get("max_version")
    vers = [v for v in (data.get("versions") or []) if isinstance(v, dict)]
    lv = next((v for v in vers if v.get("num") == latest), None)
    yanked = bool(lv and lv.get("yanked")) or (bool(vers) and all(v.get("yanked") for v in vers))
    facts = {"latest": latest, "versions": crate.get("num_versions") or len(vers),
             "first_release": crate.get("created_at"), "age_days": _days(crate.get("created_at"), now),
             "latest_yanked": yanked}
    if isinstance(crate.get("recent_downloads"), int):
        facts["downloads"] = {"count": crate["recent_downloads"], "period": "last 90 days", "url": url}
    obs = []
    if yanked:
        obs.append({"signal": "yanked", "url": url, "claim": f"the latest version of {name} ({latest}) is yanked"
                    if lv and lv.get("yanked") else f"every version of {name} is yanked"})
    return _answer("cargo", name, url, resp, True, facts, obs)


def _go_escape(module: str) -> str:
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), module)


def _lookup_go(transport, name: str, now: float) -> dict:
    url = f"https://proxy.golang.org/{_go_escape(name)}/@latest"
    resp, err = _get(transport, url)
    if err:
        return _fail("go", name, url, err["reason"], err["error"])
    st = _status(resp)
    if st == "not_found":
        return _not_found("go", name, url, resp)
    if st != "ok":
        return _fail("go", name, url, st, f"the Go proxy answered HTTP {resp.status}")
    data = _json(resp)
    if data is None or not isinstance(data.get("Version"), str):
        return _bad("go", name, url, "with JSON that is no version")
    return _answer("go", name, url, resp, True, {"latest": data["Version"], "latest_time": data.get("Time"),
                                                 "latest_age_days": _days(data.get("Time"), now)})


def _lookup_maven(transport, name: str, now: float) -> dict:
    group, _, artifact = name.partition(":")
    if not group or not artifact or "/" in name:
        return _fail("maven", name, "", "bad_name", f"{name} is not group:artifact")
    url = f"https://repo1.maven.org/maven2/{group.replace('.', '/')}/{artifact}/maven-metadata.xml"
    resp, err = _get(transport, url)
    if err:
        return _fail("maven", name, url, err["reason"], err["error"])
    st = _status(resp)
    if st == "not_found":
        res = _not_found("maven", name, url, resp)
        res["observed"][0]["claim"] = (f"Maven Central has no artifact {name} (HTTP {resp.status}); a build may take "
                                       "it from another repository")
        return res
    if st != "ok":
        return _fail("maven", name, url, st, f"Maven Central answered HTTP {resp.status}")
    try:
        root = ET.fromstring(resp.body)
    except ET.ParseError:
        return _bad("maven", name, url, "with XML that cannot be read")
    if root.tag != "metadata":   # an HTML error or portal page answered with 200 is no artifact
        return _bad("maven", name, url, f"with a <{root.tag[:40]}> document, not Maven metadata")
    vs =[v.text for v in root.iter("version") if v.text]
    latest = root.findtext("versioning/release") or root.findtext("versioning/latest") or (vs[-1] if vs else None)
    upd = root.findtext("versioning/lastUpdated")
    return _answer("maven", name, url, resp, True, {"latest": latest, "versions": len(vs), "last_updated": upd})


LOOKUPS = {"pypi": _lookup_pypi, "npm": _lookup_npm, "cargo": _lookup_cargo, "go": _lookup_go,
           "maven": _lookup_maven}


# -- cache --------------------------------------------------------------------------------------------------

def cache_path(repo: Path) -> Path:
    from verinoda.paths import research_dir

    return research_dir(Path(repo)) / "package-check.json"


def _load_cache(path: Path) -> dict:
    """The cache entries (a file that cannot be read, or is not this schema, holds none)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ent = data.get("entries") if isinstance(data, dict) and data.get("schema") == SCHEMA else None
    return ent if isinstance(ent, dict) else {}


def _valid_entry(hit) -> dict | None:
    """A cache entry as it was written, or None (a hand-edited or damaged entry is a miss, never a crash)."""
    if not isinstance(hit, dict):
        return None
    ans = hit.get("answer")
    try:
        epoch = float(hit.get("epoch"))
    except (TypeError, ValueError):
        return None
    if epoch != epoch or epoch in (float("inf"), float("-inf")):
        return None
    if not isinstance(ans, dict) or ans.get("registry") not in REGISTRY_NAME or not isinstance(ans.get("name"), str) \
            or not isinstance(ans.get("exists"), bool) or not isinstance(ans.get("url"), str) \
            or not isinstance(ans.get("facts", {}), dict) or not isinstance(ans.get("observed", []), list) \
            or not all(isinstance(o, dict) and isinstance(o.get("signal"), str) and isinstance(o.get("claim"), str)
                       for o in ans.get("observed", [])):
        return None
    return {**hit, "epoch": epoch}


def _save_cache(path: Path, new: dict) -> None:
    """Merge ``new`` into the entries on disk now (another run may have written since this one read) and
    replace the file in one step (a temporary file and ``os.replace``), so a reader never sees half a file."""
    import os
    import tempfile

    if not new:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = {k: v for k, v in _load_cache(path).items() if _valid_entry(v)}
        entries.update(new)
        fd, tmp = tempfile.mkstemp(prefix=".package-check.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps({"schema": SCHEMA, "entries": entries}, ensure_ascii=False, indent=1,
                                sort_keys=True).encode("utf-8"))
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:   # Windows: a reader holds the file for a moment
                time.sleep(0.05 * (attempt + 1))
        os.unlink(tmp)
    except OSError:
        pass


# -- private registries -------------------------------------------------------------------------------------

def _rel_at(repo: Path, p: Path, line: int | None = None) -> str:
    try:
        rel = p.relative_to(repo).as_posix()
    except ValueError:
        rel = str(p)
    return f"{rel}:{line}" if line else rel


def private_sources(repo: Path, env: dict | None = None) -> dict:
    """The package sources the project configures besides the public registries, each with where it is set:
    ``npm`` (``.npmrc`` ``@scope:registry=`` per scope, ``registry=`` for all), ``pypi`` (``--index-url`` /
    ``--extra-index-url`` / ``--find-links`` in requirements files, ``pip.conf`` / ``pip.ini``, uv, Poetry and
    PDM sources, the ``PIP_*`` / ``UV_*`` index variables), ``cargo`` (``.cargo/config.toml`` registries and a
    replaced crates.io), ``go`` (``GOPRIVATE`` / ``GONOPROXY`` patterns from the environment, read lazily)."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - py3.10
        import tomli as tomllib  # type: ignore[no-redef]

    repo = Path(repo)
    env = os_environ() if env is None else env
    out: dict = {"npm": {"scopes": {}, "all": None}, "pypi": [], "cargo": {"registries": {}, "replaced": None},
                 "go": None}

    def lines(p: Path) -> list[str]:
        try:
            return p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

    npmrc = repo / ".npmrc"
    for i, ln in enumerate(lines(npmrc), 1):
        s = ln.split("#")[0].split(";")[0].strip()
        m = re.match(r"^(@[\w.-]+):registry\s*=\s*(\S+)", s)
        if m:
            out["npm"]["scopes"][m.group(1).lower()] = f"{_rel_at(repo, npmrc, i)} ({m.group(2)})"
            continue
        m = re.match(r"^registry\s*=\s*(\S+)", s)
        if m and "registry.npmjs.org" not in m.group(1):
            out["npm"]["all"] = f"{_rel_at(repo, npmrc, i)} ({m.group(1)})"
    reg_env = env.get("npm_config_registry") or env.get("NPM_CONFIG_REGISTRY")
    if reg_env and "registry.npmjs.org" not in reg_env:
        out["npm"]["all"] = f"the environment (npm_config_registry={reg_env})"

    reqs = sorted(list(repo.glob("requirements*.txt")) + list(repo.glob("requirements/*.txt")))
    for p in reqs:
        for i, ln in enumerate(lines(p), 1):
            m = re.match(r"^\s*(-i|--index-url|--extra-index-url|-f|--find-links)(?:\s+|=)(\S+)", ln)
            if m:
                out["pypi"].append(f"{_rel_at(repo, p, i)} ({m.group(1)} {m.group(2)})")
    for conf in (repo / "pip.conf", repo / "pip.ini"):
        for i, ln in enumerate(lines(conf), 1):
            m = re.match(r"^\s*(index-url|extra-index-url|find-links)\s*=\s*(\S+)", ln)
            if m:
                out["pypi"].append(f"{_rel_at(repo, conf, i)} ({m.group(1)} = {m.group(2)})")
    for cfg_name, prefix in (("pyproject.toml", ("tool",)), ("uv.toml", ())):
        p = repo / cfg_name
        if not p.is_file():
            continue
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - an unreadable file configures nothing here
            continue
        tool = (data.get("tool") or {}) if prefix else {"uv": data}
        uv = tool.get("uv") or {}
        if uv.get("index") or uv.get("index-url") or uv.get("extra-index-url") or uv.get("find-links"):
            out["pypi"].append(f"{cfg_name} ({'[tool.uv]' if prefix else 'uv.toml'} package index)")
        if (tool.get("poetry") or {}).get("source"):
            out["pypi"].append(f"{cfg_name} ([[tool.poetry.source]])")
        if (tool.get("pdm") or {}).get("source"):
            out["pypi"].append(f"{cfg_name} ([[tool.pdm.source]])")
    for var in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL",
                "UV_INDEX", "UV_DEFAULT_INDEX"):
        if env.get(var):
            out["pypi"].append(f"the environment ({var})")

    for p in (repo / ".cargo" / "config.toml", repo / ".cargo" / "config"):
        if not p.is_file():
            continue
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        for name in (data.get("registries") or {}):
            out["cargo"]["registries"][str(name)] = _rel_at(repo, p)
        rep = ((data.get("source") or {}).get("crates-io") or {}).get("replace-with")
        if rep:
            out["cargo"]["replaced"] = f"{_rel_at(repo, p)} (crates-io replaced with {rep})"
    return out


def os_environ() -> dict:
    import os

    return dict(os.environ)


def _go_private(env: dict) -> list[tuple[str, str]]:
    """``GOPRIVATE`` / ``GONOPROXY`` patterns: the environment, else ``go env`` when Go is installed."""
    import shutil
    import subprocess

    pats: list[tuple[str, str]] = []
    vals = {v: env.get(v) for v in ("GOPRIVATE", "GONOPROXY")}
    if not any(vals.values()) and shutil.which("go"):
        try:
            r = subprocess.run(["go", "env", "GOPRIVATE", "GONOPROXY"], capture_output=True, text=True, timeout=10)
            got = r.stdout.splitlines() if r.returncode == 0 else []
            vals = {"GOPRIVATE": got[0] if got else "", "GONOPROXY": got[1] if len(got) > 1 else ""}
        except (OSError, subprocess.SubprocessError):
            pass
    for var, val in vals.items():
        for pat in str(val or "").split(","):
            if pat.strip():
                pats.append((pat.strip(), var))
    return pats


def _go_matches(module: str, pattern: str) -> bool:
    """Go's rule: the pattern matches a path prefix with as many elements as the pattern has."""
    import fnmatch

    n = pattern.count("/") + 1
    return fnmatch.fnmatchcase("/".join(module.split("/")[:n]), pattern)


def _private_reason(reg: str, name: str, private: dict, env: dict) -> str | None:
    if reg == "npm":
        npm = private.get("npm") or {}
        scope = name.split("/")[0].lower() if name.startswith("@") else None
        if scope and scope in (npm.get("scopes") or {}):
            return f"the scope {scope} is installed from a private registry ({npm['scopes'][scope]})"
        if npm.get("all"):
            return f"npm packages are installed from a configured registry ({npm['all']})"
    if reg == "pypi" and private.get("pypi"):
        src = private["pypi"]
        return (f"the project configures another package index ({src[0]}" + (f" and {len(src) - 1} more" if
                len(src) > 1 else "") + "): a name may be a private package, and sending it to PyPI would leak it")
    if reg == "cargo" and (private.get("cargo") or {}).get("replaced"):
        return f"crates.io is replaced ({private['cargo']['replaced']})"
    if reg == "go":
        if private.get("go") is None:
            private["go"] = _go_private(env)
        for pat, var in private["go"]:
            if _go_matches(name, pat):
                return f"the module matches {var} pattern {pat}: a private module"
    return None


# -- the check ----------------------------------------------------------------------------------------------

def _counts(s: dict) -> bool:
    return s.get("status") != "weak_inference"


def _verdict(signals: list[dict], exists) -> str:
    kinds = {s["signal"] for s in signals if _counts(s)}
    if kinds & set(FLAG):
        return "flagged"
    if kinds & set(CAUTION):
        return "caution"
    return "unknown" if exists is None else "ok"


def _record_signals(ans: dict, now: float) -> list[dict]:
    """The observed registry facts as signals, plus age and downloads judged against the thresholds."""
    when = ans.get("observed_at")
    out = [{**o, "status": "observed", "observed_at": when} for o in ans.get("observed") or []]
    f = ans.get("facts") or {}
    reg = REGISTRY_NAME.get(ans.get("registry"), str(ans.get("registry")))
    if isinstance(f.get("age_days"), int) and f["age_days"] < YOUNG_DAYS:
        out.append({"signal": "young", "status": "observed", "observed_at": when, "url": ans.get("url"),
                    "claim": f"{ans.get('name')} was first published on {reg} {f['age_days']} day(s) before the "
                             f"check ({f.get('first_release')})"})
    dl = f.get("downloads")
    if isinstance(dl, dict) and isinstance(dl.get("count"), int):
        few = FEW_NPM_WEEKLY if dl.get("period") == "last week" else FEW_CRATES_RECENT
        if dl["count"] < few:
            out.append({"signal": "few_downloads", "status": "observed", "observed_at": when, "url": dl.get("url"),
                        "claim": f"{ans.get('name')} was downloaded {dl['count']} time(s) in the {dl.get('period')} "
                                 f"(under {few})"})
    return out


def _weaken_if_widely_used(signals: list[dict], facts: dict) -> None:
    """A look-alike name the registry shows widely used is a package of its own: weak_inference, no caution."""
    dl = facts.get("downloads") if isinstance(facts, dict) else None
    if not (isinstance(dl, dict) and isinstance(dl.get("count"), int)):
        return
    floor = WIDELY_USED.get(dl.get("period"))
    if floor is None or dl["count"] < floor:
        return
    for s in signals:
        if s["signal"] in LOOKALIKE:
            s["status"] = "weak_inference"
            s["claim"] += f" - but it was downloaded {dl['count']:,} times in the {dl['period']}, so it is likely " \
                          "a package of its own"


def check_packages(repo: Path, packages: list[dict], *, network: str = "off", transport=None,
                   now: float | None = None, ttl_hours: float = TTL_HOURS, cache_file: Path | None = None,
                   private: dict | None = None, env: dict | None = None) -> dict:
    """Check ``packages`` against their registries (see the module). A package is ``{"ecosystem", "name"
    (the name to look up, as declared), "why"?, "evidence"?, "imported"?, "skip"? (a reason: a local, VCS or
    URL source - never asked), "hold"? (a reason: a private source - never sent, unknown)}``. ``transport``:
    anything with ``get(url, accept=None)`` returning a transport ``Response``; the live, SSRF-guarded one when
    None and the network mode needs it. ``private``: :func:`private_sources` (read from ``repo`` when None)."""
    if network not in NETWORK_MODES:
        raise PackageCheckError(f"network mode must be off, cache or on, not {network!r}")
    now = time.time() if now is None else now
    env = os_environ() if env is None else env
    private = private_sources(repo, env) if private is None else private
    path = cache_file or cache_path(repo)
    cache = _load_cache(path)
    written: dict = {}
    ttl = ttl_hours * 3600
    results: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    for p in packages:
        reg = registry_of(p.get("ecosystem"))
        name = str(p.get("name") or "").strip()
        base = {"ecosystem": p.get("ecosystem"), "name": name, **({"why": p["why"]} if p.get("why") else {}),
                **({"evidence": p["evidence"]} if p.get("evidence") else {}),
                **({"declared_as": p["declared_as"]} if p.get("declared_as") else {})}
        if reg is None:
            results.append({**base, "verdict": "unknown", "signals": [],
                            "unknown": f"no registry lookup for {p.get('ecosystem')} dependencies"})
            continue
        skip = p.get("skip") or skip_reason(reg, name, imported=bool(p.get("imported")))
        if skip:
            results.append({**base, "registry": reg, "verdict": "skipped", "signals": [], "skipped": skip})
            continue
        hold = p.get("hold") or _private_reason(reg, name, private, env)
        if hold:
            results.append({**base, "registry": reg, "exists": None, "verdict": "unknown", "signals": [],
                            "unknown": f"not sent: {hold}",
                            "next_step": "check the name against the registry the project installs it from"})
            continue
        norm = normalize(reg, name)
        if (reg, norm) in seen:   # one question per package; the second mention keeps its own evidence
            results.append({**seen[(reg, norm)], **base})
            continue
        key = f"{reg}:{norm}"
        hit = _valid_entry(cache.get(key))
        ans = None
        note = None
        if network == "off":
            note = (f"network is off, so {REGISTRY_NAME[reg]} was not asked" +
                    (f" (a cached answer from {hit['answer'].get('observed_at')} exists: --network cache reuses it)"
                     if hit else ""))
        elif network == "cache" and hit and now - hit["epoch"] < ttl:
            ans = {**hit["answer"], "from_cache": True}
        else:
            if transport is None:
                from verinoda.paths import http_cache_dir
                from verinoda.references.transport import LiveTransport

                transport = LiveTransport(timeout=TIMEOUT, state_path=http_cache_dir(Path(repo)) / "hosts.json")
            got = LOOKUPS[reg](transport, name, now)
            if got.get("failure"):
                note = (f"{REGISTRY_NAME[reg]} could not be read ({got['failure']}: {got.get('error')})")
                if hit:
                    ans = {**hit["answer"], "from_cache": True}
                    note += f"; the answer cached at {hit['answer'].get('observed_at')} is shown"
            else:
                ans = {**got, "from_cache": False}
                written[key] = cache[key] = {"observed_at": got.get("observed_at"), "epoch": now, "answer": got}
        signals = _record_signals(ans, now) if ans else []
        signals += name_signals(reg, name)
        if ans:
            _weaken_if_widely_used(signals, ans.get("facts") or {})
        exists = ans.get("exists") if ans else None
        res = {**base, "registry": reg, "exists": exists, "verdict": _verdict(signals, exists), "signals": signals}
        if ans:
            res.update({"url": ans.get("url"), "observed_at": ans.get("observed_at"),
                        "from_cache": ans.get("from_cache", False), "facts": ans.get("facts") or {}})
        if note:
            res["unknown"] = note
        res["next_step"] = _next(res, network)
        seen[(reg, norm)] = {k: v for k, v in res.items() if k not in ("why", "evidence", "declared_as")}
        results.append(res)
    _save_cache(path, written)
    summary = {v: sum(1 for r in results if r["verdict"] == v)
               for v in ("flagged", "caution", "ok", "unknown", "skipped")}
    limits = [
        "only package names are sent, and only to the public registry of their ecosystem; names the project "
        "installs from a private index, scope, registry or Go module pattern it configures are not sent "
        "(unknown); a source configured outside the project (a user-level pip.conf or .npmrc) is not read",
        "local, workspace, VCS and URL dependencies are skipped: no registry holds them",
        "age, downloads and malware signals are what the registry shows without an account; no registry here "
        "scans packages for malicious code, so a package with no signal is not shown to be safe",
        "name signals compare with a small bundled list of popular names: strong_inference at most, and weak "
        "when the registry shows the near name widely used",
        "PyPI download counts are not read (its JSON API has none; pypistats.org rate-limits anonymous use); Go "
        "and Maven Central give no first release date in one request; Maven Central is the only Maven "
        "repository asked",
    ]
    return {"kind": "package_check", "network": network, "packages": results, "summary": summary,
            "cache": str(path), "limits": limits}


def _next(res: dict, network: str) -> str:
    v, name = res["verdict"], res["name"]
    kinds = {s["signal"] for s in res["signals"]}
    if "not_found" in kinds:
        return (f"do not install {name}: check the name (a typo, or a package an assistant made up), find the real "
                "package in the registry or the library's documentation, then fix the manifest or the import")
    if kinds & {"quarantined", "security_holding"}:
        return f"remove {name}: the registry took it down; check whether it was ever installed here"
    if res.get("exists") is None and network == "off":
        return "run again with --network on (sends only the package names to their registries)"
    if v == "caution":
        return (f"compare {name} with the package you meant (its page, its source repository, its maintainers) "
                "before installing it")
    if res.get("exists") is None:
        return "run again later, or check the package page by hand"
    return "no registry signal; still check that it is the package you meant"


# -- what the change adds -----------------------------------------------------------------------------------

class NotGitError(PackageCheckError):
    """The project is not in a git work tree: there is no change to read."""


_MANIFEST_RX = re.compile(r"^(package\.json|pyproject\.toml|go\.mod|Cargo\.toml|requirements.*\.txt|setup\.py|"
                          r"setup\.cfg|pom\.xml|.*\.gradle(\.kts)?|.*\.versions\.toml|gradle\.properties)$")


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    import subprocess

    r = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=off", *args], input=data,
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        raise PackageCheckError(f"git {' '.join(args[:2])} failed: "
                                f"{r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return r.stdout


def resolve_rev(repo: Path, rev: str) -> str:
    """``rev`` as a commit of ``repo``'s work tree. :class:`NotGitError` outside one, :class:`PackageCheckError`
    for anything that is not one commit (a revision never starts with ``-``: it cannot be read as an option)."""
    rev = (rev or "HEAD").strip()
    try:
        inside = _git(repo, "rev-parse", "--is-inside-work-tree").strip() == b"true"
    except (PackageCheckError, OSError):
        inside = False
    if not inside:
        raise NotGitError(f"{repo} is not a git work tree")
    if not rev or rev.startswith("-") or any(c in rev for c in "\0\n\r"):
        raise PackageCheckError(f"--diff {rev!r}: not a revision")
    try:
        sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").decode().strip()
    except PackageCheckError:
        sha = ""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise PackageCheckError(f"--diff {rev}: not a commit of this repository")
    return sha


def declared_at(repo: Path, rev: str) -> dict[str, set[str]]:
    """The dependency names each manifest declared at ``rev``, read the way the working tree is read
    (:func:`verinoda.guards.declared_dependencies` over the manifests of that commit, written to a temporary
    folder): ``{path: {name, ...}}``."""
    import shutil
    import tempfile

    from verinoda.guards import declared_dependencies

    sha = resolve_rev(repo, rev)
    names = [n for n in _git(repo, "ls-tree", "-r", "--name-only", "-z", sha).decode("utf-8", "replace").split("\0")
             if n and _MANIFEST_RX.match(n.rsplit("/", 1)[-1]) and "node_modules/" not in n]
    prefix = _git(repo, "rev-parse", "--show-prefix").decode("utf-8", "replace").strip()
    tmp = Path(tempfile.mkdtemp(prefix="verinoda-manifests-"))
    try:
        for rel in names:
            try:
                body = _git(repo, "cat-file", "blob", f"{sha}:{prefix}{rel}")
            except PackageCheckError:
                continue
            dst = tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(body)
        items = declared_dependencies(tmp, names).get("items") or []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out: dict[str, set[str]] = {}
    for it in items:
        out.setdefault(str(it.get("path")), set()).add(str(it.get("name") or "").lower())
    return out


def added_dependencies(repo: Path, items: list[dict], rev: str = "HEAD") -> list[dict]:
    """The declarations (``guards.declared_dependencies`` items) whose name the same manifest did not declare at
    ``rev``: a new dependency, not a version change, and not a name the old file only mentioned (a comment, a
    script). A manifest that did not exist at ``rev`` adds all of its own. :class:`NotGitError` outside a git
    work tree, :class:`PackageCheckError` for a revision that is not a commit."""
    before = declared_at(Path(repo), rev)
    return [it for it in items
            if str(it.get("name") or "").lower() not in before.get(str(it.get("path")), set())]


# -- where a declaration comes from -------------------------------------------------------------------------

_NPM_NOT_REGISTRY = ("workspace:", "file:", "link:", "portal:", "git+", "git:", "git@", "github:", "gitlab:",
                     "bitbucket:", "gist:", "http://", "https://", "patch:", "exec:", "./", "../", "/", "~")


def _npm_source(name: str, spec: str) -> dict:
    s = (spec or "").strip()
    if s.startswith("npm:"):
        real = s[4:]
        at = real.rfind("@")
        if at > 0:
            real = real[:at]
        return {"name": real, "declared_as": name} if real else {"skip": f"an npm alias with no package ({s})"}
    if s.startswith(_NPM_NOT_REGISTRY) or re.fullmatch(r"[\w.-]+/[\w.-]+(#.*)?", s):
        return {"skip": f"a local, workspace, VCS or URL source ({s[:80]}): no registry holds it"}
    return {}


def _cargo_source(name: str, spec: str, table: str | None) -> dict:
    t = None
    for raw in (table, spec):
        if raw and raw.strip().startswith("{"):
            try:
                t = json.loads(raw)
            except ValueError:
                t = None
            if isinstance(t, dict):
                break
    if not isinstance(t, dict):
        return {}
    if t.get("path") or t.get("git"):
        return {"skip": f"a {'path' if t.get('path') else 'git'} dependency ({t.get('path') or t.get('git')}): no "
                        "registry holds it"}
    if t.get("workspace") is True:
        return {"skip": "inherited from the workspace's [workspace.dependencies] (not read here)"}
    if t.get("registry") or t.get("registry-index"):
        return {"hold": f"from the registry {t.get('registry') or t.get('registry-index')}, not crates.io"}
    if isinstance(t.get("package"), str) and t["package"] != name:
        return {"name": t["package"], "declared_as": name}
    return {}


def _python_source(name: str, spec: str, uv_sources: dict) -> dict:
    s = (spec or "").strip()
    if s.startswith("@"):
        return {"skip": f"a direct URL dependency (PEP 508 {s[:80]}): no registry is asked for it"}
    if s.startswith("{"):
        try:
            t = json.loads(s)
        except ValueError:
            t = None
        if isinstance(t, dict):
            if t.get("path") or t.get("git") or t.get("url") or t.get("develop"):
                return {"skip": "a path, git or URL dependency (Poetry): no registry holds it"}
            if t.get("source"):
                return {"hold": f"from the Poetry source {t['source']}"}
    u = uv_sources.get(normalize("pypi", name))
    if isinstance(u, dict):
        if u.get("path") or u.get("git") or u.get("url") or u.get("workspace"):
            return {"skip": "a path, git, URL or workspace source ([tool.uv.sources]): no registry holds it"}
        if u.get("index"):
            return {"hold": f"from the uv index {u['index']} ([tool.uv.sources])"}
    return {}


def _uv_sources(repo: Path) -> dict:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - py3.10
        import tomli as tomllib  # type: ignore[no-redef]
    p = Path(repo) / "pyproject.toml"
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - no file, or one that cannot be read: no sources
        return {}
    src = ((data.get("tool") or {}).get("uv") or {}).get("sources") or {}
    return {normalize("pypi", k): v for k, v in src.items()} if isinstance(src, dict) else {}


def go_replacements(repo: Path) -> dict[str, str]:
    """``replace`` directives of the root ``go.mod``: module -> its replacement (a path or a module)."""
    try:
        lines = (Path(repo) / "go.mod").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    out: dict[str, str] = {}
    block = False
    for ln in lines:
        s = ln.split("//")[0].strip()
        if s.startswith("replace ("):
            block = True
            continue
        if block and s == ")":
            block = False
            continue
        if block or s.startswith("replace "):
            m = re.match(r"^(?:replace\s+)?(\S+)(?:\s+\S+)?\s+=>\s+(\S+)", s)
            if m:
                out[m.group(1)] = m.group(2)
    return out


def _go_source(name: str, replaces: dict[str, str]) -> dict:
    to = replaces.get(name)
    if not to:
        return {}
    if to.startswith((".", "/")) or re.match(r"^[A-Za-z]:[\\/]", to) or "." not in to.split("/")[0]:
        return {"skip": f"replaced by the local path {to} in go.mod"}
    return {"name": to, "declared_as": name}


def from_declarations(items: list[dict], why: str, repo: Path | None = None) -> list[dict]:
    """Packages to check from declaration items, each with its declaration as evidence: the name as declared
    (npm and Maven names are case-sensitive), an npm alias or a Cargo ``package =`` rename read as the real
    package, a local, workspace, VCS or URL source skipped, a named private source held back."""
    uv = _uv_sources(repo) if repo is not None else {}
    replaces = go_replacements(repo) if repo is not None else {}
    out = []
    for it in items:
        eco = it.get("ecosystem")
        reg = registry_of(eco)
        if reg is None:
            continue
        name = str(it.get("declared") or it.get("name") or "")
        spec = str(it.get("spec") or "")
        src = (_npm_source(name, spec) if reg == "npm" else
               _cargo_source(name, spec, it.get("source")) if reg == "cargo" else
               _python_source(name, spec, uv) if reg == "pypi" else
               _go_source(name, replaces) if reg == "go" else {})
        out.append({"ecosystem": eco, "name": name, "why": why, **src,
                    "evidence": [{"locator": it.get("at") or f"{it.get('path')}:{it.get('line')}",
                                  "role": "declaration", "excerpt": spec}]})
    return out


_GO_HOSTS3 = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "golang.org", "go.googlesource.com")


def go_module_of(path: str, requires: list[str]) -> str | None:
    """The module a Go import path belongs to: the longest ``go.mod`` require that prefixes it, else the first
    three elements on hosts whose modules are ``host/owner/repo``; None when it cannot be told."""
    best = max((r for r in requires if path == r or path.startswith(r + "/")), key=len, default=None)
    if best:
        return best
    parts = path.split("/")
    if parts[0] in _GO_HOSTS3 and len(parts) >= 3:
        return "/".join(parts[:3])
    if parts[0] == "gopkg.in" and len(parts) >= 2:
        return "/".join(parts[:2])
    return None


def from_import_name(source: str, target: str, repo: Path | None = None) -> dict | None:
    """The package a proposed import names (``decide ask``): the registry from the source file's language, the
    distribution for a known Python import name, the npm package of a deep specifier, the Go module of a package
    path, the crate of a Rust path. None when the language has no registry lookup."""
    suffix = Path(source).suffix.lower()
    t = target.strip()
    why = {"why": "proposed import", "imported": True}
    if suffix in (".py", ".pyi"):
        from verinoda.depcheck import PY_ALIASES

        name = PY_ALIASES.get(t) or PY_ALIASES.get(t.split(".")[0])
        if not name and t.split(".")[0] in _PY_NAMESPACES:
            return {"ecosystem": "python", "name": t, **why,
                    "hold": f"{t.split('.')[0]} is a namespace several distributions share: the import does not "
                            "name the distribution (give the distribution name)"}
        return {"ecosystem": "python", "name": name or t.split(".")[0], **why}
    if suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte"):
        parts = t.split("/")
        name = "/".join(parts[:2]) if t.startswith("@") else parts[0]
        return {"ecosystem": "npm", "name": name, **why}
    if suffix == ".rs":
        return {"ecosystem": "cargo", "name": t.split("::")[0], **why}
    if suffix == ".go":
        if "." not in t.split("/")[0]:
            return {"ecosystem": "go", "name": t, **why}   # the standard library: skipped
        requires: list[str] = []
        if repo is not None:
            from verinoda.research import dependencies

            try:
                requires = [str(i["name"]) for i in dependencies(Path(repo)).get("items") or []
                            if i.get("ecosystem") == "go"]
            except Exception:  # noqa: BLE001 - an unreadable go.mod: no requires
                requires = []
        mod = go_module_of(t, requires)
        if mod is None:
            return {"ecosystem": "go", "name": t, **why,
                    "hold": "a Go package path whose module is not known (not required by go.mod, not a "
                            "host/owner/repo path): give the module path"}
        return {"ecosystem": "go", "name": mod, **why}
    if suffix in (".java", ".kt", ".kts", ".groovy", ".scala") and re.fullmatch(r"[\w.-]+:[\w.-]+", t):
        return {"ecosystem": "maven", "name": t, "why": "proposed dependency"}
    return None


def render_lines(r: dict) -> list[str]:
    s = r["summary"]
    out = [f"registry check (network {r['network']}): {s['flagged']} flagged, {s['caution']} caution, {s['ok']} ok, "
           f"{s['unknown']} unknown" + (f", {s['skipped']} skipped" if s.get("skipped") else "")]
    for p in r["packages"]:
        if p["verdict"] == "skipped":
            continue
        head = f"  {p['verdict'].upper():<8} {p['name']} ({REGISTRY_NAME.get(p.get('registry'), p.get('ecosystem'))}"
        if p.get("declared_as"):
            head += f", declared as {p['declared_as']}"
        if p.get("why"):
            head += f", {p['why']}"
        out.append(head + ")")
        for ev in p.get("evidence") or []:
            out.append(f"      {ev['role']:<11} {ev['locator']}")
        for sg in p["signals"]:
            at = f" [{sg['url']} at {sg.get('observed_at')}]" if sg.get("url") else ""
            out.append(f"      {sg['signal']} ({sg['status']}): {sg['claim']}{at}")
        f = p.get("facts") or {}
        bits = [f"latest {f['latest']}" if f.get("latest") else "",
                f"first release {str(f['first_release'])[:10]}" if f.get("first_release") else "",
                f"{f['downloads']['count']} downloads ({f['downloads']['period']})"
                if isinstance(f.get("downloads"), dict) else "",
                "from the cache" if p.get("from_cache") else ""]
        if any(bits):
            out.append("      " + ", ".join(b for b in bits if b))
        if p.get("unknown"):
            out.append(f"      unknown: {p['unknown']}")
        if p["verdict"] != "ok" and p.get("next_step"):
            out.append(f"      next: {p['next_step']}")
    return out


# -- check --deps --registry --------------------------------------------------------------------------------

REGISTRY_FINDINGS = ("not_in_registry", "registry_signal", "lookalike_name")


def add_to_deps(repo: Path, res: dict, *, which: str = "new", network: str = "off", rev: str = "HEAD",
                transport=None, now: float | None = None, private: dict | None = None,
                env: dict | None = None) -> dict:
    """Extend a ``depcheck.check_deps`` result with the registry check of the dependencies the change adds
    (``which="new"``: names a manifest declares now and did not declare at ``rev``) or of every declared one
    (``"all"``), plus the packages code imports but no manifest declares (``missing``). Registry facts and
    name signals become separate findings with their own status: ``not_in_registry`` (observed: the registry
    has no such name), ``registry_signal`` (observed: taken down, yanked, deprecated, young, few downloads, ...)
    and ``lookalike_name`` (strong_inference: near a popular name). Any of them makes the exit 3. A revision
    that is not a commit raises :class:`PackageCheckError`; outside a git work tree ``new`` checks the
    undeclared imports only and says so."""
    from verinoda.guards import declared_dependencies

    if which not in ("new", "all"):
        raise PackageCheckError(f"--registry must be new or all, not {which!r}")
    repo = Path(repo).resolve()
    items = declared_dependencies(repo).get("items") or []
    limits: list[str] = []
    if which == "all":
        pkgs = from_declarations(items, "declared", repo)
    else:
        try:
            pkgs = from_declarations(added_dependencies(repo, items, rev), f"added since {rev}", repo)
        except NotGitError:
            pkgs = []
            limits.append("not a git work tree, so the dependencies a change adds cannot be read: only the "
                          "undeclared imports were checked (--registry all checks every declared one)")
    for f in res.get("findings") or []:
        if f.get("finding") == "missing" and f.get("ecosystem") in ("python", "npm"):
            pkg = {"ecosystem": f["ecosystem"], "name": f["package"], "why": "imported, not declared",
                   "imported": True, "evidence": [e for e in f.get("evidence") or [] if e.get("role") == "import"][:3]}
            if f["ecosystem"] == "python" and f["package"] in _PY_NAMESPACES:
                pkg["hold"] = (f"{f['package']} is a namespace several distributions share: the import does not name "
                               "the distribution")
            pkgs.append(pkg)
    skipped = sorted({str(i.get("ecosystem")) for i in items if registry_of(i.get("ecosystem")) is None})
    if skipped:
        limits.append(f"{', '.join(skipped)} declarations have no registry lookup")
    reg = check_packages(repo, pkgs, network=network, transport=transport, now=now, private=private, env=env)
    reg["which"] = which
    reg["limits"] = limits + reg["limits"]
    res["registry"] = reg
    findings = res.setdefault("findings", [])
    for p in reg["packages"]:
        observed = [s for s in p["signals"] if s["status"] == "observed"
                    and s["signal"] in set(FLAG) | set(CAUTION)]
        if observed:
            kinds = {s["signal"] for s in observed}
            evidence = list(p.get("evidence") or []) + [
                {"locator": s["url"], "role": "registry", "excerpt": f"{s['claim']} (at {s.get('observed_at')})"}
                for s in observed if s.get("url")]
            findings.append({"finding": "not_in_registry" if "not_found" in kinds else "registry_signal",
                             "ecosystem": p["ecosystem"], "package": p["name"], "status": "observed",
                             "claim": "; ".join(s["claim"] for s in observed), "evidence": evidence,
                             "next_step": p.get("next_step", "")})
        looks = [s for s in p["signals"] if s["signal"] in LOOKALIKE and s["status"] == "strong_inference"]
        if looks:
            findings.append({"finding": "lookalike_name", "ecosystem": p["ecosystem"], "package": p["name"],
                             "status": "strong_inference", "claim": "; ".join(s["claim"] for s in looks),
                             "evidence": list(p.get("evidence") or []),
                             "next_step": f"compare {p['name']} with {looks[0]['like']}: use the package you meant"})
    summary = res.setdefault("summary", {})
    for k in REGISTRY_FINDINGS:
        summary[k] = sum(1 for f in findings if f["finding"] == k)
    if any(summary[k] for k in REGISTRY_FINDINGS):
        res["exit"] = 3
        res["exit_because"] = f"{len(findings)} finding(s)"
    return res
