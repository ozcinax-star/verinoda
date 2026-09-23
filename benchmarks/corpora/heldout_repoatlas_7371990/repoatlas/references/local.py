"""The versions the local project actually uses (docs/DESIGN.md D13), offline.

:func:`local_versions` reads, without running or importing anything:

* lock files - ``uv.lock``, ``pylock.toml`` / ``pylock.*.toml`` (PEP 751),
  ``poetry.lock``, ``Pipfile.lock``, ``requirements*.txt`` ``==`` pins,
  ``package-lock.json`` (v1-v3), ``Cargo.lock``, ``go.mod`` + ``go.sum``;
* installed metadata of the *project's own* virtual environment
  (``.venv`` / ``venv`` / ``env`` with a ``pyvenv.cfg``), read with
  ``importlib.metadata.distributions(path=[site-packages])`` - the files are
  parsed, no project code is imported and RepoAtlas's own interpreter is never
  consulted - including PEP 610 ``direct_url.json`` (VCS commit, editable);
* runtime pins - ``requires-python``, ``.python-version``, ``pyvenv.cfg``,
  ``.nvmrc`` / ``.node-version``, ``package.json`` ``engines``, ``go.mod``
  ``go``/``toolchain``, ``rust-toolchain(.toml)``;
* manifest constraints (``pyproject.toml``, ``package.json``, ``Cargo.toml``)
  as a last resort - a constraint, not a version.

Every item carries ``source_file`` and ``line`` so it can be cited as source
evidence (:func:`evidence_for`) and goes stale like any other claim evidence.
Unsupported lock formats are listed in ``unsupported``, never guessed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - py3.10
    import tomli as tomllib  # type: ignore[no-redef]

KIND_ORDER = {"lock": 0, "installed": 1, "runtime": 2, "manifest": 3}
_VENV_DIRS = (".venv", "venv", "env", ".env")


def norm_name(name: str, ecosystem: str) -> str:
    if ecosystem == "pypi":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name.lower() if ecosystem in ("npm", "cargo") else name


def key(ecosystem: str, name: str) -> str:
    """purl without version: ``pkg:pypi/requests``."""
    return f"pkg:{ecosystem}/{norm_name(name, ecosystem)}"


def _lines(p: Path) -> list[str]:
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _find_line(lines: list[str], rx: str, start: int = 0) -> int:
    r = re.compile(rx)
    for i in range(start, len(lines)):
        if r.search(lines[i]):
            return i + 1
    return 1


def _toml(p: Path) -> dict:
    try:
        return tomllib.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - a broken lock file is reported, not fatal
        return {}


def _git_source(url: str) -> tuple[str | None, str | None, str | None]:
    """``git+https://github.com/o/r?rev=0.10.2#<sha>`` -> (repo url, requested rev, commit)."""
    u = re.sub(r"^git\+", "", url or "")
    commit = None
    if "#" in u:
        u, _, frag = u.partition("#")
        commit = frag if re.fullmatch(r"[0-9a-f]{7,40}", frag) else None
    rev = None
    if "?" in u:
        u, _, q = u.partition("?")
        m = re.search(r"(?:rev|tag|branch)=([^&]+)", q)
        rev = m.group(1) if m else None
    if "@" in u.split("/")[-1]:
        u, _, at = u.rpartition("@")
        rev = rev or at
    return (u.rstrip("/") or None), rev, commit


class _Collector:
    def __init__(self, repo: Path):
        self.repo = repo
        self.packages: dict[str, list[dict]] = {}
        self.runtime: dict[str, list[dict]] = {}
        self.sources: list[str] = []
        self.unsupported: list[dict] = []
        self.problems: list[str] = []

    def rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.repo).as_posix()
        except ValueError:
            return p.as_posix()

    def add(self, ecosystem: str, name: str, version: str | None, p: Path, line: int, kind: str, **extra) -> None:
        item = {"ecosystem": ecosystem, "name": name, "version": version, "source_file": self.rel(p),
                "line": line, "kind": kind, "locator": f"{self.rel(p)}:{line}"}
        item.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
        self.packages.setdefault(key(ecosystem, name), []).append(item)

    def add_runtime(self, runtime: str, version: str, p: Path, line: int, kind: str = "runtime", **extra) -> None:
        item = {"runtime": runtime, "version": version, "source_file": self.rel(p), "line": line, "kind": kind,
                "locator": f"{self.rel(p)}:{line}"}
        item.update({k: v for k, v in extra.items() if v})
        self.runtime.setdefault(runtime, []).append(item)


# -- lock files ------------------------------------------------------------------------------------------

def _uv_lock(c: _Collector, p: Path) -> None:
    data, lines = _toml(p), _lines(p)
    pos = 0
    for pkg in data.get("package") or []:
        name, ver = pkg.get("name"), pkg.get("version")
        src = pkg.get("source") or {}
        line = _find_line(lines, rf'^name = "{re.escape(name or "")}"', pos)
        pos = line
        if not name or src.get("virtual") or src.get("editable") or src.get("directory") or src.get("path"):
            continue  # the project itself or a local path, not a published dependency
        extra: dict = {}
        if src.get("git"):
            extra["vcs_url"], extra["vcs_requested"], extra["vcs_commit"] = _git_source(src["git"])
        if src.get("registry"):
            extra["registry"] = src["registry"]
        sd = pkg.get("sdist") or {}
        if sd.get("hash"):
            extra["artifact_hashes"] = [sd["hash"]]
            extra["upload_time"] = sd.get("upload-time")
        c.add("pypi", name, ver, p, line, "lock", **extra)
    rp = data.get("requires-python")
    if rp:
        c.add_runtime("python", rp, p, _find_line(lines, r"^requires-python"), kind="runtime_constraint")


def _pylock(c: _Collector, p: Path) -> None:
    data, lines = _toml(p), _lines(p)
    pos = 0
    for pkg in data.get("packages") or []:
        name, ver = pkg.get("name"), pkg.get("version")
        if not name:
            continue
        line = _find_line(lines, rf'^name = "{re.escape(name)}"', pos)
        pos = line
        vcs = pkg.get("vcs") or {}
        c.add("pypi", name, ver, p, line, "lock", vcs_url=vcs.get("url"), vcs_commit=vcs.get("commit-id"),
              vcs_requested=vcs.get("requested-revision"))


def _poetry_lock(c: _Collector, p: Path) -> None:
    data, lines = _toml(p), _lines(p)
    pos = 0
    for pkg in data.get("package") or []:
        name, ver = pkg.get("name"), pkg.get("version")
        if not name:
            continue
        line = _find_line(lines, rf'^name = "{re.escape(name)}"', pos)
        pos = line
        src = pkg.get("source") or {}
        extra = {}
        if src.get("type") == "git":
            extra = {"vcs_url": src.get("url"), "vcs_requested": src.get("reference"),
                     "vcs_commit": src.get("resolved_reference")}
        elif src.get("type") in ("directory", "file"):
            continue
        c.add("pypi", name, ver, p, line, "lock", **extra)


def _pipfile_lock(c: _Collector, p: Path) -> None:
    lines = _lines(p)
    try:
        data = json.loads("\n".join(lines))
    except ValueError:
        c.problems.append(f"{c.rel(p)}: not valid JSON")
        return
    for section in ("default", "develop"):
        for name, info in (data.get(section) or {}).items():
            info = info or {}
            ver = (info.get("version") or "").lstrip("=") or None
            line = _find_line(lines, rf'^\s*"{re.escape(name)}": \{{')
            extra = {}
            if info.get("git"):
                extra = {"vcs_url": info["git"], "vcs_requested": info.get("ref"), "vcs_commit": info.get("ref")
                         if re.fullmatch(r"[0-9a-f]{40}", info.get("ref") or "") else None}
            c.add("pypi", name, ver, p, line, "lock", scope=section, **extra)


def _requirements(c: _Collector, p: Path) -> None:
    lines = _lines(p)
    for i, raw in enumerate(lines, 1):
        s = raw.split(" #")[0].strip()
        if not s or s.startswith(("#", "-r", "-c", "--", "-e .")):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*@\s*(git\+\S+)", s)
        if m:
            url, rev, commit = _git_source(m.group(2))
            c.add("pypi", m.group(1), None, p, i, "lock" if commit else "manifest", vcs_url=url, vcs_requested=rev,
                  vcs_commit=commit)
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*===?\s*([\w.+!-]+)", s)
        if m:
            c.add("pypi", m.group(1), m.group(2), p, i, "lock", hashed="--hash" in raw or None)
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*((?:[<>=!~]=?|===)\s*[\w.*+!-]+"
                     r"(?:\s*,\s*(?:[<>=!~]=?)\s*[\w.*+!-]+)*)", s)
        if m:
            c.add("pypi", m.group(1), None, p, i, "manifest", spec=m.group(2).replace(" ", ""))


def _package_lock(c: _Collector, p: Path) -> None:
    lines = _lines(p)
    try:
        data = json.loads("\n".join(lines))
    except ValueError:
        c.problems.append(f"{c.rel(p)}: not valid JSON")
        return
    pk = data.get("packages")
    if isinstance(pk, dict):  # lockfileVersion 2/3
        for path, info in pk.items():
            if not path or "node_modules/" not in path:
                continue
            name = info.get("name") or path.rsplit("node_modules/", 1)[1]
            if path.count("node_modules/") > 1:
                continue  # nested duplicates: the top-level copy is what the project imports
            line = _find_line(lines, rf'^\s*"{re.escape(path)}": \{{')
            c.add("npm", name, info.get("version"), p, line, "lock", resolved=info.get("resolved"),
                  integrity=info.get("integrity"), dev=info.get("dev"))
        return
    for name, info in (data.get("dependencies") or {}).items():  # lockfileVersion 1
        line = _find_line(lines, rf'^\s*"{re.escape(name)}": \{{')
        c.add("npm", name, (info or {}).get("version"), p, line, "lock", resolved=(info or {}).get("resolved"))


def _cargo_lock(c: _Collector, p: Path) -> None:
    data, lines = _toml(p), _lines(p)
    pos = 0
    for pkg in data.get("package") or []:
        name, ver = pkg.get("name"), pkg.get("version")
        if not name:
            continue
        line = _find_line(lines, rf'^name = "{re.escape(name)}"', pos)
        pos = line
        src = pkg.get("source") or ""
        if not src:
            continue  # workspace member
        extra = {"checksum": pkg.get("checksum")}
        if src.startswith("git+"):
            extra["vcs_url"], extra["vcs_requested"], extra["vcs_commit"] = _git_source(src)
        c.add("cargo", name, ver, p, line, "lock", **extra)


def _go_mod(c: _Collector, p: Path) -> None:
    lines = _lines(p)
    sums = _lines(p.with_name("go.sum"))
    block = False
    for i, raw in enumerate(lines, 1):
        s = raw.split("//")[0].strip()
        m = re.match(r"^go\s+(\d+\.\d+(?:\.\d+)?)$", s)
        if m:
            c.add_runtime("go", m.group(1), p, i)
            continue
        m = re.match(r"^toolchain\s+go(\S+)$", s)
        if m:
            c.add_runtime("go", m.group(1), p, i, kind="runtime", toolchain=True)
            continue
        if s.startswith("require ("):
            block = True
            continue
        if block and s == ")":
            block = False
            continue
        in_require = block or s.startswith("require ")
        m = re.match(r"^(?:require\s+)?([\w./~-]+\.[\w./~-]+)\s+(v[\w.+-]+)", s) if in_require else None
        if m:
            mod, ver = m.group(1), m.group(2)
            sum_line = next((j for j, ln in enumerate(sums, 1) if ln.startswith(f"{mod} {ver} ")), None)
            pseudo = re.match(r"^v\d+\.\d+\.\d+-(?:[\w.]+\.)?(?:0\.)?\d{14}-([0-9a-f]{12})$", ver)
            c.add("golang", mod, ver, p, i, "lock", indirect=("indirect" in raw) or None,
                  go_sum=f"go.sum:{sum_line}" if sum_line else None,
                  vcs_commit=pseudo.group(1) if pseudo else None)


# -- installed metadata of the project's own virtual environment -------------------------------------------

def project_site_packages(repo: Path) -> list[Path]:
    """site-packages directories of the project's venv (``.venv``/``venv``/``env`` with ``pyvenv.cfg``)."""
    out = []
    for d in _VENV_DIRS:
        venv = repo / d
        if not (venv / "pyvenv.cfg").is_file():
            continue
        win = venv / "Lib" / "site-packages"
        if win.is_dir():
            out.append(win)
        out += sorted(x for x in (venv / "lib").glob("python3*/site-packages") if x.is_dir())
    return out


def _installed(c: _Collector) -> None:
    from importlib import metadata

    for site in project_site_packages(c.repo):
        for dist in metadata.distributions(path=[str(site)]):
            try:
                name = dist.metadata["Name"]
                ver = dist.version
            except Exception:  # noqa: BLE001 - a broken dist-info is skipped
                continue
            if not name:
                continue
            base = Path(str(getattr(dist, "_path", ""))) if getattr(dist, "_path", None) else None
            meta_file = base / "METADATA" if base and (base / "METADATA").is_file() else \
                (base / "PKG-INFO" if base and (base / "PKG-INFO").is_file() else None)
            line = _find_line(_lines(meta_file), r"^Version:") if meta_file else 1
            extra: dict = {}
            try:
                du = dist.read_text("direct_url.json")
            except Exception:  # noqa: BLE001
                du = None
            if du:
                try:
                    d = json.loads(du)
                    vi = d.get("vcs_info") or {}
                    extra.update(direct_url=d.get("url"), vcs_commit=vi.get("commit_id"),
                                 vcs_requested=vi.get("requested_revision"),
                                 editable=(d.get("dir_info") or {}).get("editable"))
                    if vi:
                        extra["vcs_url"] = d.get("url")
                except ValueError:
                    pass
            c.add("pypi", name, ver, meta_file or site, line, "installed", site_packages=c.rel(site), **extra)
        c.sources.append(c.rel(site))


# -- runtime pins and manifests ------------------------------------------------------------------------------

def _runtime_files(c: _Collector) -> None:
    repo = c.repo
    pp = repo / "pyproject.toml"
    if pp.is_file():
        lines = _lines(pp)
        data = _toml(pp)
        rp = (data.get("project") or {}).get("requires-python")
        if rp:
            c.add_runtime("python", rp, pp, _find_line(lines, r"^requires-python"), kind="runtime_constraint")
    pv = repo / ".python-version"
    if pv.is_file():
        first = next((ln.strip() for ln in _lines(pv) if ln.strip() and not ln.startswith("#")), None)
        if first:
            c.add_runtime("python", first, pv, 1)
    for d in _VENV_DIRS:
        cfg = repo / d / "pyvenv.cfg"
        if cfg.is_file():
            lines = _lines(cfg)
            for i, ln in enumerate(lines, 1):
                m = re.match(r"^\s*(?:version|version_info)\s*=\s*([\d.]+)", ln)
                if m:
                    c.add_runtime("python", m.group(1), cfg, i, kind="installed")
                    break
    for name in (".nvmrc", ".node-version"):
        f = repo / name
        if f.is_file():
            first = next((ln.strip() for ln in _lines(f) if ln.strip()), None)
            if first:
                c.add_runtime("node", first.lstrip("v"), f, 1)
    pj = repo / "package.json"
    if pj.is_file():
        lines = _lines(pj)
        try:
            data = json.loads("\n".join(lines))
        except ValueError:
            data = {}
        eng = (data.get("engines") or {}).get("node")
        if eng:
            c.add_runtime("node", eng, pj, _find_line(lines, r'"node"\s*:'), kind="runtime_constraint")
    for name in ("rust-toolchain.toml", "rust-toolchain"):
        f = repo / name
        if f.is_file():
            lines = _lines(f)
            if name.endswith(".toml"):
                ch = ((_toml(f).get("toolchain") or {}).get("channel"))
                if ch:
                    c.add_runtime("rust", ch, f, _find_line(lines, r"^channel"))
            elif lines:
                c.add_runtime("rust", lines[0].strip(), f, 1)


def _manifests(c: _Collector) -> None:
    from repoatlas.research import dependencies

    eco = {"python": "pypi", "npm": "npm", "go": "golang", "cargo": "cargo"}
    try:
        deps = dependencies(c.repo)
    except Exception:  # noqa: BLE001
        return
    for it in deps.get("items", []):
        e = eco.get(it.get("ecosystem"), it.get("ecosystem"))
        if e == "golang":
            continue  # go.mod versions are read as the selected versions above
        c.add(e, it["name"], None, c.repo / it["path"], it["line"], "manifest", spec=it.get("spec"),
              scope=it.get("scope"))


LOCK_PARSERS = {
    "uv.lock": _uv_lock, "poetry.lock": _poetry_lock, "Pipfile.lock": _pipfile_lock,
    "package-lock.json": _package_lock, "npm-shrinkwrap.json": _package_lock, "Cargo.lock": _cargo_lock,
    "go.mod": _go_mod,
}
UNSUPPORTED_LOCKS = ("pnpm-lock.yaml", "yarn.lock", "bun.lockb", "Gemfile.lock", "composer.lock",
                     "gradle.lockfile", "packages.lock.json", "pdm.lock", "conda-lock.yml")


def local_versions(repo: Path) -> dict:
    """``{"packages": {purl_key: [item...]}, "runtime": {name: [item...]}, "sources", "unsupported", "problems"}``.

    Items within a key are ordered lock > installed > runtime > manifest.
    """
    repo = Path(repo).resolve()
    c = _Collector(repo)
    for fname, parser in LOCK_PARSERS.items():
        p = repo / fname
        if p.is_file():
            parser(c, p)
            c.sources.append(fname)
    for p in sorted(repo.glob("pylock*.toml")):
        _pylock(c, p)
        c.sources.append(p.name)
    for p in sorted(list(repo.glob("requirements*.txt")) + list(repo.glob("requirements/*.txt"))):
        _requirements(c, p)
        c.sources.append(c.rel(p))
    for fname in UNSUPPORTED_LOCKS:
        if (repo / fname).is_file():
            c.unsupported.append({"file": fname, "why": "lock format not supported yet; its versions are not read",
                                  "next_step": f"state the version explicitly, or read {fname} yourself"})
    try:
        _installed(c)
    except Exception as exc:  # noqa: BLE001 - metadata problems never break resolution
        c.problems.append(f"installed metadata: {type(exc).__name__}: {exc}")
    _runtime_files(c)
    _manifests(c)
    for items in c.packages.values():
        items.sort(key=lambda it: KIND_ORDER.get(it["kind"], 9))
    for items in c.runtime.values():
        items.sort(key=lambda it: {"runtime": 0, "installed": 1, "runtime_constraint": 2}.get(it["kind"], 9))
    return {"packages": c.packages, "runtime": c.runtime, "sources": sorted(set(c.sources)),
            "unsupported": c.unsupported, "problems": c.problems}


def best(local: dict, ecosystem: str, name: str) -> dict | None:
    """The strongest local version item for a package (lock, then installed, then manifest)."""
    items = (local.get("packages") or {}).get(key(ecosystem, name)) or []
    for it in items:
        if it.get("version") or it.get("vcs_commit"):
            return it
    return items[0] if items else None


def find_by_repo(local: dict, canonical_url: str) -> dict | None:
    """A locked/installed package whose VCS source is this repository."""
    want = _canon(canonical_url)
    for items in (local.get("packages") or {}).values():
        for it in items:
            for u in (it.get("vcs_url"), it.get("direct_url")):
                if u and _canon(u) == want:
                    return it
    return None


def find_by_name(local: dict, name: str) -> list[dict]:
    """Local items named ``name`` in any ecosystem (the package name equals the repository name)."""
    out = []
    for k, items in (local.get("packages") or {}).items():
        eco = k.split(":", 1)[1].split("/", 1)[0]
        if k == key(eco, name) and items:
            out.append(next((it for it in items if it.get("version") or it.get("vcs_commit")), items[0]))
    return out


def _canon(url: str) -> str:
    u = re.sub(r"^git\+", "", url or "").strip().lower()
    u = re.sub(r"^(?:ssh://)?git@([^:/]+)[:/]", r"https://\1/", u)
    u = re.sub(r"^http://", "https://", u)
    u = re.sub(r"[?#].*$", "", u)
    u = re.sub(r"@[^/]*$", "", u) if u.count("@") and "://" in u and "@" in u.split("/")[-1] else u
    return re.sub(r"\.git$", "", u.rstrip("/"))


def evidence_for(repo: Path, item: dict, *, commit: str | None) -> dict | None:
    """Source evidence (file:line + content hash) for a local version item."""
    from repoatlas import evidence as evmod

    return evmod.source_evidence(Path(repo), item["source_file"], int(item.get("line") or 1), commit=commit,
                                 meta={"check": "local_version", "kind": item.get("kind"),
                                       "version": item.get("version"), "package": item.get("name")
                                       or item.get("runtime")}, anchor=False)
