"""parse -> classify -> bind -> resolve -> pin -> report (docs/DESIGN.md D10-D13).

:func:`resolve` turns what the user wrote into exact, recorded pins. Output
schema ``verinoda.reference_resolution/1``:

``mentions``           every extracted mention (plus the explicit references)
``references``         one entry per referenced object: ``class``, ``status``
                       (``pinned`` | ``pinned_floating`` | ``partially_resolved`` |
                       ``ambiguous`` | ``unresolved`` | ``refused``), ``identity``,
                       ``requested`` (what the text and the URL asked for),
                       ``pin`` (``kind``, ``value``, ``display``, ``basis``,
                       ``precedence_rank``, ``immutable``), ``alternates`` (every
                       other candidate with why it lost), ``mismatches`` (M1-M12),
                       ``unresolved`` parts with a ``next_step``, ``warnings``
``unbound_mentions``   mentions that qualify nothing, each with ``why``
``questions_for_user`` fixed TR/EN questions when the intent cannot be told apart
``summary``            counts incl. ``mentions_accounted`` and ``silent_floating_pins``

Invariant I1: every mention id is in exactly one ``references[].mentions`` or
``unbound_mentions``. Invariant I2: a named version never falls back to a
floating ref. Results are stored append-only in ``reference_resolutions``.

The resolver never runs project code and needs no network for local
repositories, lock files and cached data; ``network`` is ``off`` (local and
cache only), ``cache`` (cache first) or ``on`` (revalidate).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from verinoda import textnorm as tn
from verinoda.references import local as localmod
from verinoda.references import registries as reg
from verinoda.references import versions as vermod
from verinoda.references.classify import (
    GIT_CLASSES,
    REF_REQUIRED,
    SHA_RE,
    _empty,
    classify,
    classify_mention,
)
from verinoda.references.gitref import GitRunner, run_git
from verinoda.references.mentions import (
    QUALIFIER_KINDS,
    REFERENCE_KINDS,
    clause_of,
    clauses,
    extract,
)
from verinoda.references.pin import (
    candidate,
    choose,
    is_floating,
    make_pin,
    mismatch,
    worst_severity,
)
from verinoda.references.transport import CassetteMiss, Transport, for_mode

SCHEMA = "verinoda.reference_resolution/1"
EXTERNAL = frozenset(GIT_CLASSES | {"package", "paper", "doi", "swh_object", "doc_page", "qa_post", "web_page",
                                    "issue", "application", "runtime"})
_VERSIONABLE = frozenset(GIT_CLASSES | {"package", "runtime", "paper", "doc_page", "issue", "application"})
_GAP = re.compile(r"(?:['’ʼ][a-zçğıöşüâîû]{1,14})?\s{1,3}", re.I)


def _norm_name(s: str) -> str:
    return re.sub(r"[-_.]+", "-", (s or "").lower())


def _now(now) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    if isinstance(now, str):
        return dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    return now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)


class _Ctx:
    def __init__(self, store, repo: Path, text: str, network: str, now: dt.datetime, transport: Transport,
                 git: GitRunner, local_intent: bool | None, max_network_calls: int):
        self.store, self.repo, self.text, self.network = store, repo, text, network
        self.now = now
        self.now_iso = now.isoformat().replace("+00:00", "Z")
        self.transport, self.git = transport, git
        self.local_intent = bool(local_intent)
        self.local_intent_source = "caller" if local_intent else None
        self.max_network_calls = max_network_calls
        self._local: dict | None = None
        self._registry: dict = {}
        self.local_commit = None
        self.lang = tn.detect_language(text) if text.strip() else "unknown"

    @property
    def local(self) -> dict:
        if self._local is None:
            try:
                self._local = localmod.local_versions(self.repo)
            except Exception as exc:  # noqa: BLE001 - a broken lock file must not break resolution
                self._local = {"packages": {}, "runtime": {}, "sources": [], "unsupported": [],
                               "problems": [f"{type(exc).__name__}: {exc}"]}
        return self._local

    def budget_left(self) -> bool:
        return (self.transport.network_calls + self.git.network_calls) < self.max_network_calls

    def registry(self, fn, *args) -> dict:
        k = (fn.__name__, args)
        if k not in self._registry:
            if self.network != "off" and not self.budget_left():
                self._registry[k] = {"ok": False, "reason": "budget",
                                     "error": f"network budget of {self.max_network_calls} calls used up"}
            else:
                self._registry[k] = fn(self.transport, *args)
        return self._registry[k]


# =============================================================================
# mentions -> references
# =============================================================================

def _explicit_mentions(explicit) -> list[dict]:
    out = []
    for i, e in enumerate(explicit or (), 1):
        if isinstance(e, str):
            e = {"reference": e, "ref": None}
        ref = e.get("reference") or e.get("text")
        if not ref:
            continue
        out.append({"id": f"e{i}", "kind": "explicit", "span": None, "text": str(ref), "stripped_suffix": None,
                    "normalized": {"ref": e.get("ref")}, "extractor": "explicit", "confidence": "exact",
                    "binds_to": None, "binding": None})
    return out


def _new_ref(rid: str, mention: dict, spec: dict) -> dict:
    return {"id": rid, "mentions": [mention["id"]], "class": spec["class"], "status": None,
            "coreference": None, "identity": spec["identity"], "requested": spec["requested"],
            "pin": None, "alternates": [], "mapping": [], "artifacts": [], "mismatches": [], "unresolved": [],
            "warnings": list(spec.get("notes") or []), "_spec": spec, "_anchor": mention,
            "_qual": [], "_explicit_ref": (mention.get("normalized") or {}).get("ref")
            if mention["kind"] == "explicit" else None}


def _identity_key(ref: dict) -> str | None:
    ident = ref["identity"]
    cls = ref["class"]
    if cls in GIT_CLASSES and ident.get("canonical_url"):
        req = ref["requested"]
        return f"git:{ident['canonical_url'].lower()}|{cls}|{req.get('url_ref')}|{req.get('path')}|{req.get('number')}"
    if cls in ("issue", "pull_request", "merge_request") and ident.get("canonical_url"):
        return f"num:{ident['canonical_url'].lower()}|{ref['requested'].get('number')}"
    if cls == "package" and ident.get("package") and ident["package"].get("ecosystem") != "unknown":
        p = ident["package"]
        return f"pkg:{p['ecosystem']}/{_norm_name(p['name'])}|{ref['requested'].get('version_text')}"
    if cls in ("paper",) and ident.get("paper"):
        return f"arxiv:{ident['paper']['arxiv_id']}|{ref['requested'].get('version_text')}"
    if cls == "doi":
        return f"doi:{ident['paper']['doi'].lower()}"
    return None


def _merge(into: dict, other: dict, via: str) -> None:
    into["mentions"] += other["mentions"]
    into["_qual"] += other["_qual"]
    co = into.get("coreference") or {"merged": [], "via": []}
    co["merged"].append(other["_anchor"]["text"])
    if via not in co["via"]:
        co["via"].append(via)
    into["coreference"] = co
    if other.get("_explicit_ref") and not into.get("_explicit_ref"):
        into["_explicit_ref"] = other["_explicit_ref"]
    pkg = other["identity"].get("package")
    if pkg and pkg.get("ecosystem") not in (None, "unknown") and not into["identity"].get("package"):
        into["identity"]["package"] = pkg
    if other["class"] == "package" and other["requested"].get("version_text"):
        into.setdefault("_package_versions", []).append(
            {"version": other["requested"]["version_text"], "exact": other["requested"].get("version_exact"),
             "mention": other["_anchor"]["id"]})
    if other["class"] == "local_file" and not into["requested"].get("path"):
        into["requested"]["path"] = other["requested"]["path"]


REGISTRY_ECOSYSTEMS = ("pypi", "npm", "cargo")   # tried for "<name> <version>" when nothing local names it
_DETERMINERS = frozenset("a an the this that these those its our their my your some any every each no one another "
                         "bir bu şu su o her hiç hic".split())


# words that stand before a number in ordinary prose ("took 2.5 seconds", "macOS 14.2", "below 0.75")
_NOT_PACKAGE_NAMES = frozenset("""took take takes taking waits wait waited waiting lasts last lasted drops drop dropped
    falls fall fell rises rise rose fails fail failed runs run ran uses use used needs need needed gets get got
    below above after before about around over under than approx approximately roughly nearly almost only just
    by to from at on in of for with within and or is was are were be been being version versions release releases
    build builds update upgrade upgraded patch fix fixed new old latest same next last first second final current
    previous stable beta alpha bare seconds second secs sec minutes minute mins ms hours hour days day times time
    percent pct x mb gb kb tb px pt em rem macos ios ipados android windows win linux ubuntu debian fedora centos
    rhel alpine chrome chromium firefox safari edge opera iphone ipad pixel galaxy java jdk jre sdk api level
    step steps page pages line lines chapter section figure table item items""".split())


def _name_strength(ctx: _Ctx, r: dict, mentions: list[dict]) -> str:
    """How surely the user wrote a project name: ``strong`` ("the X package", "kütüphane X", a name
    right before a cued version such as ``v0.3`` or ``sürüm 2``), ``weak`` (a word before a bare number,
    an apostrophe-suffixed word) or ``none`` (an ordinary word: "took 2.5 seconds")."""
    m = r["_anchor"]
    ex = m.get("extractor") or ""
    if ex in ("regex:cue_name", "regex:name_cue"):
        return "strong"
    name = (m.get("text") or "").lower()
    if name in _NOT_PACKAGE_NAMES or name in _DETERMINERS or tn.fold_tr(name) in tn.TR_STOPWORDS:
        return "none"
    if ex == "regex:name_before_version":
        ver = _version_after(ctx, r, mentions)
        return "strong" if ver is not None and ver["kind"] == "version" else "weak"
    return "weak"


def _version_after(ctx: _Ctx, r: dict, mentions: list[dict]) -> dict | None:
    span = r["_anchor"].get("span")
    if not span:
        return None
    after = [m for m in mentions if m["kind"] in ("version", "version_candidate") and m["span"][0] >= span[1]]
    ver = min(after, key=lambda m: m["span"][0]) if after else None
    return ver if ver is not None and _GAP.fullmatch(ctx.text[span[1]:ver["span"][0]]) else None


MAX_REGISTRY_NAMES = 2   # names before a version looked up in public registries per message (network on)


def _registry_package(ctx: _Ctx, r: dict, name: str, mentions: list[dict]) -> str | None:
    """``requests 2.31``: a name written right before a version may be a package. Public registries
    are asked only with ``network="on"`` (the name of an internal package is not sent to PyPI,
    npm and crates.io by default), for at most :data:`MAX_REGISTRY_NAMES` names per message. A
    registry that has the name and the version binds it; several such registries, or a foreign one
    while the project's own ecosystem has the name without that version, are ambiguous: the name
    stays unbound and the reason says which registries have it. Returns None when the name was
    bound, else why it stays unbound.
    """
    ver = _version_after(ctx, r, mentions)
    if ver is None:
        return "no version right after the name"
    if _name_strength(ctx, r, mentions) == "none":
        return "an ordinary word before a number, not a package name"
    if ctx.network != "on":
        return "a name before a version; public registries are asked only with network on"
    ctx.registry_names = getattr(ctx, "registry_names", 0) + 1
    if ctx.registry_names > MAX_REGISTRY_NAMES:
        return f"more than {MAX_REGISTRY_NAMES} names before versions in one message; not looked up"
    version = ver["text"].lstrip("vV")
    local_ecos = [e for e in dict.fromkeys(it["ecosystem"] for items in (ctx.local.get("packages") or {}).values()
                                           for it in items) if e in REGISTRY_ECOSYSTEMS]
    found: list[tuple[str, bool]] = []
    for eco in dict.fromkeys(local_ecos + list(REGISTRY_ECOSYSTEMS)):
        info = ctx.registry(reg.package_versions, eco, name)
        if info.get("ok"):
            has = any(vermod.equal(v, version) or str(v).startswith(version + ".") for v in info.get("versions", []))
            found.append((eco, has))
            if has and eco in local_ecos:
                break  # the project's own ecosystem has it: no need to ask the others
    exact = [e for e, has in found if has]
    own = [e for e, _has in found if e in local_ecos]
    if len(exact) > 1 and not any(e in local_ecos for e in exact):
        return f"'{name} {version}' exists in several registries ({', '.join(exact)}); which one is meant?"
    if exact and exact[0] not in local_ecos and own:
        return (f"the project's own {own[0]} has '{name}' but not version {version}; {exact[0]} has it: "
                "which one is meant?")
    eco = next((e for e in exact if e in local_ecos), exact[0] if exact else (own[0] if own else None))
    if eco is None:
        return f"no registry has '{name}' with version {version}"
    r["class"] = "package"
    r["identity"]["package"].update(ecosystem=eco, purl=f"pkg:{eco}/{name}", ecosystem_guessed=True)
    r["coreference"] = {"merged": [], "via": ["name_before_version", f"registry_{eco}"]}
    if eco not in exact:
        r["warnings"].append(f"'{name}' is in the project's own {eco} registry, but without version {version}")
    return None


_PREP_GAP = re.compile(r"\s+(?:in|on|from|of|at|for)\s+|\s*(?:['’]\w{1,6})?\s+", re.I)


def _has_origin(ctx: _Ctx) -> bool:
    rc, out, _ = run_git(["remote", "get-url", "origin"], cwd=ctx.repo)
    return rc == 0 and bool(out.strip())


def _clause_refs(refs: list[dict], segs, pos: int) -> list[dict]:
    ci = clause_of(segs, pos)
    return [r for r in refs if r["_anchor"].get("span") and clause_of(segs, r["_anchor"]["span"][0]) == ci]


def _build_references(ctx: _Ctx, mentions: list[dict], explicit: list[dict]) -> tuple[list[dict], list[dict]]:
    refs: list[dict] = []
    unbound: list[dict] = []
    n = 0
    for m in explicit + [m for m in mentions if m["kind"] in REFERENCE_KINDS]:
        try:
            spec = classify(m["text"]) if m["kind"] == "explicit" else classify_mention(m)
        except ValueError as exc:
            unbound.append({"mention": m["id"], "text": m["text"], "why": f"not a usable reference: {exc}"})
            continue
        n += 1
        refs.append(_new_ref(f"r{n}", m, spec))
    segs = clauses(ctx.text, mentions)
    # 1. identical identities merge (explicit + the same URL in the text, duplicated mentions)
    merged: list[dict] = []
    seen: dict[str, dict] = {}
    for r in refs:
        k = _identity_key(r)
        if k and k in seen:
            _merge(seen[k], r, "same_identity")
            continue
        if k:
            seen[k] = r
        merged.append(r)
    refs = merged
    # 2. names: a name next to a repository/package/docs reference is that reference; else a local dependency
    out: list[dict] = []
    for r in refs:
        spec = r["_spec"]
        if not spec.get("candidate") and r["class"] != "runtime":
            out.append(r)
            continue
        name = r["_anchor"]["text"]
        near = [x for x in (_clause_refs(refs, segs, r["_anchor"]["span"][0]) if r["_anchor"].get("span") else refs)
                if x is not r and not x["_spec"].get("candidate") and x["class"] != "runtime"]
        target = None
        via = None
        for x in near:
            ident = x["identity"]
            if r["class"] == "runtime":
                rt = r["identity"]["package"]["name"]
                if x["class"] == "doc_page" and (ident.get("docs") or {}).get("project") == rt:
                    target, via = x, "runtime_docs"
                    break
                continue
            if ident.get("repo") and _norm_name(ident["repo"]) == _norm_name(name):
                target, via = x, "name_matches_repo"
                break
            pkg = ident.get("package") or {}
            if pkg.get("name") and _norm_name(pkg["name"].split("/")[-1]) == _norm_name(name):
                target, via = x, "name_matches_package"
                break
            docs = ident.get("docs") or {}
            if docs.get("project") and _norm_name(docs["project"]) == _norm_name(name):
                target, via = x, "name_matches_docs_project"
                break
        if target is not None:
            _merge(target, r, via)
            continue
        if r["class"] == "runtime":
            out.append(r)
            continue
        locals_ = localmod.find_by_name(ctx.local, name)
        if locals_:
            it = locals_[0]
            r["class"] = "package"
            r["identity"]["package"] = {"ecosystem": it["ecosystem"], "name": it["name"],
                                        "purl": localmod.key(it["ecosystem"], it["name"])}
            r["coreference"] = {"merged": [], "via": ["local_dependency"], "evidence": it["locator"]}
            out.append(r)
            continue
        why = _registry_package(ctx, r, name, mentions)
        if why is None:
            out.append(r)
            continue
        strength = _name_strength(ctx, r, mentions)
        unbound.append({"mention": r["_anchor"]["id"], "text": name, "ask": strength == "strong",
                        "why": "a name that is neither a local dependency nor next to a repository, package or docs "
                               f"reference ({why})"})
    refs = out
    # 3. package spec + repository with the same name in one clause: one project
    for r in list(refs):
        if r["class"] != "package" or not r["_anchor"].get("span"):
            continue
        name = (r["identity"].get("package") or {}).get("name") or ""
        for x in _clause_refs(refs, segs, r["_anchor"]["span"][0]):
            if x is not r and x["class"] in GIT_CLASSES and x["identity"].get("repo") \
                    and _norm_name(x["identity"]["repo"]) == _norm_name(name.split("/")[-1]):
                _merge(x, r, "name_matches_repo")
                refs.remove(r)
                break
    # 4. local files: in the project, or the path of a repository reference in the clause
    for r in list(refs):
        if r["class"] != "local_file":
            continue
        path = r["requested"]["path"]
        if (ctx.repo / path).is_file():
            continue
        host = [x for x in (_clause_refs(refs, segs, r["_anchor"]["span"][0]) if r["_anchor"].get("span") else [])
                if x["class"] in GIT_CLASSES and not x["requested"].get("path")]
        if host:
            _merge(host[0], r, "path_of_repository_reference")
            refs.remove(r)
    # 5. "PR #123 and issue #456 in psf/requests": a bare number belongs to the repository its sentence
    #    names (else to the only repository in the message; else, later, to the local origin remote)
    repos = [x for x in refs if x["class"] in GIT_CLASSES and x["identity"].get("canonical_url")]
    distinct_repos = list({x["identity"]["canonical_url"]: x for x in repos}.values())
    for r in refs:
        if r["class"] not in ("issue", "pull_request", "merge_request") or r["identity"].get("canonical_url"):
            continue
        span = r["_anchor"].get("span")
        near = list({x["identity"]["canonical_url"]: x for x in
                     (_clause_refs(repos, segs, span[0]) if span else [])}.values())
        host = via = None
        if len(near) == 1:
            host, via = near[0], "repository_in_sentence"
        elif len(near) > 1 and span:
            # "PR #5 in psf/requests and PR #7 in urllib3/urllib3": the repository written right after it
            follow = [x for x in near if x["_anchor"]["span"][0] >= span[1]
                      and _PREP_GAP.fullmatch(ctx.text[span[1]:x["_anchor"]["span"][0]])]
            if len(follow) == 1:
                host, via = follow[0], "repository_after_number"
            else:
                r["warnings"].append(f"its sentence names {len(near)} repositories "
                                     f"({', '.join(x['_anchor']['text'] for x in near)}); none was chosen")
        elif not near and len(distinct_repos) == 1 and not _has_origin(ctx):
            # another sentence names the only repository, and the project has no origin remote to prefer
            host, via = distinct_repos[0], "only_repository_in_message"
        if host is not None:
            for k in ("host", "owner", "repo", "canonical_url", "clone_url", "local_path"):
                r["identity"][k] = host["identity"].get(k)
            r["coreference"] = {"merged": [], "via": [via], "repository": host["_anchor"]["text"]}
    for i, r in enumerate(refs, 1):
        r["id"] = f"r{i}"
    return refs, unbound


def _bind(ctx: _Ctx, mentions: list[dict], refs: list[dict], unbound: list[dict], questions: list[dict]) -> None:
    segs = clauses(ctx.text, mentions)
    by_anchor = {}
    for r in refs:
        for mid in r["mentions"]:
            by_anchor[mid] = r
    ordered = sorted(mentions, key=lambda m: m["span"][0])
    for idx, q in enumerate(ordered):
        if q["kind"] not in QUALIFIER_KINDS:
            continue
        if q["kind"] == "local_pin":
            ctx.local_intent = True
            ctx.local_intent_source = ctx.local_intent_source or f"mention {q['id']}"
        cands = [r for r in _clause_refs(refs, segs, q["span"][0])]
        if q["kind"] == "sha":
            cands = [r for r in cands if r["class"] in GIT_CLASSES | {"issue", "pull_request", "merge_request"}]
        elif q["kind"] in ("version", "version_candidate", "date", "floating"):
            cands = [r for r in cands if r["class"] in _VERSIONABLE]
        # adjacency: "requests 2.31", "requests'in 2.31 sürümü"
        prev = ordered[idx - 1] if idx > 0 else None
        adjacent = None
        if prev is not None and prev["id"] in by_anchor and _GAP.fullmatch(ctx.text[prev["span"][1]:q["span"][0]]):
            adjacent = by_anchor[prev["id"]] if by_anchor[prev["id"]] in cands else None
        target, binding = None, None
        if adjacent is not None:
            target, binding = adjacent, "single"
        elif len(cands) == 1:
            target, binding = cands[0], "single"
        elif len(cands) > 1:
            if q["kind"] in ("version", "version_candidate") and not (q.get("normalized") or {}).get("relative"):
                exists = [r for r in cands if _version_exists(ctx, r, q["text"])]
                if len(exists) == 1:
                    target, binding = exists[0], "validated_by_existence"
            if target is None:
                target = min(cands, key=lambda r: abs(r["_anchor"]["span"][0] - q["span"][0]))
                binding = "nearest_ambiguous"
        if q["kind"] == "version_candidate" and not (q.get("normalized") or {}).get("relative") and adjacent is None:
            # a bare "2.5" is only a version when it follows a grounded reference ("requests 2.31")
            unbound.append({"mention": q["id"], "text": q["text"],
                            "why": "a bare number without a version word or a grounded reference right before it"})
            continue
        if q["kind"] == "sha" and target is None:
            local_ref = _local_commit_ref(ctx, q, len(refs) + 1)
            if local_ref is not None:
                refs.append(local_ref)
                continue
        if target is None:
            why = {"local_pin": "applies to the whole question as local intent; no reference in its sentence",
                   "floating": "no reference in its sentence to take the latest version of",
                   "date": "no reference in its sentence to date",
                   "sha": "no repository in its sentence and not a commit of the local project"}.get(
                q["kind"], "no reference in its sentence to attach the version to")
            unbound.append({"mention": q["id"], "text": q["text"], "why": why})
            continue
        q["binds_to"] = target["_anchor"]["id"]
        q["binding"] = binding
        target["mentions"].append(q["id"])
        target["_qual"].append(q)
        if binding == "nearest_ambiguous":
            questions.append(_question(ctx, target["id"], "binding", q["text"],
                                       [f"{c['id']}: {c['_anchor']['text']}" for c in cands[:4]]))


def _version_exists(ctx: _Ctx, r: dict, version: str) -> bool:
    try:
        if r["class"] in GIT_CLASSES:
            url = r["identity"].get("local_path") or r["identity"].get("clone_url")
            rs = ctx.git.refs(url) if url else {}
            return bool(rs.get("ok")) and vermod.match_tags(version, list(rs["tags"]),
                                                           name=r["identity"].get("repo"))["how"] != "none"
        if r["class"] == "package":
            p = r["identity"].get("package") or {}
            if p.get("ecosystem") in (None, "unknown"):
                return False
            info = ctx.registry(reg.package_versions, p["ecosystem"], p["name"]) if ctx.network != "off" else {}
            return bool(info.get("ok")) and any(vermod.equal(v, version) for v in info.get("versions", []))
    except CassetteMiss:
        raise
    except Exception:  # noqa: BLE001 - existence is a tie-breaker only
        return False
    return False


def _local_commit_ref(ctx: _Ctx, q: dict, n: int) -> dict | None:
    rc, out, _ = run_git(["rev-parse", "--verify", "--quiet", f"{q['text']}^{{commit}}"], cwd=ctx.repo)
    if rc != 0 or not out.strip():
        return None
    spec = _empty("git_commit", q["text"])
    spec["identity"].update(local_path=str(ctx.repo), repo=ctx.repo.name, canonical_url=ctx.repo.as_uri())
    spec["requested"]["commit"] = q["text"]
    spec["requested"]["path"] = None
    ref = _new_ref(f"r{n}", q, spec)
    ref["coreference"] = {"merged": [], "via": ["local_project_commit"]}
    ref["_local_sha"] = out.strip()
    return ref


# =============================================================================
# per-class resolution
# =============================================================================

def _q_versions(r: dict) -> list[dict]:
    return [q for q in r["_qual"] if q["kind"] in ("version", "version_candidate")
            and not (q.get("normalized") or {}).get("relative")]


def _q(r: dict, kind: str) -> list[dict]:
    return [q for q in r["_qual"] if q["kind"] == kind]


def _unresolved(r: dict, what: str, why: str, next_step: str, *, mention: str | None = None, reason: str | None = None):
    item = {"what": what, "why": why, "next_step": next_step}
    if mention:
        item["mention"] = mention
    if reason:
        item["reason"] = reason
    r["unresolved"].append(item)


def _sha12(s: str | None) -> str:
    return (s or "")[:12]


def _repo_url(r: dict) -> str | None:
    ident = r["identity"]
    return ident.get("local_path") or ident.get("clone_url") or ident.get("canonical_url")


def _resolve_git(ctx: _Ctx, r: dict, questions: list[dict]) -> None:
    ident, req = r["identity"], r["requested"]
    url = _repo_url(r)
    name = ident.get("repo")
    if not url:
        r["status"] = "unresolved"
        _unresolved(r, "repository", "the reference names no repository", "give the repository URL or owner/repo")
        return
    if r.get("_local_sha"):
        sha = r["_local_sha"]
        r["pin"] = make_pin("commit", sha, f"local commit {_sha12(sha)}", "immutable_in_reference", immutable=True)
        r["status"] = "pinned"
        return
    if r["class"] in REF_REQUIRED and not (req.get("ref_candidates") or req.get("compare") or req.get("commit")
                                           or req.get("number")):
        # compare/archive/release/PR/MR name a version by construction: without it, refuse (never HEAD)
        r["status"] = "refused"
        _unresolved(r, "ref", f"this {r['class'].replace('_', ' ')} URL names no version (no range, tag or number); "
                    "refusing to use the default branch or any other version in its place",
                    "give the full URL with its range/tag/number", reason="ref_required")
        return
    rs = ctx.git.refs(url)
    if not rs.get("ok"):
        reason = rs.get("reason") or "unreachable"
        if req.get("commit") and SHA_RE.match(req["commit"]):
            r["pin"] = make_pin("commit", req["commit"], f"commit {req['commit'][:12]} (existence not verified: "
                                f"{reason})", "immutable_in_reference", immutable=True)
            r["status"] = "pinned"
            r["warnings"].append(f"the repository could not be listed ({rs.get('error')}); the commit named in the "
                                 "reference is used as given")
            return
        r["status"] = "unresolved"
        if reason in ("not_found", "auth_required"):
            r["mismatches"].append(mismatch("M12", f"{url}: {rs.get('error')}",
                                            "the repository is missing, private or needs credentials"))
        steps = {"offline": f"run with network enabled: verinoda resolve ... --network cache (or clone {url} and "
                            "pass the local path)",
                 "not_found": "check the repository URL; it may have moved or been deleted",
                 "auth_required": "configure git credentials for this host (prompts are disabled)"}
        _unresolved(r, f"refs of {ident.get('canonical_url') or url}", rs.get("error") or reason,
                    steps.get(reason, f"test access with: git ls-remote {url}"), reason=reason)
        return
    if rs.get("warning"):
        r["warnings"].append(f"refs read from the cached mirror as last fetched: {rs['warning']}")
    tags = list(rs.get("tags") or {})
    cands: list[dict] = []
    collisions: list[dict] = []
    hint = req.get("ref_namespace_hint") or "none"

    def pin_named(nm: str, basis_tag: str, basis_branch: str, source: str, *, hint_: str = hint) -> dict:
        res = ctx.git.resolve_name(rs, nm, hint_)
        if not res["found"]:
            return candidate(basis_tag, error=f"'{nm}' is neither a branch nor a tag of {ident.get('canonical_url')}",
                             source=source, wanted=nm)
        if res["collision"]:
            collisions.append({"name": nm, **res})
        kind = res["kind"]
        basis = basis_tag if kind == "tag" else basis_branch
        pk = "tag" if kind == "tag" else "branch"
        disp = (f"{nm} -> {_sha12(res['sha'])}" if kind == "tag"
                else f"branch {nm} -> {_sha12(res['sha'])} at {ctx.now_iso}")
        return candidate(basis, pin=make_pin(pk, res["sha"], disp, basis, immutable=kind == "tag", name=nm,
                                             ref=res["qualified"], retrieved_at=ctx.now_iso), source=source)

    # rank 1: a commit in the reference or a bound bare SHA
    shas = ([req["commit"]] if req.get("commit") else []) + [q["text"] for q in _q(r, "sha")]
    definitive = ctx.git.local_path(url) is not None or ctx.network != "off"
    for sha in shas:
        gd = ctx.git.objects(url, need=[sha] if len(sha) == 40 else None)
        full = ctx.git.commit_of(gd, sha) if gd is not None else None
        if gd is not None and full is None and definitive:
            cands.append(candidate("immutable_in_reference",
                                   error=f"commit {sha} not found in {ident.get('canonical_url')}",
                                   source="reference", wanted=sha))
            continue
        cands.append(candidate("immutable_in_reference", pin=make_pin(
            "commit", full or sha, f"commit {_sha12(full or sha)}" + ("" if full else " (not verified)"),
            "immutable_in_reference", immutable=True), source="reference"))
    # rank 2: versions in the text, an explicit ref, a coreferenced package version
    for q in _q_versions(r):
        m = vermod.match_tags(q["text"], tags, name=name)
        if m["chosen"]:
            sha = rs["tags"][m["chosen"]]
            disp = f"{m['chosen']} -> {_sha12(sha)}"
            c = candidate("explicit_text_version", pin=make_pin("tag", sha, disp, "explicit_text_version",
                                                                immutable=True, name=m["chosen"],
                                                                ref=f"refs/tags/{m['chosen']}",
                                                                version_text=q["text"],
                                                                version_candidates=m["candidates"][:8]),
                          source=q["id"])
            if m["how"] == "prefix" and len(m["candidates"]) > 1:
                r["warnings"].append(f"'{q['text']}' matches {len(m['candidates'])} tags; the highest release "
                                     f"{m['chosen']} is used")
                questions.append(_question(ctx, r["id"], "version_series", q["text"], m["candidates"][:5]))
            cands.append(c)
        else:
            near = [t for t in tags if vermod.normalize_tag(t, name).split(".")[:1]
                    == vermod.normalize_version(q["text"]).split(".")[:1]][-5:]
            cands.append(candidate("explicit_text_version", error=f"no tag of {ident.get('canonical_url')} matches "
                                   f"version '{q['text']}'", source=q["id"], wanted=q["text"], near=near))
    for pv in r.get("_package_versions") or []:
        m = vermod.match_tags(pv["version"], tags, name=name)
        if m["chosen"]:
            sha = rs["tags"][m["chosen"]]
            cands.append(candidate("explicit_text_version", pin=make_pin(
                "tag", sha, f"{m['chosen']} -> {_sha12(sha)}", "explicit_text_version", immutable=True,
                name=m["chosen"], ref=f"refs/tags/{m['chosen']}", version_text=pv["version"]),
                source=pv["mention"], via="package version mapped by tag name"))
        else:
            cands.append(candidate("explicit_text_version", error=f"no tag matches package version {pv['version']}",
                                   source=pv["mention"], wanted=pv["version"]))
    if r.get("_explicit_ref"):
        er = r["_explicit_ref"]
        if SHA_RE.match(er) and not rs["tags"].get(er) and not rs["heads"].get(er):
            gd = ctx.git.objects(url, need=[er] if len(er) == 40 else None)
            full = ctx.git.commit_of(gd, er) if gd is not None else None
            cands.append(candidate("immutable_in_reference", pin=make_pin(
                "commit", full, f"commit {_sha12(full)}", "immutable_in_reference", immutable=True), source="explicit")
                if full else candidate("immutable_in_reference", error=f"commit {er} not found", source="explicit",
                                       wanted=er))
        else:
            cands.append(pin_named(er, "explicit_text_version", "explicit_floating_intent", "explicit", hint_="none"))
    # rank 3: explicit floating intent ("main", "latest", "en son"; /releases/latest)
    for q in _q(r, "floating"):
        br = (q.get("normalized") or {}).get("branch")
        if br and br in (rs.get("heads") or {}):
            sha = rs["heads"][br]
            cands.append(candidate("explicit_floating_intent", pin=make_pin(
                "branch", sha, f"branch {br} -> {_sha12(sha)} at {ctx.now_iso}", "explicit_floating_intent",
                immutable=False, name=br, ref=f"refs/heads/{br}", retrieved_at=ctx.now_iso), source=q["id"]))
        elif r["class"] == "release_latest" or re.search(r"latest|newest|son|yeni|guncel|güncel|current",
                                                          tn.fold_tr(q["text"])):
            cands.append(_latest_release(ctx, rs, name, "explicit_floating_intent", q["id"]))
        else:
            cands.append(_default_head(ctx, rs, "explicit_floating_intent", q["id"]))
    if r["class"] == "release_latest":
        cands.append(_latest_release(ctx, rs, name, "explicit_floating_intent", "url"))
    # rank 4 / 6: the ref in the URL (PR/MR heads, compare ranges, tags, branches)
    cmp = req.get("compare")
    if cmp:
        base = pin_named(cmp["base"], "url_tag", "url_branch", "url")
        head = pin_named(cmp["head"], "url_tag", "url_branch", "url")
        if base.get("pin") and head.get("pin"):
            both_tags = base["basis"] == "url_tag" and head["basis"] == "url_tag"
            basis = "url_tag" if both_tags else "url_branch"
            hp, bp = head["pin"], base["pin"]
            gd = ctx.git.objects(url, need=[bp["value"], hp["value"]])
            mb = ctx.git.merge_base(gd, bp["value"], hp["value"]) if gd is not None else None
            p = make_pin("compare", hp["value"], f"{cmp['base']}{'...' if cmp['three_dot'] else '..'}{cmp['head']} "
                         f"({_sha12(bp['value'])}..{_sha12(hp['value'])})", basis, immutable=both_tags,
                         name=cmp["head"], ref=hp.get("ref"), retrieved_at=ctx.now_iso,
                         compare={"base": bp, "head": hp, "merge_base": mb, "three_dot": cmp["three_dot"]})
            if not both_tags:
                p["kind"] = "branch"  # a branch end moves: the range is floating
            cands.append(candidate(basis, pin=p, source="url"))
        else:
            bad = base if not base.get("pin") else head
            cands.append(candidate("url_tag", error=bad["error"], source="url", wanted=bad.get("wanted")))
    elif r["class"] in ("pull_request", "merge_request") or req.get("url_ref_kind") in ("pr_head", "mr_head"):
        n = req.get("number")
        gitlab = r["class"] == "merge_request"
        head = ctx.git.pull_head(url, n, gitlab=gitlab) if n else None
        if head:
            label = f"{'MR !' if gitlab else 'PR #'}{n} head"
            cands.append(candidate("url_pr_head", pin=make_pin(
                "pr_head", head, f"{label} {_sha12(head)} at {ctx.now_iso}", "url_pr_head", immutable=False,
                name=label, ref=f"refs/{'merge-requests' if gitlab else 'pull'}/{n}/head", retrieved_at=ctx.now_iso),
                source="url"))
            if req.get("commit"):
                gd = ctx.git.objects(url, need=[req["commit"], head])
                anc = ctx.git.is_ancestor(gd, req["commit"], head) if gd is not None else None
                full = ctx.git.commit_of(gd, req["commit"]) if gd is not None else req["commit"]
                if anc is False and full != head:
                    r["mismatches"].append(mismatch(
                        "M6", f"the URL names commit {_sha12(req['commit'])}, which is not in the history of the "
                              f"current {label} {_sha12(head)} (force-pushed or rewritten)",
                        "the commit named in the URL is pinned; the current head is kept as an alternate"))
        elif n and not req.get("commit"):
            cands.append(candidate("url_pr_head", error=f"{'refs/merge-requests' if gitlab else 'refs/pull'}/{n}/head "
                                   f"does not exist in {ident.get('canonical_url')}", source="url",
                                   wanted=f"{'MR !' if gitlab else 'PR #'}{n}",
                                   what=f"{'MR !' if gitlab else 'PR #'}{n}"))
    elif req.get("ref_candidates") and not req.get("commit"):
        found = None
        for ref_name, sub in req["ref_candidates"]:
            c = pin_named(ref_name, "url_tag", "url_branch", "url")
            if c.get("pin"):
                found = c
                req["path"] = sub  # the split of <ref>/<path> whose ref exists
                req["url_ref"] = ref_name
                req["url_ref_kind"] = c["pin"]["kind"]
                break
        if found:
            cands.append(found)
        else:
            tried = ", ".join(x[0] for x in req["ref_candidates"][:3])
            cands.append(candidate("url_tag", error=f"the ref named in the URL ({tried}) is neither a branch nor a tag "
                                   f"of {ident.get('canonical_url')}", source="url", wanted=tried))
    # rank 5: the version the local project uses
    loc = _local_for_repo(ctx, r)
    if loc is not None:
        c = None
        if loc.get("vcs_commit"):
            gd = ctx.git.objects(url)
            full = ctx.git.commit_of(gd, loc["vcs_commit"]) if gd is not None else None
            c = candidate("local_project_version", pin=make_pin(
                "commit", full or loc["vcs_commit"], f"locally used commit {_sha12(full or loc['vcs_commit'])} "
                f"({loc['locator']})", "local_project_version", immutable=True, local=loc), source="local")
        elif loc.get("version"):
            m = vermod.match_tags(loc["version"], tags, name=name)
            if m["chosen"]:
                sha = rs["tags"][m["chosen"]]
                c = candidate("local_project_version", pin=make_pin(
                    "tag", sha, f"{m['chosen']} -> {_sha12(sha)} (local {loc['version']}, {loc['locator']})",
                    "local_project_version", immutable=True, name=m["chosen"], ref=f"refs/tags/{m['chosen']}",
                    local=loc), source="local")
            else:
                c = candidate("local_project_version", error=f"no tag matches the local version {loc['version']} "
                              f"({loc['locator']})", source="local", blocking=False)
        if c is not None:
            req["local_version"] = {k: loc.get(k) for k in ("version", "source_file", "line", "kind", "vcs_commit")}
            if r.get("_local_via"):
                req["local_version"]["via"] = r["_local_via"]
            cands.append(c)
    # rank 7: a date in the text
    for q in _q(r, "date"):
        until = (q.get("normalized") or {}).get("date_to")
        tip = rs.get("head_sha")
        gd = ctx.git.objects(url, need=[tip] if tip else None) if until and tip else None
        sha = ctx.git.rev_before(gd, f"{until}T23:59:59Z", tip) if gd is not None else None
        if sha:
            cands.append(candidate("text_date", pin=make_pin(
                "commit", sha, f"{rs.get('default_branch') or 'default branch'} at {until}: {_sha12(sha)}",
                "text_date", immutable=True, date=until), source=q["id"]))
        else:
            cands.append(candidate("text_date", error=f"no commit of the default branch before {until} could be "
                                   "read (objects unavailable offline, or none that old)", source=q["id"],
                                   blocking=False))
    # rank 8: the floating default (never for classes that name a version by construction)
    if r["class"] not in REF_REQUIRED:
        cands.append(_default_head(ctx, rs, "default", "default"))
    _finish_choice(ctx, r, cands, questions)
    for col in collisions:
        r["mismatches"].append(mismatch(
            "M5", f"'{col['name']}' is both a branch ({_sha12((rs['heads'] or {}).get(col['name']))}) and a tag "
                  f"({_sha12((rs['tags'] or {}).get(col['name']))})",
            f"followed the {col['kind']} ("
            + ("GitHub shows the branch for tree/blob/compare URLs" if hint == "heads_first"
               else "git and release URLs mean the tag") + "); "
            "the other is kept as an alternate"))
        other = col.get("other_sha")
        if other:
            r["alternates"].append({"pin": make_pin(col["other_kind"], other, f"{col['other_kind']} {col['name']} -> "
                                                    f"{_sha12(other)}", "url_tag" if col["other_kind"] == "tag"
                                                    else "url_branch", immutable=col["other_kind"] == "tag",
                                                    name=col["name"]),
                                    "why": "same name in the other namespace (M5)", "path_relation": None})
    if r["pin"] is not None:
        _mismatch_git(ctx, r, cands, url)


def _default_head(ctx: _Ctx, rs: dict, basis: str, source: str) -> dict:
    sha, br = rs.get("head_sha"), rs.get("default_branch")
    if not sha:
        return candidate(basis, error="the repository has no default-branch HEAD", source=source, blocking=False)
    kind = "default_head" if basis == "default" else "branch"
    return candidate(basis, pin=make_pin(kind, sha, f"default branch {br or '?'} HEAD {_sha12(sha)} at {ctx.now_iso}",
                                         basis, immutable=False, name=br, ref=f"refs/heads/{br}" if br else "HEAD",
                                         retrieved_at=ctx.now_iso), source=source)


def _latest_release(ctx: _Ctx, rs: dict, name: str | None, basis: str, source: str) -> dict:
    tags = list(rs.get("tags") or {})
    newest = vermod.newest(tags, name=name)
    if not newest:
        return candidate(basis, error="the repository has no release tags to take the latest of", source=source)
    sha = rs["tags"][newest]
    return candidate(basis, pin=make_pin("latest_release", sha, f"latest release {newest} -> {_sha12(sha)} as of "
                                         f"{ctx.now_iso}", basis, immutable=False, name=newest,
                                         ref=f"refs/tags/{newest}", retrieved_at=ctx.now_iso), source=source)


def _local_for_repo(ctx: _Ctx, r: dict) -> dict | None:
    ident = r["identity"]
    if ident.get("canonical_url"):
        it = localmod.find_by_repo(ctx.local, ident["canonical_url"])
        if it is not None:
            r.setdefault("_local_via", "lock/installed VCS source is this repository")
            return it
    pkg = ident.get("package") or {}
    if pkg.get("ecosystem") not in (None, "unknown", "runtime"):
        it = localmod.best(ctx.local, pkg["ecosystem"], pkg["name"])
        if it is not None and (it.get("version") or it.get("vcs_commit")):
            return it
    if ident.get("repo"):
        hits = [h for h in localmod.find_by_name(ctx.local, ident["repo"]) if h.get("version") or h.get("vcs_commit")]
        if hits:
            r.setdefault("_local_via", "a local dependency has the repository's name (heuristic)")
            return hits[0]
    return None


def _finish_choice(ctx: _Ctx, r: dict, cands: list[dict], questions: list[dict]) -> None:
    # a URL branch drops below the local version only for local-behaviour questions about a local dependency
    has_local = any(c["basis"] == "local_project_version" and c.get("pin") for c in cands)
    demote = ctx.local_intent and has_local
    win, alts, blocker = choose(cands, demote_branch=demote)
    r["_cands"] = cands
    if blocker is not None:
        r["status"] = "unresolved"
        r["mismatches"].append(mismatch(
            "M10", blocker["error"], "not pinned: a named version never falls back to another ref (I2)",
            mention=blocker.get("source") if str(blocker.get("source", "")).startswith(("m", "e")) else None))
        near = blocker.get("near") or []
        _unresolved(r, blocker.get("what") or f"version {blocker.get('wanted')}", blocker["error"],
                    "name an existing tag, branch or commit" + (f" (e.g. {', '.join(near[-3:])})" if near else ""),
                    reason="version_not_found")
        questions.append(_question(ctx, r["id"], "version_missing", blocker.get("wanted") or "?",
                                   near[-4:] or [a["pin"]["display"] for a in alts[:3]]))
        r["alternates"] = [{"pin": a["pin"], "why": _why_lost(a, blocker), "path_relation": None} for a in alts]
        return
    if win is None:
        if r["class"] in REF_REQUIRED:
            r["status"] = "refused"
            what = r["class"].replace("_", " ")
            _unresolved(r, "ref", f"a {what} names a version by construction, but none could be "
                        "read from it; refusing to use the default branch", "give the full URL with its ref/number",
                        reason="ref_required")
        else:
            r["status"] = "unresolved"
            errs = [c["error"] for c in cands if c.get("error")]
            _unresolved(r, "pin", "; ".join(errs[:2]) or "nothing to pin", "name a version, tag or commit")
        return
    r["pin"] = dict(win["pin"])
    r["pin"]["basis"] = win["basis"]
    if win.get("via"):
        r["pin"]["via"] = win["via"]
    r["status"] = "pinned_floating" if is_floating(r["pin"]) else "pinned"
    if win["basis"] == "default":
        r["warnings"].append("W_floating: no version was named; pinned to "
                             + {"default_head": "the default-branch HEAD", "latest_version": "the newest release",
                                "latest_arxiv": "the latest arXiv version",
                                "content_at_retrieval": "the content at retrieval time",
                                "floating_doc": "the floating docs version"}.get(r["pin"]["kind"], "a floating version")
                             + f" as of {ctx.now_iso}")
    elif is_floating(r["pin"]) and win["basis"] not in ("explicit_floating_intent", "url_branch"):
        r["warnings"].append(f"W_floating: {r['pin']['display']} moves over time (resolved as of {ctx.now_iso})")
    seen = {r["pin"].get("value")}
    r["alternates"] = []
    for a in alts:
        if a["basis"] == "default" and a["pin"].get("value") in seen:
            continue  # the floating default adds nothing when another candidate already names that commit
        seen.add(a["pin"].get("value"))
        r["alternates"].append({"pin": a["pin"], "why": _why_lost(a, win), "path_relation": None})
    for a in alts:
        if a["basis"] == "text_date" and a["pin"].get("value") != r["pin"].get("value"):
            r["warnings"].append(f"the text names a date ({a['pin'].get('date')}); by the precedence ladder "
                                 f"{win['basis']} wins, the dated commit {_sha12(a['pin'].get('value'))} "
                                 "is an alternate")
    for c in cands:
        if c.get("error") and not c.get("pin") and c is not win and c.get("blocking", True) is False:
            r["warnings"].append(c["error"])


def _why_lost(a: dict, win: dict) -> str:
    return f"{a['basis']} (rank {a['rank']}) loses to {win['basis']} (rank {win['rank']})"


def _mismatch_git(ctx: _Ctx, r: dict, cands: list[dict], url: str) -> None:
    pin, req = r["pin"], r["requested"]
    by = {}
    for c in cands:
        if c.get("pin"):
            by.setdefault(c["basis"], c["pin"])
    url_pin = by.get("url_tag") or by.get("url_branch") or by.get("url_pr_head")
    text_pin = by.get("explicit_text_version")
    local_pin = by.get("local_project_version")
    if text_pin and url_pin and text_pin["value"] != url_pin["value"]:
        r["mismatches"].append(mismatch(
            "M1", f"the text says {text_pin.get('version_text') or text_pin.get('name')} (-> {text_pin['name']}), the "
                  f"link points at {url_pin['kind']} '{url_pin.get('name')}' ({_sha12(url_pin['value'])})",
            f"pinned {pin['display']} ({pin['basis']}); the linked ref is kept as an alternate"))
    if local_pin and local_pin["value"] != pin["value"] and pin["basis"] != "local_project_version":
        other = url_pin if url_pin and url_pin["value"] == pin["value"] else pin
        r["mismatches"].append(mismatch(
            "M2", f"{other['display']} differs from the version the project uses: {local_pin['display']}",
            "the local version is kept as an alternate; say which one the question is about"))
    elif local_pin and url_pin and local_pin["value"] != url_pin["value"] and pin["basis"] == "local_project_version":
        r["mismatches"].append(mismatch(
            "M2", f"the link points at {url_pin['display']}, the project uses {local_pin['display']}",
            "pinned the local version (the question is about local behaviour); the linked ref is an alternate"))
    path = req.get("path")
    if path and r["class"] in ("git_file", "git_repo", "git_commit", "release", "git_archive"):
        gd = ctx.git.objects(url, need=[pin["value"]] + ([url_pin["value"]] if url_pin else []))
        if gd is None:
            _unresolved(r, f"{path} at {pin.get('name') or _sha12(pin['value'])}",
                        "the repository objects are not available offline, so the path was not checked",
                        "run with --network cache to create a blobless mirror (trees only)", reason="offline")
        else:
            here = ctx.git.path_exists(gd, pin["value"], path)
            if here is False:
                same = ctx.git.same_basename(gd, pin["value"], path)
                renamed = ctx.git.renamed_to(gd, pin["value"], url_pin["value"], path) if url_pin else None
                r["mismatches"].append(mismatch(
                    "M1b", f"{path} does not exist at {pin.get('name') or _sha12(pin['value'])}"
                           + (f"; same-name file(s) there: {', '.join(same)}" if same else ""),
                    "cite the file that exists at the pinned version"
                    + (f" (renamed from {renamed})" if renamed else ""),
                    same_name=same or None, renamed_from=renamed))
                _unresolved(r, f"{path} at {pin.get('name') or _sha12(pin['value'])}", "the path is missing at the pin",
                            f"read {same[0]} at the pin instead" if same else "find the file by name at the pin",
                            reason="path_missing_at_pin")
            elif here is None:
                _unresolved(r, f"{path} at {_sha12(pin['value'])}", "the pinned commit is not in the available objects",
                            "fetch the commit (network) and resolve again", reason="offline")
            else:
                blob = ctx.git.blob_id(gd, pin["value"], path)
                if blob:
                    lines = req.get("lines")
                    r["artifacts"].append({"path": path, "blob": blob, "swhid": (
                        f"swh:1:cnt:{blob};origin={r['identity'].get('canonical_url')};anchor=swh:1:rev:{pin['value']};"
                        f"path=/{path}" + (f";lines={lines[0]}-{lines[1]}" if lines else ""))})
            for alt in r["alternates"]:
                ap = alt["pin"]
                if not ap.get("value") or ap.get("kind") in ("compare",):
                    continue
                there = ctx.git.path_exists(gd, ap["value"], path)
                if here is False and there:
                    alt["path_relation"] = "missing_at_primary"
                elif here and there is False:
                    alt["path_relation"] = "missing_at_alternate"
                elif here and there:
                    same_blob = ctx.git.blob_id(gd, ap["value"], path) == ctx.git.blob_id(gd, pin["value"], path)
                    alt["path_relation"] = "identical" if same_blob else "differs"
    lines = req.get("lines")
    if lines and url_pin and url_pin["value"] != pin["value"]:
        r["mismatches"].append(mismatch(
            "M1c", f"lines L{lines[0]}-L{lines[1]} belong to {url_pin['kind']} '{url_pin.get('name')}', not to the pin "
                   f"{pin.get('name') or _sha12(pin['value'])}",
            "the line numbers are not carried over; locate the code by symbol in the pinned checkout"))
        _unresolved(r, f"lines {lines[0]}-{lines[1]} at the pin", "line numbers refer to the linked ref",
                    "trace the symbol at the pin: verinoda research <ref> --topic <symbol>", reason="lines_other_ref")


# -- packages / runtimes -----------------------------------------------------------------------------------

def _resolve_package(ctx: _Ctx, r: dict, questions: list[dict]) -> None:
    pkg = r["identity"].get("package") or {}
    eco, name = pkg.get("ecosystem"), pkg.get("name")
    req = r["requested"]
    if eco in (None, "unknown"):
        r["status"] = "unresolved"
        _unresolved(r, f"package {name}", "the ecosystem of this name is unknown", "give a package spec (pkg==x.y)")
        return
    info = ctx.registry(reg.package_versions, eco, name)
    avail = info.get("versions") if info and info.get("ok") else None
    rel = (info or {}).get("releases") or {}
    cands: list[dict] = []

    def ver_pin(v: str, basis: str, source: str, **extra) -> dict:
        found = None
        if avail is not None:
            found = next((x for x in avail if vermod.equal(x, v)), None)
            if found is None:
                return candidate(basis, error=f"{eco} has no {name} {v}", source=source, wanted=v,
                                 near=[x for x in avail if vermod.in_series(x, ".".join(v.split(".")[:2]))][-4:])
        disp = f"{name} {found or v}" + ("" if avail is not None else " (existence not verified: registry "
                                                                       f"{(info or {}).get('reason', 'unavailable')})")
        return candidate(basis, pin=make_pin("version", found or v, disp, basis, immutable=True, name=found or v,
                                             **extra), source=source)

    if req.get("version_text") and req.get("version_exact"):
        cands.append(ver_pin(req["version_text"], "immutable_in_reference", "reference"))
    for q in _q_versions(r):
        if avail is not None:
            m = [x for x in avail if vermod.equal(x, q["text"])] or sorted(
                [x for x in avail if vermod.in_series(x, q["text"])], key=vermod.sort_key)
            if m:
                cands.append(ver_pin(m[-1] if not vermod.equal(m[0], q["text"]) else m[0], "explicit_text_version",
                                     q["id"], version_text=q["text"]))
                continue
        cands.append(ver_pin(q["text"], "explicit_text_version", q["id"], version_text=q["text"]))
    for pv in r.get("_package_versions") or []:
        cands.append(ver_pin(pv["version"], "explicit_text_version", pv["mention"]))
    if _q(r, "floating"):
        if avail:
            newest = (info or {}).get("latest") or vermod.newest(avail)
            cands.append(candidate("explicit_floating_intent", pin=make_pin(
                "latest_version", newest, f"{name} {newest} (latest as of {ctx.now_iso})", "explicit_floating_intent",
                immutable=False, name=newest, retrieved_at=ctx.now_iso), source=_q(r, "floating")[0]["id"]))
        else:
            cands.append(candidate("explicit_floating_intent", error=f"the latest {name} version needs the registry "
                                   f"({(info or {}).get('reason')})", source=_q(r, "floating")[0]["id"],
                                   blocking=False))
    loc = localmod.best(ctx.local, eco, name)
    spec_text = req.get("version_spec") if not req.get("version_exact") else None
    local_fits = None
    if loc is not None and (loc.get("version") or loc.get("vcs_commit")):
        v = loc.get("version") or loc.get("vcs_commit")
        req["local_version"] = {k: loc.get(k) for k in ("version", "source_file", "line", "kind", "vcs_commit")}
        local_fits = vermod.satisfies(spec_text, str(v), ecosystem=eco) if spec_text else True
        if local_fits is False:
            # the text names a range the project's version is outside of: never pin the local one in its place
            r["mismatches"].append(mismatch(
                "M2", f"the project uses {name} {v} ({loc['locator']}), outside the requested range {spec_text}",
                "pinned the newest version inside the range; the local version is not what was asked about"))
        else:
            cands.append(candidate("local_project_version", pin=make_pin(
                "version", v, f"{name} {v} ({loc['kind']}, {loc['locator']})", "local_project_version",
                immutable=True, name=v, local=loc), source="local"))
    if avail:
        pool = [x for x in avail if vermod.satisfies(spec_text, x, ecosystem=eco)] if spec_text else list(avail)
        pool = [x for x in pool if not (rel.get(x) or {}).get("yanked")]
        newest = vermod.newest(pool) if pool else None
        if newest and spec_text:
            # a range in the reference is a version the user named: its newest member, dated
            cands.append(candidate("explicit_text_version", pin=make_pin(
                "latest_version", newest, f"{name} {newest} (newest satisfying {spec_text} as of {ctx.now_iso})",
                "explicit_text_version", immutable=False, name=newest, retrieved_at=ctx.now_iso, range=spec_text),
                source="reference"))
        elif newest:
            cands.append(candidate("default", pin=make_pin(
                "latest_version", newest, f"{name} {newest} (newest as of {ctx.now_iso})", "default",
                immutable=False, name=newest, retrieved_at=ctx.now_iso), source="registry"))
        elif spec_text:
            cands.append(candidate("explicit_text_version", error=f"no published {name} version satisfies {spec_text}",
                                   source="reference", wanted=spec_text))
    elif spec_text and local_fits is False:
        r["status"] = "unresolved"
        _unresolved(r, f"{name} {spec_text}", f"the range needs the registry ({(info or {}).get('reason')}) and the "
                    "local version is outside it", "name an exact version, or run with --network cache",
                    reason=(info or {}).get("reason") or "offline")
        return
    elif not cands:
        r["status"] = "unresolved"
        reason = (info or {}).get("reason") or "offline"
        _unresolved(r, f"version of {name}", f"no version named, no local version, and the registry is not available "
                    f"({reason})", "name the version, or run with --network cache", reason=reason)
        return
    _finish_choice(ctx, r, cands, questions)
    if r["pin"] is None:
        return
    by = {c["basis"]: c["pin"] for c in cands if c.get("pin")}
    loc_pin = by.get("local_project_version")
    if loc_pin and r["pin"]["basis"] != "local_project_version" and not vermod.equal(str(loc_pin["value"]),
                                                                                     str(r["pin"]["value"])):
        r["mismatches"].append(mismatch(
            "M2", f"{name} {r['pin']['value']} ({r['pin']['basis']}) differs from the version the project uses: "
                  f"{loc_pin['display']}", "the local version is kept as an alternate"))
    v = str(r["pin"]["value"])
    rinfo = next((rel[x] for x in rel if vermod.equal(x, v)), None)
    if rinfo and (rinfo.get("yanked") or rinfo.get("deprecated")):
        r["mismatches"].append(mismatch(
            "M9", f"{name} {v} is {'yanked' if rinfo.get('yanked') else 'deprecated'}"
                  + (f": {rinfo.get('yanked_reason') or rinfo.get('deprecated')}"
                     if rinfo.get("yanked_reason") or rinfo.get("deprecated") else ""),
            "the version is still what was asked for; its behaviour may be known-bad"))
    if info and info.get("ok") and eco == "pypi":
        repo_url = reg.source_repo_from_urls(info.get("project_urls"), info.get("home_page"))
        if repo_url:
            r["mapping"].append({"repo": repo_url, "method": "registry_pointer", "strength": "pointer",
                                 "why": "PyPI project_urls names this repository (metadata only, not verification)"})
            r["identity"].setdefault("source_repo", repo_url)


def _resolve_runtime(ctx: _Ctx, r: dict, questions: list[dict]) -> None:
    rt = (r["identity"].get("package") or {}).get("name")
    cands: list[dict] = []
    for q in _q_versions(r):
        cands.append(candidate("explicit_text_version", pin=make_pin(
            "version", vermod.normalize_version(q["text"]), f"{rt} {vermod.normalize_version(q['text'])}",
            "explicit_text_version", immutable=True, name=q["text"]), source=q["id"]))
    lr = _local_runtime(ctx, rt)
    if lr:
        cands.append(candidate("local_project_version", pin=make_pin(
            "version", lr["version"], f"{rt} {lr['version']} ({lr['locator']})", "local_project_version",
            immutable=True, name=lr["version"], local=lr), source="local"))
        r["requested"]["local_version"] = {k: lr.get(k) for k in ("version", "source_file", "line", "kind")}
    if not cands:
        r["status"] = "unresolved"
        _unresolved(r, f"{rt} version", "no version named and no runtime pin in the project",
                    "say which version, or add .python-version / requires-python")
        return
    _finish_choice(ctx, r, cands, questions)
    by = {c["basis"]: c["pin"] for c in cands if c.get("pin")}
    t, lp = by.get("explicit_text_version"), by.get("local_project_version")
    if t and lp and not _same_series(t["value"], lp["value"]):
        r["mismatches"].append(mismatch("M2", f"the text says {rt} {t['value']}, the project uses {lp['display']}",
                                        "pinned the text version; the local runtime is an alternate"))


def _local_runtime(ctx: _Ctx, rt: str | None) -> dict | None:
    items = (ctx.local.get("runtime") or {}).get(rt or "") or []
    concrete = [i for i in items if re.fullmatch(r"\d+(?:\.\d+){0,2}", str(i.get("version") or ""))]
    return concrete[0] if concrete else None


def _same_series(a: str, b: str) -> bool:
    a, b = str(a), str(b)
    n = min(len(a.split(".")), len(b.split(".")), 2)
    return a.split(".")[:n] == b.split(".")[:n]


# -- papers, DOIs, SWH ----------------------------------------------------------------------------------------

def _resolve_paper(ctx: _Ctx, r: dict, questions: list[dict]) -> None:
    paper = r["identity"]["paper"]
    aid = paper.get("arxiv_id")
    req = r["requested"]
    cands: list[dict] = []
    info = ctx.registry(reg.arxiv_latest, aid)
    latest = info.get("latest") if info.get("ok") else None
    if info.get("ok"):
        paper["title"] = info.get("title")
    if req.get("version_text"):
        v = int(req["version_text"].lstrip("vV"))
        cands.append(candidate("immutable_in_reference", pin=make_pin(
            "arxiv_version", f"{aid}v{v}", f"arXiv:{aid}v{v}", "immutable_in_reference", immutable=True, name=f"v{v}"),
            source="reference"))
    for q in _q_versions(r):
        m = re.fullmatch(r"v(\d+)", q["text"], re.I)
        if m:
            cands.append(candidate("explicit_text_version", pin=make_pin(
                "arxiv_version", f"{aid}v{m.group(1)}", f"arXiv:{aid}v{m.group(1)}", "explicit_text_version",
                immutable=True, name=f"v{m.group(1)}"), source=q["id"]))
    for q in _q(r, "date"):
        vs = ctx.registry(reg.arxiv_versions, aid)
        until = (q.get("normalized") or {}).get("date_to")
        pick = [x for x in vs.get("versions", []) if x.get("date") and x["date"] <= until] if vs.get("ok") else []
        if pick:
            v = pick[-1]["version"]
            cands.append(candidate("text_date", pin=make_pin("arxiv_version", f"{aid}v{v}", f"arXiv:{aid}v{v} "
                                                             f"(newest version dated <= {until})", "text_date",
                                                             immutable=True, name=f"v{v}"), source=q["id"]))
        else:
            cands.append(candidate("text_date", error=f"no arXiv version dated <= {until} could be read "
                                   f"({vs.get('reason')})", source=q["id"], blocking=False))
    if latest:
        basis = "explicit_floating_intent" if _q(r, "floating") else "default"
        cands.append(candidate(basis, pin=make_pin("latest_arxiv", f"{aid}v{latest}", f"arXiv:{aid}v{latest} (latest "
                                                   f"as of {ctx.now_iso})", basis, immutable=False,
                                                   name=f"v{latest}", retrieved_at=ctx.now_iso), source="registry"))
    if not cands:
        r["status"] = "unresolved"
        _unresolved(r, f"arXiv:{aid} version", "the id has no version and the arXiv API is not available "
                    f"({info.get('reason') or 'offline'})",
                    "cite the version (e.g. arXiv:<id>v1) or run with --network cache", reason="offline")
        return
    _finish_choice(ctx, r, cands, questions)
    if r["pin"] is None:
        return
    pv = int(str(r["pin"]["name"]).lstrip("v"))
    if not req.get("version_text") and not _q_versions(r):
        r["mismatches"].append(mismatch("M4", f"arXiv:{aid} was cited without a version; it resolves to "
                                              f"v{pv}" + (f" today (v{latest} is the latest)" if latest else ""),
                                        "the version read is recorded; cite it with vN to make it stable"))
    elif latest and pv < latest:
        r["mismatches"].append(mismatch("M4", f"arXiv:{aid}v{pv} is not the latest version (v{latest})",
                                        "the cited version is used; later versions may differ"))


def _resolve_fixed(ctx: _Ctx, r: dict, questions: list[dict]) -> None:
    cls = r["class"]
    if cls == "doi":
        doi = r["identity"]["paper"]["doi"]
        r["pin"] = make_pin("doi", doi, f"doi:{doi}", "immutable_in_reference", immutable=True)
    else:
        sw = r["identity"]["swhid"]
        r["pin"] = make_pin("swhid", sw["core"], sw["core"], "immutable_in_reference", immutable=True)
    r["status"] = "pinned"
    for q in r["_qual"]:
        if q["kind"] in ("version", "floating", "date"):
            r["warnings"].append(f"'{q['text']}' ignored: the reference is already immutable")


# -- documentation ---------------------------------------------------------------------------------------------

def _versioned_docs_url(url: str, slot: str | None, new: str) -> str:
    if slot is None:
        return url
    return re.sub(rf"/{re.escape(slot)}/", f"/{new}/", url, count=1) if f"/{slot}/" in url else url


def _resolve_docs(ctx: _Ctx, r: dict, questions: list[dict]) -> None:
    req, docs = r["requested"], r["identity"].get("docs") or {}
    url, slot, floating = req.get("url"), req.get("docs_version"), req.get("floating_slot")
    site = docs.get("site")
    cands: list[dict] = []
    if slot and not floating:
        cands.append(candidate("url_tag", pin=make_pin("docs_version", slot, f"{site or 'docs'} {slot} ({url})",
                                                       "url_tag", immutable=False, name=slot, url=url), source="url"))
    elif slot is not None or site in ("python", "rtd"):
        pin = make_pin("floating_doc", slot or "", f"{site or 'docs'} '{slot or '(no version)'}' as served on "
                       f"{ctx.now_iso} ({url})", "url_branch", immutable=False, name=slot, url=url,
                       retrieved_at=ctx.now_iso)
        if site == "python" and ctx.network != "off":
            info = ctx.registry(reg.python_docs_release, url)
            if info.get("ok"):
                pin["release"] = info["release"]
                pin["display"] = f"Python {info['release']} docs (/{slot}/ as served on {ctx.now_iso})"
        cands.append(candidate("url_branch", pin=pin, source="url"))
    for q in _q_versions(r):
        v = vermod.normalize_version(q["text"])
        new_slot = ".".join(v.split(".")[:2]) if site == "python" else q["text"]
        cands.append(candidate("explicit_text_version", pin=make_pin(
            "docs_version", new_slot, f"{site or 'docs'} {new_slot} ({_versioned_docs_url(url, slot, new_slot)})",
            "explicit_text_version", immutable=False, name=new_slot, url=_versioned_docs_url(url, slot, new_slot)),
            source=q["id"]))
    if site == "python":
        lr = _local_runtime(ctx, "python")
        if lr:
            s = ".".join(str(lr["version"]).split(".")[:2])
            cands.append(candidate("local_project_version", pin=make_pin(
                "docs_version", s, f"Python {s} docs (local runtime {lr['version']}, {lr['locator']})",
                "local_project_version", immutable=False, name=s, url=_versioned_docs_url(url, slot, s), local=lr),
                source="local"))
    if not cands:
        cands.append(candidate("default", pin=make_pin("content_at_retrieval", None, f"{url} as fetched",
                                                       "default", immutable=False, url=url), source="url"))
    _finish_choice(ctx, r, cands, questions)
    if r["pin"] is None:
        return
    by = {c["basis"]: c["pin"] for c in cands if c.get("pin")}
    t, u, lp = by.get("explicit_text_version"), by.get("url_tag") or by.get("url_branch"), by.get(
        "local_project_version")
    if t and u and str(t["name"]) != str(u.get("name")):
        r["mismatches"].append(mismatch("M1", f"the text says {t['name']}, the link is the '{u.get('name')}' docs",
                                        f"reading the {t['name']} docs: {t.get('url')}"))
    if lp and u and u["kind"] == "floating_doc":
        served = u.get("release") or u.get("name")
        if not u.get("release") or not _same_series(u["release"], lp["name"]):
            host_says = "Python " + served if u.get("release") else "version decided by the host"
            r["mismatches"].append(mismatch(
                "M3", f"the link is the floating '{u.get('name')}' docs ({host_says}), "
                      f"the project runs {lp['display']}",
                f"use {lp.get('url')} for the project's version"))
    concrete = t or (u if u and u["kind"] == "docs_version" else None)
    if lp and concrete and not _same_series(concrete["name"], lp["name"]):
        r["mismatches"].append(mismatch("M2", f"{'the text says' if concrete is t else 'the link is'} "
                                              f"{concrete['name']}, the project uses {lp['display']}",
                                        "the local version's docs are kept as an alternate"))
    if site == "rtd" and ctx.network != "off" and r["pin"].get("name"):
        info = ctx.registry(reg.rtd_version, docs.get("project"), str(r["pin"]["name"]))
        if info.get("ok"):
            r["pin"]["rtd"] = {k: info.get(k) for k in ("type", "identifier", "ref", "built", "active")}
            if info.get("type") == "tag" and info.get("identifier"):
                r["pin"]["commit"] = info["identifier"]
            if info.get("built") is False or info.get("active") is False:
                r["mismatches"].append(mismatch("M11", f"Read the Docs does not host a built '{r['pin']['name']}' "
                                                       "version", "read docs/ in the repository at that tag instead"))
        elif info.get("reason") == "not_found":
            r["mismatches"].append(mismatch("M11", f"Read the Docs has no version '{r['pin']['name']}'",
                                            "read docs/ in the repository at that tag instead"))


def _resolve_issue(ctx: _Ctx, r: dict, questions: list[dict]) -> None:
    ident, req = r["identity"], r["requested"]
    n = req.get("number")
    url = _repo_url(r)
    if not url:
        r["status"] = "unresolved"
        _unresolved(r, f"#{n}", "no repository to look the number up in (none in the sentence, no origin remote)",
                    "write it as owner/repo#N or give the URL")
        return
    head = ctx.git.pull_head(url, n) if ident.get("host") not in ("gitlab.com",) else None
    if head:
        r["class"] = "pull_request"
        r["warnings"].append(f"#{n} is a pull request (refs/pull/{n}/head exists)")
        req["url_ref"], req["url_ref_kind"] = f"refs/pull/{n}/head", "pr_head"
        _resolve_git(ctx, r, questions)
        return
    pin = make_pin("content_at_retrieval", None, f"issue #{n} of {ident.get('canonical_url')} (content hash when "
                   "researched)", "default", immutable=False, retrieved_at=ctx.now_iso,
                   url=req.get("url") or f"{ident.get('canonical_url')}/issues/{n}")
    if ident.get("host") == "github.com" and ctx.network != "off" and ident.get("owner"):
        info = ctx.registry(reg.github_issue, ident["owner"], ident["repo"], n)
        if info.get("ok"):
            pin["value"] = info.get("updated_at")
            pin["display"] = f"issue #{n} '{(info.get('title') or '')[:60]}' ({info.get('state')}, updated " \
                             f"{info.get('updated_at')})"
            if info.get("is_pull_request"):
                r["warnings"].append("the API says this number is a pull request")
        elif info.get("reason") == "moved":
            r["mismatches"].append(mismatch("M7", f"issue #{n} moved ({info.get('location')})",
                                            "the moved issue is what the number now refers to"))
        elif info.get("reason") == "not_found":
            r["status"] = "unresolved"
            _unresolved(r, f"#{n}", "neither a pull request nor an issue with this number exists",
                        "check the number and the repository", reason="not_found")
            return
    r["pin"] = pin
    r["status"] = "pinned_floating"
    r["warnings"].append("W_floating: an issue is a discussion that keeps changing; research records its content "
                         "hash and time")


def _resolve_page(ctx: _Ctx, r: dict, questions: list[dict]) -> None:
    url = r["requested"].get("url")
    r["pin"] = make_pin("content_at_retrieval", None, f"{url} (content hash when researched)", "default",
                        immutable=False, url=url)
    r["status"] = "pinned_floating"
    r["warnings"].append("W_floating: a web page has no version; it is pinned by the content hash when fetched")


def _resolve_app(ctx: _Ctx, r: dict, questions: list[dict]) -> None:
    name = r["identity"]["app"]["name"]
    r["status"] = "unresolved"
    r["identity"]["source_availability"] = "unknown"
    _unresolved(r, f"application {name}", "applications are not resolved to a source repository yet; whether its "
                "source is open, partial or closed is unknown",
                f"find the official repository of {name} (if any) and give its URL with a version tag",
                reason="application_not_supported")
    questions.append(_question(ctx, r["id"], "app_source", name, []))


def _resolve_local(ctx: _Ctx, r: dict, questions: list[dict]) -> None:
    if r["class"] == "local_file":
        path = re.sub(r"^\./", "", r["requested"]["path"])
        hits = [path] if (ctx.repo / path).is_file() else _files_ending_with(ctx.repo, path)
        if len(hits) == 1:
            r["requested"]["path_in_project"] = hits[0]
            r["pin"] = make_pin("working_tree", ctx.local_commit, f"{hits[0]} in the local project"
                                + (f" at {_sha12(ctx.local_commit)}" if ctx.local_commit else ""), "local_snapshot",
                                immutable=bool(ctx.local_commit))
            r["pin"]["precedence_rank"] = None
            r["status"] = "pinned"
        elif hits:
            r["status"] = "ambiguous"
            _unresolved(r, path, f"{len(hits)} files of the local project end with this path",
                        "say which one: " + ", ".join(hits[:4]), reason="ambiguous_path")
            questions.append(_question(ctx, r["id"], "binding", path, hits[:4]))
        else:
            r["status"] = "unresolved"
            _unresolved(r, path, "no such file in the local project and no repository reference in its sentence",
                        "give the path relative to the repository root, or the repository URL")
        return
    r["status"] = "partially_resolved"
    _unresolved(r, r["requested"].get("symbol") or r["_anchor"]["text"],
                "symbols are grounded against the code graph by the question plan, not by the reference resolver",
                "verinoda plan check (or analyze) links it to a definition", reason="symbol_grounding_elsewhere")


def _files_ending_with(repo: Path, path: str) -> list[str]:
    """Project files whose path ends with ``/<path>`` (tracked and untracked-not-ignored)."""
    try:
        from verinoda.snapshot import list_files

        files = list_files(repo)
    except Exception:  # noqa: BLE001
        return []
    tail = "/" + path.replace("\\", "/")
    return [f for f in files if f.endswith(tail) or f == path][:10]


def _bind_origin(ctx: _Ctx, r: dict) -> None:
    """A bare #N / !N / PR N with no repository in its sentence: the local project's origin remote."""
    if r["identity"].get("canonical_url"):
        return
    rc, out, _ = run_git(["remote", "get-url", "origin"], cwd=ctx.repo)
    if rc == 0 and out.strip():
        try:
            spec = classify(out.strip())
        except ValueError:
            return
        for k in ("host", "owner", "repo", "canonical_url", "clone_url", "local_path"):
            r["identity"][k] = spec["identity"].get(k)
        r["coreference"] = {"merged": [], "via": ["local_origin_remote"]}


def _resolve_one(ctx: _Ctx, r: dict, questions: list[dict]) -> None:
    cls = r["class"]
    try:
        if cls in ("issue", "pull_request", "merge_request") and not r["identity"].get("canonical_url"):
            _bind_origin(ctx, r)
        if cls == "issue":
            _resolve_issue(ctx, r, questions)
        elif cls in GIT_CLASSES:
            if cls in ("pull_request", "merge_request") and not _repo_url(r):
                r["status"] = "unresolved"
                _unresolved(r, r["_anchor"]["text"], "no repository to look the number up in",
                            "write it as owner/repo#N or give the URL")
            else:
                _resolve_git(ctx, r, questions)
        elif cls == "package":
            _resolve_package(ctx, r, questions)
        elif cls == "runtime":
            _resolve_runtime(ctx, r, questions)
        elif cls == "paper":
            _resolve_paper(ctx, r, questions)
        elif cls in ("doi", "swh_object"):
            _resolve_fixed(ctx, r, questions)
        elif cls == "doc_page":
            _resolve_docs(ctx, r, questions)
        elif cls in ("web_page", "qa_post"):
            _resolve_page(ctx, r, questions)
        elif cls == "application":
            _resolve_app(ctx, r, questions)
        else:
            _resolve_local(ctx, r, questions)
    except CassetteMiss:
        raise
    except Exception as exc:  # noqa: BLE001 - one reference never breaks the others
        r["status"] = "unresolved"
        _unresolved(r, r["_anchor"]["text"], f"resolver error: {type(exc).__name__}: {str(exc)[:200]}",
                    "report this; resolve the reference manually", reason="error")
    for q in r["_qual"]:
        if q["kind"] == "version_candidate" and (q.get("normalized") or {}).get("relative"):
            _unresolved(r, q["text"], "a relative version ('old', 'önceki') does not say which one",
                        "ask which tag, release or date is meant", mention=q["id"], reason="relative_version")
            questions.append(_question(ctx, r["id"], "relative_version", q["text"],
                                       _recent_tags(ctx, r)))
            if r["status"] in ("pinned", "pinned_floating"):
                r["status"] = "ambiguous"


def _recent_tags(ctx: _Ctx, r: dict) -> list[str]:
    url = _repo_url(r)
    if r["class"] not in GIT_CLASSES or not url:
        return []
    rs = ctx.git.refs(url)
    name = r["identity"].get("repo")
    tags = sorted(rs.get("tags") or {}, key=lambda t: vermod.sort_key(vermod.normalize_tag(t, name)))
    return tags[-4:][::-1]


# =============================================================================
# questions, summary, storage
# =============================================================================

_Q = {
    "binding": ("'{x}' birden fazla referansa ait olabilir; hangisi?",
                "'{x}' could belong to more than one reference; which one?"),
    "version_series": ("'{x}' birden fazla sürüme uyuyor; en yüksek sürüm kullanıldı. Doğru mu?",
                       "'{x}' matches several releases; the highest was used. Is that the one?"),
    "version_missing": ("'{x}' bulunamadı; hangisini kastettiniz?",
                        "'{x}' was not found; which one did you mean?"),
    "relative_version": ("'{x}' hangi sürüm, etiket ya da tarih?", "Which version, tag or date is '{x}'?"),
    "app_source": ("{x} için hangi kaynak deposu ve sürüm kullanılsın?",
                   "Which source repository and version of {x} should be used?"),
    "unknown_name": ("'{x}' hangi proje? owner/repo, bağlantı ya da paket (paket==sürüm) olarak yazar mısınız?",
                     "Which project is '{x}'? Please give it as owner/repo, a URL or a package (pkg==version)."),
}


def _question(ctx: _Ctx, rid: str, kind: str, x: str, options: list[str]) -> dict:
    tr, en = _Q[kind]
    lang_tr = ctx.lang in ("tr", "mixed")
    return {"reference": rid, "kind": kind, "question": (tr if lang_tr else en).format(x=x),
            "question_en": en.format(x=x), "options": [str(o) for o in options][:5]}


def _silent_floating(r: dict) -> bool:
    pin = r.get("pin")
    if not pin or not is_floating(pin):
        return False
    if pin["basis"] in ("explicit_floating_intent", "url_branch"):
        return False
    return not any(w.startswith("W_floating") for w in r["warnings"])


def accounted(result: dict) -> tuple[bool, list[str]]:
    """Invariant I1: every mention id is in exactly one reference or in ``unbound_mentions``."""
    ids = [m["id"] for m in result["mentions"]]
    seen: dict[str, int] = {}
    for r in result["references"]:
        for mid in r["mentions"]:
            seen[mid] = seen.get(mid, 0) + 1
    for u in result["unbound_mentions"]:
        seen[u["mention"]] = seen.get(u["mention"], 0) + 1
    bad = [i for i in ids if seen.get(i, 0) != 1]
    return not bad, bad


def _public(r: dict) -> dict:
    out = {k: v for k, v in r.items() if not k.startswith("_")}
    for a in out["alternates"]:
        a.setdefault("swhid", None)
    return out


def _local_commit(repo: Path, store) -> str | None:
    try:
        snap = store.latest_snapshot() if store is not None else None
        if snap and snap.get("commit_sha"):
            return snap["commit_sha"]
    except Exception:  # noqa: BLE001
        pass
    rc, out, _ = run_git(["rev-parse", "HEAD"], cwd=repo)
    return out.strip() if rc == 0 and out.strip() else None


def resolve(store, repo, text: str, *, explicit=(), network: str = "cache", topic: str | None = None, now=None,
            transport: Transport | None = None, git: GitRunner | None = None, local_intent: bool | None = None,
            record: bool = True, max_network_calls: int = 40) -> dict:
    """Resolve every reference in ``text`` (+ ``explicit``) to an exact pin (see the module docstring).

    ``explicit`` items are strings (``URL[@ref]``) or ``{"reference", "ref"}``.
    ``transport``/``git`` are injected by tests (cassettes, local fixture
    repositories); by default they follow ``network``. ``local_intent`` marks
    a question about the local project's behaviour even without a "the
    version we use" phrase (feedback on a local claim).
    """
    if network not in ("off", "cache", "on"):
        raise ValueError(f"network must be off, cache or on, not {network!r}")
    repo = Path(repo).resolve()
    text = text or ""
    nowd = _now(now)
    from verinoda.paths import ensure_atlas, research_dir

    if network != "off" and (transport is None or git is None):
        ensure_atlas(repo)  # caches and mirrors live under .verinoda/ (git-ignored), never loose in the project
    transport = transport if transport is not None else for_mode(repo, network)
    git = git if git is not None else GitRunner(network=network, cache_root=research_dir(repo))
    ctx = _Ctx(store, repo, text, network, nowd, transport, git, local_intent, max_network_calls)
    ctx.local_commit = _local_commit(repo, store)
    mentions = extract(text)
    explicit_m = _explicit_mentions(explicit)
    questions: list[dict] = []
    refs, unbound = _build_references(ctx, mentions, explicit_m)
    _bind(ctx, mentions, refs, unbound, questions)
    for r in refs:
        _resolve_one(ctx, r, questions)
    all_mentions = explicit_m + mentions
    references = [_public(r) for r in refs]
    for r in references:
        r["mentions"] = sorted(dict.fromkeys(r["mentions"]), key=lambda x: (x[0] != "e", int(x[1:])))
    summary = {s: sum(1 for r in references if r["status"] == s) for s in
               ("pinned", "pinned_floating", "partially_resolved", "ambiguous", "unresolved", "refused")}
    mms = [{"reference": r["id"], **m} for r in references for m in r["mismatches"]]
    unres = [{"reference": r["id"], **u} for r in references for u in r["unresolved"]]
    summary.update(mismatches=len(mms), errors=sum(1 for m in mms if m["severity"] == "error"),
                   network_calls=transport.network_calls + git.network_calls, cache_hits=transport.cache_hits,
                   silent_floating_pins=sum(1 for r in refs if _silent_floating(r)),
                   worst_severity=worst_severity([m for r in references for m in r["mismatches"]]))
    result = {
        "schema": SCHEMA, "id": None,
        "input": {"text": text, "explicit": [{"reference": m["text"], "ref": m["normalized"].get("ref")}
                                             for m in explicit_m],
                  "lang": ctx.lang, "input_hash": "sha256:" + hashlib.sha256(
                      json.dumps([text, [m["text"] for m in explicit_m]], ensure_ascii=False).encode("utf-8")
                  ).hexdigest()},
        "context": {"repo": str(repo), "local_commit": ctx.local_commit, "now": ctx.now_iso, "network": network,
                    "local_intent": ctx.local_intent, "local_intent_source": ctx.local_intent_source,
                    "local_sources": ctx._local["sources"] if ctx._local else [],
                    "unsupported_locks": (ctx._local or {}).get("unsupported") or [], "topic": topic},
        "mentions": all_mentions, "references": references, "unbound_mentions": unbound,
        "questions_for_user": questions, "summary": summary,
        "mismatches": mms, "unresolved": unres,
    }
    ok, missing = accounted(result)
    summary["mentions_total"] = len(all_mentions)
    summary["mentions_accounted"] = len(all_mentions) - len(missing)
    if not ok:  # a bug, never a silent drop: say which mentions went missing
        for mid in missing:
            result["unbound_mentions"].append({"mention": mid, "why": "internal: mention lost during resolution"})
    # a name the user wrote as a reference ("Graphify v0.3", "the X package") that nothing resolved
    names_left = [u for u in unbound if u.get("ask")]
    for u in names_left:
        q = _question(ctx, u["mention"], "unknown_name", u["text"], [])
        if not any(x.get("question") == q["question"] for x in questions):
            questions.append(q)
    bad = summary["unresolved"] + summary["refused"] + summary["ambiguous"]
    result["status"] = ("complete" if not bad and not names_left and not questions and not unres
                        and not summary["errors"] else
                        "unresolved" if (references and bad == len(references)) or (names_left and not references)
                        else "partial")
    if store is not None and record:
        from verinoda.store import new_id

        result["id"] = new_id("rrs")
        try:
            store.insert("reference_resolutions", {
                "id": result["id"], "text": text,
                "explicit": result["input"]["explicit"], "network": network, "result": result,
                "created_at": ctx.now_iso})
        except Exception as exc:  # noqa: BLE001 - the answer is still useful without its row
            result["warnings"] = [f"could not record the resolution: {type(exc).__name__}: {exc}"]
    return result
