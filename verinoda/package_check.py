"""Does a dependency exist in its registry, and does its name or record look like a squat?
(``verinoda check --deps --registry``, ``verinoda decide ask ... --registry``).

Opt-in and network-gated. The only thing sent anywhere is a package name, to its public registry, and only
when the network mode is ``on`` or ``cache`` (never by default): PyPI (the JSON API, and the Simple API for
the PEP 792 project status), npm (the registry document and the weekly download count), crates.io (the crate
API), Maven Central (``maven-metadata.xml``) and the Go module proxy (``@latest``). No credentials, no code,
no file contents.

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
CAUTION = ("yanked", "deprecated", "archived", "young", "few_downloads", "known_vulnerabilities", "typo_of",
           "separator_confusable")
# words a made-up package name often adds to a real one
_GENERIC = {"py", "python", "python3", "js", "node", "lib", "libs", "utils", "util", "tools", "tool", "toolkit",
            "helper", "helpers", "sdk", "api", "client", "core", "easy", "simple", "plus", "pro", "extra",
            "extras", "ai", "wrapper", "kit", "lite", "fast", "official", "dev", "secure", "safe", "auth"}
_NODE_BUILTINS = {"assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants", "crypto",
                  "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http", "http2", "https",
                  "inspector", "module", "net", "os", "path", "perf_hooks", "process", "punycode", "querystring",
                  "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls", "trace_events", "tty",
                  "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib"}


class PackageCheckError(ValueError):
    """A request that cannot be answered (bad network mode, no git for ``new``)."""


# -- names --------------------------------------------------------------------------------------------------

def registry_of(ecosystem: str) -> str | None:
    return REGISTRY.get(str(ecosystem or "").lower())


def normalize(registry: str, name: str) -> str:
    """The name as the registry compares it (PEP 503 for PyPI; lower case for npm and crates.io)."""
    n = str(name or "").strip()
    if registry == "pypi":
        return re.sub(r"[-_.]+", "-", n).lower()
    if registry in ("npm", "cargo"):
        return n.lower()
    return n


_POPULAR: dict[str, list[str]] | None = None


def popular(registry: str) -> list[str]:
    global _POPULAR
    if _POPULAR is None:
        try:
            data = json.loads((Path(__file__).parent / "data" / "popular_packages.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _POPULAR = {k: list(dict.fromkeys(normalize(k, x) for x in v)) for k, v in data.items()
                    if isinstance(v, list)}
    return _POPULAR.get(registry, [])


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
    return re.sub(r"[-_.]+", "", name.split("/")[-1] if name.startswith("@") else name)


def name_signals(registry: str, name: str) -> list[dict]:
    """Local, offline signals from the name alone (``strong_inference`` at most)."""
    n = normalize(registry, name)
    pop = popular(registry)
    if not n or n in pop:
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
        if project_status:
            return _answer("pypi", name, url, resp, True, {"project_status": project_status},
                           _status_signals(project_status, name, simple_url))
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
    vs = [v.text for v in root.iter("version") if v.text]
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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ent = data.get("entries") if isinstance(data, dict) and data.get("schema") == SCHEMA else None
    return ent if isinstance(ent, dict) else {}


def _save_cache(path: Path, entries: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps({"schema": SCHEMA, "entries": entries}, ensure_ascii=False, indent=1,
                                    sort_keys=True).encode("utf-8"))
    except OSError:
        pass


# -- the check ----------------------------------------------------------------------------------------------

def _verdict(signals: list[dict], exists) -> str:
    kinds = {s["signal"] for s in signals}
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
    reg = REGISTRY_NAME.get(ans["registry"], ans["registry"])
    if isinstance(f.get("age_days"), int) and f["age_days"] < YOUNG_DAYS:
        out.append({"signal": "young", "status": "observed", "observed_at": when, "url": ans["url"],
                    "claim": f"{ans['name']} was first published on {reg} {f['age_days']} day(s) before the check "
                             f"({f.get('first_release')})"})
    dl = f.get("downloads")
    if isinstance(dl, dict) and isinstance(dl.get("count"), int):
        few = FEW_NPM_WEEKLY if dl.get("period") == "last week" else FEW_CRATES_RECENT
        if dl["count"] < few:
            out.append({"signal": "few_downloads", "status": "observed", "observed_at": when, "url": dl.get("url"),
                        "claim": f"{ans['name']} was downloaded {dl['count']} time(s) in the {dl['period']} "
                                 f"(under {few})"})
    return out


def check_packages(repo: Path, packages: list[dict], *, network: str = "off", transport=None,
                   now: float | None = None, ttl_hours: float = TTL_HOURS, cache_file: Path | None = None) -> dict:
    """Check ``packages`` (``{"ecosystem", "name", "why"?, "evidence"?}``) against their registries (see the
    module). ``transport``: anything with ``get(url, accept=None)`` returning a transport ``Response``; the live,
    SSRF-guarded one when None and the network mode needs it."""
    if network not in NETWORK_MODES:
        raise PackageCheckError(f"network mode must be off, cache or on, not {network!r}")
    now = time.time() if now is None else now
    path = cache_file or cache_path(repo)
    cache = _load_cache(path)
    dirty = False
    ttl = ttl_hours * 3600
    results: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    for p in packages:
        reg = registry_of(p.get("ecosystem"))
        name = str(p.get("name") or "").strip()
        base = {"ecosystem": p.get("ecosystem"), "name": name, **({"why": p["why"]} if p.get("why") else {}),
                **({"evidence": p["evidence"]} if p.get("evidence") else {})}
        if reg is None:
            results.append({**base, "verdict": "unknown", "signals": [],
                            "unknown": f"no registry lookup for {p.get('ecosystem')} dependencies"})
            continue
        skip = skip_reason(reg, name, imported=bool(p.get("imported")))
        if skip:
            results.append({**base, "registry": reg, "verdict": "skipped", "signals": [], "skipped": skip})
            continue
        norm = normalize(reg, name)
        if (reg, norm) in seen:   # one question per package; the second mention keeps its own evidence
            results.append({**seen[(reg, norm)], **base})
            continue
        key = f"{reg}:{norm}"
        hit = cache.get(key) if isinstance(cache.get(key), dict) else None
        ans = None
        note = None
        if network == "off":
            note = (f"network is off, so {REGISTRY_NAME[reg]} was not asked" +
                    (f" (a cached answer from {hit.get('observed_at')} exists: --network cache reuses it)"
                     if hit else ""))
        elif network == "cache" and hit and now - float(hit.get("epoch") or 0) < ttl:
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
                    note += f"; the answer cached at {hit.get('observed_at')} is shown"
            else:
                ans = {**got, "from_cache": False}
                cache[key] = {"observed_at": got.get("observed_at"), "epoch": now, "answer": got}
                dirty = True
        signals = _record_signals(ans, now) if ans else []
        signals += name_signals(reg, name)
        exists = ans.get("exists") if ans else None
        res = {**base, "registry": reg, "exists": exists, "verdict": _verdict(signals, exists), "signals": signals}
        if ans:
            res.update({"url": ans.get("url"), "observed_at": ans.get("observed_at"),
                        "from_cache": ans.get("from_cache", False), "facts": ans.get("facts") or {}})
        if note:
            res["unknown"] = note
        res["next_step"] = _next(res, network)
        seen[(reg, norm)] = {k: v for k, v in res.items() if k not in ("why", "evidence")}
        results.append(res)
    if dirty:
        _save_cache(path, cache)
    summary = {v: sum(1 for r in results if r["verdict"] == v)
               for v in ("flagged", "caution", "ok", "unknown", "skipped")}
    limits = [
        "only package names are sent, and only to the public registry of their ecosystem; a private registry or "
        "mirror the project installs from is not asked, so a private package can look missing",
        "age, downloads and malware signals are what the registry shows without an account; no registry here "
        "scans packages for malicious code, so a package with no signal is not shown to be safe",
        "name signals compare with a small bundled list of popular names: strong_inference at most",
        "PyPI download counts are not read (its JSON API has none; pypistats.org rate-limits anonymous use); Go "
        "and Maven Central give no first release date in one request",
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

MANIFEST_GLOBS = ("*pyproject.toml", "*requirements*.txt", "*setup.py", "*setup.cfg", "*package.json", "*go.mod",
                  "*Cargo.toml", "*.gradle", "*.gradle.kts", "*pom.xml", "*.versions.toml")


def added_dependencies(repo: Path, items: list[dict], rev: str = "HEAD") -> list[dict]:
    """The declarations (``guards.declared_dependencies`` items) on lines the working tree changed against
    ``rev`` whose name the file at ``rev`` did not mention (a new dependency, not a version change). A new,
    untracked manifest adds all of its own. Raises :class:`PackageCheckError` outside a git work tree."""
    import subprocess

    from verinoda.codecheck import changed_lines

    repo = Path(repo)
    try:
        changed = changed_lines(repo, rev, globs=MANIFEST_GLOBS)
    except ValueError as exc:
        raise PackageCheckError(str(exc)) from None
    old: dict[str, set[str]] = {}

    def key(s: str) -> str:
        return re.sub(r"[-_.]+", "-", s).lower()

    def before(rel: str) -> set[str]:
        """The name-like words of the file at ``rev`` (whole words: ``request`` is not ``requests``)."""
        if rel not in old:
            r = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:./{rel}"], capture_output=True, timeout=60)
            text = r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""
            old[rel] = {key(w) for w in re.findall(r"@?[A-Za-z0-9][A-Za-z0-9._/@-]*", text)}
        return old[rel]

    out = []
    for it in items:
        rel, line = it.get("path"), it.get("line")
        if rel not in changed:
            continue
        lines = changed[rel]
        if lines is not None and line not in lines:
            continue
        if lines is not None:
            words = before(rel)
            name = str(it.get("name") or "")
            parts = [p for p in name.split(":") if p] if ":" in name else [name]   # group:artifact: both
            if parts and all(key(p) in words for p in parts):
                continue
        out.append(it)
    return out


def from_declarations(items: list[dict], why: str) -> list[dict]:
    """Packages to check from declaration items (each with its declaration as evidence)."""
    out = []
    for it in items:
        eco = it.get("ecosystem")
        if registry_of(eco) is None:
            continue
        out.append({"ecosystem": eco, "name": it.get("name"), "why": why,
                    "evidence": [{"locator": it.get("at") or f"{it.get('path')}:{it.get('line')}",
                                  "role": "declaration", "excerpt": it.get("spec") or ""}]})
    return out


def from_import_name(source: str, target: str) -> dict | None:
    """The package a proposed import names (``decide ask``): the registry from the source file's language, the
    distribution for a known Python import name, the npm package of a deep specifier. None when the language
    has no registry lookup."""
    suffix = Path(source).suffix.lower()
    t = target.strip()
    if suffix in (".py", ".pyi"):
        from verinoda.depcheck import PY_ALIASES

        name = PY_ALIASES.get(t) or PY_ALIASES.get(t.split(".")[0]) or t.split(".")[0]
        return {"ecosystem": "python", "name": name, "why": "proposed import", "imported": True}
    if suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte"):
        parts = t.split("/")
        name = "/".join(parts[:2]) if t.startswith("@") else parts[0]
        return {"ecosystem": "npm", "name": name, "why": "proposed import", "imported": True}
    if suffix == ".rs":
        return {"ecosystem": "cargo", "name": t.split("::")[0], "why": "proposed import", "imported": True}
    if suffix == ".go":
        return {"ecosystem": "go", "name": t, "why": "proposed import", "imported": True}
    if suffix in (".java", ".kt", ".kts", ".groovy", ".scala") and re.fullmatch(r"[\w.-]+:[\w.-]+", t):
        return {"ecosystem": "maven", "name": t.lower(), "why": "proposed dependency"}
    return None


def render_lines(r: dict) -> list[str]:
    s = r["summary"]
    out = [f"registry check (network {r['network']}): {s['flagged']} flagged, {s['caution']} caution, {s['ok']} ok, "
           f"{s['unknown']} unknown" + (f", {s['skipped']} skipped" if s.get("skipped") else "")]
    for p in r["packages"]:
        if p["verdict"] == "skipped":
            continue
        head = f"  {p['verdict'].upper():<8} {p['name']} ({REGISTRY_NAME.get(p.get('registry'), p.get('ecosystem'))}"
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
                f"first release {f['first_release'][:10]}" if f.get("first_release") else "",
                f"{f['downloads']['count']} downloads ({f['downloads']['period']})" if f.get("downloads") else "",
                "from the cache" if p.get("from_cache") else ""]
        if any(bits):
            out.append("      " + ", ".join(b for b in bits if b))
        if p.get("unknown"):
            out.append(f"      unknown: {p['unknown']}")
        if p["verdict"] != "ok" and p.get("next_step"):
            out.append(f"      next: {p['next_step']}")
    return out


# -- check --deps --registry --------------------------------------------------------------------------------

def add_to_deps(repo: Path, res: dict, *, which: str = "new", network: str = "off", rev: str = "HEAD",
                transport=None, now: float | None = None) -> dict:
    """Extend a ``depcheck.check_deps`` result with the registry check of the dependencies the change adds
    (``which="new"``: declarations on lines changed against ``rev`` naming a package the file did not name
    before) or of every declared one (``"all"``), plus the packages code imports but no manifest declares
    (``missing``). A flagged or cautioned package becomes a finding (``not_in_registry``, ``registry_signal``)
    and makes the exit 3."""
    from verinoda.guards import declared_dependencies

    if which not in ("new", "all"):
        raise PackageCheckError(f"--registry must be new or all, not {which!r}")
    repo = Path(repo).resolve()
    items = declared_dependencies(repo).get("items") or []
    limits: list[str] = []
    if which == "all":
        pkgs = from_declarations(items, "declared")
    else:
        try:
            pkgs = from_declarations(added_dependencies(repo, items, rev), f"added since {rev}")
        except PackageCheckError as exc:
            pkgs = []
            limits.append(f"the added dependencies could not be found ({exc}); --registry all checks every "
                          "declared one")
    for f in res.get("findings") or []:
        if f.get("finding") == "missing" and f.get("ecosystem") in ("python", "npm"):
            pkgs.append({"ecosystem": f["ecosystem"], "name": f["package"], "why": "imported, not declared",
                         "imported": True, "evidence": [e for e in f.get("evidence") or []
                                                        if e.get("role") == "import"][:3]})
    skipped = sorted({str(i.get("ecosystem")) for i in items if registry_of(i.get("ecosystem")) is None})
    if skipped:
        limits.append(f"{', '.join(skipped)} declarations have no registry lookup")
    reg = check_packages(repo, pkgs, network=network, transport=transport, now=now)
    reg["which"] = which
    reg["limits"] = limits + reg["limits"]
    res["registry"] = reg
    for p in reg["packages"]:
        if p["verdict"] not in ("flagged", "caution"):
            continue
        kinds = {s["signal"] for s in p["signals"]}
        observed = [s for s in p["signals"] if s["status"] == "observed"]
        status = "observed" if observed else max((s["status"] for s in p["signals"]),
                                                 key=("weak_inference", "strong_inference").index)
        evidence = list(p.get("evidence") or []) + [
            {"locator": s["url"], "role": "registry", "excerpt": f"{s['claim']} (at {s.get('observed_at')})"}
            for s in observed if s.get("url")]
        res.setdefault("findings", []).append({
            "finding": "not_in_registry" if "not_found" in kinds else "registry_signal",
            "ecosystem": p["ecosystem"], "package": p["name"], "status": status,
            "claim": "; ".join(s["claim"] for s in p["signals"]), "evidence": evidence,
            "next_step": p.get("next_step", "")})
    summary = res.setdefault("summary", {})
    summary["not_in_registry"] = sum(1 for f in res["findings"] if f["finding"] == "not_in_registry")
    summary["registry_signal"] = sum(1 for f in res["findings"] if f["finding"] == "registry_signal")
    if summary["not_in_registry"] or summary["registry_signal"]:
        res["exit"] = 3
        res["exit_because"] = f"{len(res['findings'])} finding(s)"
    return res
