"""The Python environment a name check runs against (docs/DESIGN.md D31).

:func:`select_env` picks the project's own environment - ``--env PATH``, else
``<project>/.venv``, ``venv`` or ``env`` (a directory with ``pyvenv.cfg``) -
and falls back to Verinoda's own interpreter for the standard library only.
Without a project environment, third-party names are never judged: they come
back ``not_installed`` ("no project environment"), never ``absent``.

Safety: an environment found on disk is accepted only when jedi's safety check
(``create_environment(safe=True)``) passes, i.e. its interpreter is (a link to
or a copy of) a Python installation known to the system; ``--env`` is the
user's explicit choice and is trusted as given. jedi's default policy stays in
force: compiled extension modules outside the standard library are never
imported (``Project(load_unsafe_extensions=False)``).

:class:`StdlibOracle` answers "which names does this standard-library module or
class have" from the running interpreter of the chosen environment, started
with ``-I -S`` (isolated, no site-packages) so it can import the standard
library only. That is the exact Python version the project runs, which the
typeshed stubs bundled with jedi may not match.

:class:`ImportUniverse` finds modules statically (directories on the search
path, ``.pth`` path lines, setuptools editable-install mappings) to tell "not
found anywhere on the search path" from "found, but jedi cannot read it"
(a compiled module without a stub, a namespace package).
"""

from __future__ import annotations

import ast
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import sysconfig
import threading
from dataclasses import dataclass, field
from pathlib import Path

VENV_DIRS = (".venv", "venv", "env")
# .pth lines that run code but only set up tooling, not an import hook for other modules
_BENIGN_PTH = ("_virtualenv", "distutils-precedence", "pywin32_bootstrap", "coverage", "_distutils_hack",
               "__editable__", "sitecustomize")
# standard-library modules the oracle never imports (side effects on import)
_ORACLE_DENY = ("antigravity", "this", "__main__", "__hello__", "__phello__", "turtledemo", "idlelib")
ORACLE_TIMEOUT = 20.0


def _norm_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class EnvInfo:
    """The environment of one check: which interpreter, which search path, which packages."""

    kind: str                       # project | explicit | verinoda
    label: str                      # ".venv (python 3.12.0)"
    executable: str
    version: tuple[int, int, int]
    third_party: bool               # can third-party names be judged at all?
    note: str = ""                  # why there is no project environment, when there is none
    path: Path | None = None        # the venv directory
    jedi_env: object = None
    sys_path: list[str] = field(default_factory=list)   # stdlib + site-packages (no project dirs)
    site_dirs: list[Path] = field(default_factory=list)
    dists: dict[str, tuple[str, str, Path]] = field(default_factory=dict)   # norm name -> (name, version, dist-info)
    top_level: dict[str, str] = field(default_factory=dict)                 # top-level module -> norm dist name
    fingerprint: str = ""
    _oracle: object = None
    _stdlib: list[Path] | None = None   # asked from the interpreter on first use (the oracle starts early)

    @property
    def python(self) -> str:
        return ".".join(str(x) for x in self.version)

    @property
    def stdlib_dirs(self) -> list[Path]:
        """The interpreter's own standard-library directories (never a PYTHONPATH entry or the project)."""
        if self._stdlib is None:
            info = self.oracle().ask("sys")
            if info.get("ok"):
                self._stdlib = [Path(d) for d in info.get("stdlib_dirs", [])]
            else:  # the interpreter would not answer: its Lib/DLLs and zip entries as a last resort
                self._stdlib = [Path(x) for x in self.sys_path
                                if Path(x).name.lower() in ("lib", "dlls") or x.endswith(".zip")]
        return self._stdlib

    def oracle(self) -> "StdlibOracle":
        if self._oracle is None:
            self._oracle = StdlibOracle(self.executable)
        return self._oracle  # type: ignore[return-value]

    def dist_of(self, module_path: Path | str | None) -> tuple[str, str] | None:
        """(dist name, version) of an installed file, or None."""
        if not module_path:
            return None
        p = Path(module_path)
        for site in self.site_dirs:
            try:
                rel = p.relative_to(site)
            except ValueError:
                continue
            top = rel.parts[0] if rel.parts else ""
            top = top[:-3] if top.endswith((".py", ".so")) else top.split(".")[0]
            d = self.dists.get(self.top_level.get(top, _norm_dist(top)))
            return (d[0], d[1]) if d else (top, "?")
        return None

    def origin(self, module_path: Path | str | None) -> str | None:
        """stdlib | installed | None (the project, or unknown)."""
        if not module_path:
            return None
        p = Path(module_path)
        if is_jedi_stdlib_stub(p):
            return "stdlib"
        if _under(p, self.site_dirs):
            return "installed"
        if "site-packages" in p.parts or "dist-packages" in p.parts:
            return None   # another environment's packages (for example the base installation's)
        if _under(p, self.stdlib_dirs):
            return "stdlib"
        return None

    def header(self) -> dict:
        return {"python": self.label, "kind": self.kind, "executable": self.executable,
                "third_party_checked": self.third_party, **({"note": self.note} if self.note else {})}


def _under(p: Path, roots: list[Path]) -> bool:
    for r in roots:
        try:
            p.relative_to(r)
            return True
        except ValueError:
            continue
    return False


@functools.lru_cache(maxsize=1)
def jedi_typeshed_dir() -> Path | None:
    try:
        import jedi
    except ImportError:
        return None
    return Path(jedi.__file__).resolve().parent / "third_party" / "typeshed"


@functools.lru_cache(maxsize=16384)
def _typeshed_part(p: str) -> str | None:
    """"stdlib" / "stubs" for a file inside jedi's bundled typeshed, else None."""
    ts = jedi_typeshed_dir()
    if not ts:
        return None
    for cand in (Path(p), Path(p).resolve() if Path(p).is_absolute() else Path(p)):
        try:
            rel = cand.relative_to(ts)
        except ValueError:
            continue
        return "stdlib" if rel.parts and rel.parts[0] == "stdlib" else "stubs"
    return None


def is_jedi_stdlib_stub(p: Path | str) -> bool:
    return _typeshed_part(str(p)) == "stdlib"


def is_jedi_bundled_stub(p: Path | str | None) -> bool:
    """A third-party stub shipped inside jedi (it may not match the installed version)."""
    return bool(p) and _typeshed_part(str(p)) == "stubs"


def _own_stdlib_dirs() -> list[Path]:
    base = {"base": sys.base_prefix, "platbase": sys.base_exec_prefix, "installed_base": sys.base_prefix,
            "installed_platbase": sys.base_exec_prefix}
    out: list[Path] = []
    for key in ("stdlib", "platstdlib"):
        try:
            out.append(Path(sysconfig.get_path(key, vars=base)))
        except KeyError:  # pragma: no cover
            pass
    out.append(Path(sys.base_prefix) / "DLLs")
    ver = f"python{sys.version_info[0]}{sys.version_info[1]}.zip"
    out.append(Path(sys.base_prefix) / ver)
    out.append(Path(sys.base_prefix) / "lib" / ver)
    seen, res = set(), []
    for p in out:
        if p.exists() and str(p) not in seen:
            seen.add(str(p))
            res.append(p)
    return res


def _dists(site_dirs: list[Path]) -> tuple[dict, dict]:
    """Installed distributions of the site directories, read from their metadata files (nothing is imported)."""
    dists: dict[str, tuple[str, str, Path]] = {}
    top: dict[str, str] = {}
    for site in site_dirs:
        try:
            entries = sorted(os.listdir(site))
        except OSError:
            continue
        for e in entries:
            if not e.endswith((".dist-info", ".egg-info")):
                continue
            stem = e.rsplit(".", 1)[0]
            name, _, ver = stem.partition("-")
            ver = ver.split("-")[0] if ver else "?"
            d = site / e
            meta = d / ("METADATA" if e.endswith(".dist-info") else "PKG-INFO")
            if meta.is_file():
                try:
                    head = meta.read_text(encoding="utf-8", errors="replace")[:4000]
                    m = re.search(r"^Name:\s*(\S+)", head, re.M)
                    v = re.search(r"^Version:\s*(\S+)", head, re.M)
                    name, ver = (m.group(1) if m else name), (v.group(1) if v else ver)
                except OSError:
                    pass
            key = _norm_dist(name)
            dists[key] = (name, ver, d)
            tops: set[str] = set()
            tl = d / "top_level.txt"
            if tl.is_file():
                try:
                    tops |= {x.strip() for x in tl.read_text(encoding="utf-8", errors="replace").split() if x.strip()}
                except OSError:
                    pass
            rec = d / "RECORD"
            if rec.is_file():
                try:
                    for ln in rec.read_text(encoding="utf-8", errors="replace").splitlines():
                        first = ln.split(",", 1)[0].replace("\\", "/").split("/", 1)[0]
                        if first and not first.endswith((".dist-info", ".data")) and first not in ("..", "__pycache__"):
                            tops.add(first[:-3] if first.endswith(".py") else first.split(".")[0])
                except OSError:
                    pass
            for t in tops:
                top.setdefault(t, key)
    return dists, top


def _fingerprint(executable: str, version: tuple, site_dirs: list[Path], stdlib_dirs: list[Path]) -> str:
    h = hashlib.sha256()
    h.update(f"{Path(executable).resolve()}|{version}|".encode())
    for d in stdlib_dirs:
        h.update(f"std:{d}|".encode())
    for site in site_dirs:
        h.update(f"site:{site}|".encode())
        try:
            names = sorted(e for e in os.listdir(site) if e.endswith((".dist-info", ".egg-info", ".pth", ".egg-link")))
        except OSError:
            names = []
        for n in names:
            h.update(n.encode("utf-8", "replace") + b"|")
            if n.endswith(".pth"):
                try:
                    h.update(hashlib.sha256((site / n).read_bytes()).digest())
                except OSError:
                    pass
    return h.hexdigest()


def _jedi_sys_path(jenv) -> list[str]:
    try:
        import jedi.inference.compiled.subprocess as sp
        own = os.path.normcase(os.path.dirname(os.path.abspath(sp.__file__)))
    except Exception:  # noqa: BLE001
        own = ""
    return [p for p in jenv.get_sys_path() if os.path.normcase(os.path.abspath(p)) != own]


def _from_jedi_env(jenv, kind: str, venv: Path | None, repo: Path) -> EnvInfo:
    sp = _jedi_sys_path(jenv)
    vi = tuple(jenv.version_info)[:3]
    exe = str(jenv.executable)
    site_dirs = [Path(p) for p in sp if "site-packages" in Path(p).parts or "dist-packages" in Path(p).parts]
    oracle = StdlibOracle(exe)
    oracle.prefetch("sys")   # the interpreter starts while jedi works; stdlib_dirs reads the answer
    if venv is not None:
        try:
            rel = venv.resolve().relative_to(repo)
            where = rel.as_posix()
        except ValueError:
            where = str(venv)
    else:
        where = exe
    label = f"{where} (python {'.'.join(map(str, vi))})"
    dists, top = _dists(site_dirs)
    env = EnvInfo(kind=kind, label=label, executable=exe, version=vi, third_party=True, path=venv,
                  jedi_env=jenv, sys_path=sp, site_dirs=site_dirs, dists=dists, top_level=top, _oracle=oracle)
    env.fingerprint = _fingerprint(exe, vi, site_dirs, [Path(x) for x in sp])
    return env


def own_env(note: str) -> EnvInfo:
    """Verinoda's own interpreter, standard library only."""
    std = _own_stdlib_dirs()
    jenv = None
    try:
        from jedi.api.environment import InterpreterEnvironment
        jenv = InterpreterEnvironment()
    except ImportError:
        pass
    vi = tuple(sys.version_info)[:3]
    info = EnvInfo(kind="verinoda", label=f"Verinoda's interpreter, standard library only (python "
                                          f"{'.'.join(map(str, vi))})",
                   executable=sys.executable, version=vi, third_party=False, note=note, jedi_env=jenv,
                   sys_path=[str(p) for p in std], _stdlib=std)
    info.fingerprint = _fingerprint(sys.executable, vi, [], std)
    return info


def select_env(repo: Path, env: str | None = "auto") -> EnvInfo:
    """The environment to check against: ``auto`` (project venv, else stdlib only), a path, or ``none``."""
    repo = Path(repo).resolve()
    choice = (env or "auto").strip()
    try:
        import jedi
    except ImportError:
        return own_env("jedi is not installed (pip install 'verinoda[precise]')")
    if choice == "none":
        return own_env("--env none: third-party names are not checked")
    if choice != "auto":
        p = Path(choice)
        if not p.is_absolute():
            p = (repo / p) if (repo / p).exists() else (Path.cwd() / p)
        try:
            jenv = jedi.create_environment(str(p), safe=False)   # the user's explicit choice
            jenv.get_sys_path()
        except Exception as exc:  # noqa: BLE001 - InvalidPythonEnvironment and friends
            raise ValueError(f"--env {choice}: not a usable Python environment ({exc})")
        venv = p if p.is_dir() else None
        return _from_jedi_env(jenv, "explicit", venv, repo)
    notes = []
    for d in VENV_DIRS:
        venv = repo / d
        if not (venv / "pyvenv.cfg").is_file():
            continue
        try:
            jenv = jedi.create_environment(str(venv), safe=True)
            jenv.get_sys_path()
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{d}/ was found but not used ({type(exc).__name__}: {exc}); pass --env {d} to trust it")
            continue
        return _from_jedi_env(jenv, "project", venv, repo)
    why = "; ".join(notes) or ("no project environment found (.venv, venv or env with pyvenv.cfg); "
                               "pass --env PATH")
    return own_env(why + ": third-party names are not checked")


# -- the standard-library oracle -----------------------------------------------------------------

_ORACLE_SRC = r'''
import sys, json, importlib, inspect, pkgutil, types
out = sys.stdout
DYNAMIC_C = {("weakref", "ProxyType"), ("weakref", "CallableProxyType"), ("builtins", "super"),
             ("types", "SimpleNamespace"), ("_thread", "_local"), ("builtins", "module")}
sys.stdout = sys.stderr
STD = set(getattr(sys, "stdlib_module_names", ())) | set(sys.builtin_module_names) | set(json.loads(sys.argv[1]))
DENY = set(json.loads(sys.argv[2]))
STD_META = []
try:
    import abc, enum
    STD_META = [type, abc.ABCMeta, getattr(enum, "EnumType", None), getattr(enum, "EnumMeta", None)]
except Exception:
    pass

def std(name):
    top = name.split(".")[0]
    if top in DENY or top not in STD:
        raise ImportError("not a standard-library module: " + name)

def load(dotted):
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        mod = ".".join(parts[:i])
        std(mod)
        try:
            obj = importlib.import_module(mod)
        except ImportError:
            if i == 1:
                raise
            continue
        rest = parts[i:]
        parent = None
        for p in rest:
            parent = obj
            obj = getattr(obj, p)
        return obj, parent, (rest[-1] if rest else None), mod
    raise ImportError(dotted)

def src(k):
    try:
        return inspect.getsourcefile(k)
    except Exception:
        return None

def sig(o):
    try:
        s = inspect.signature(o)
    except Exception as e:
        return None, type(e).__name__ + ": " + str(e)
    return [[p.name, p.kind.name] for p in s.parameters.values()], str(s)

def kind_of(o):
    if inspect.ismodule(o):
        return "module"
    if isinstance(o, type):
        return "class"
    if inspect.isroutine(o) or callable(o):
        return "function"
    return "variable"

def handle(req):
    op = req["op"]
    if op == "module":
        std(req["name"])
        m = importlib.import_module(req["name"])
        d = vars(m)
        subs = sorted(x.name for x in pkgutil.iter_modules(m.__path__)) if hasattr(m, "__path__") else []
        return {"ok": True, "names": sorted(k for k in d if isinstance(k, str)), "getattr": "__getattr__" in d,
                "file": getattr(m, "__file__", None), "pkg": hasattr(m, "__path__"), "submodules": subs}
    if op == "object":
        o, parent, last, mod = load(req["name"])
        owner = o.__name__ if inspect.ismodule(o) else getattr(o, "__module__", mod)
        info = {"ok": True, "kind": kind_of(o), "module": owner}
        if isinstance(o, type):
            mro = o.__mro__
            names = set()
            for k in mro:
                names |= set(vars(k))
            meta = type(o)
            mnames = set()
            for k in meta.__mro__:
                mnames |= set(vars(k))
            info["names"] = sorted(n for n in names if isinstance(n, str))
            info["meta_names"] = sorted(n for n in mnames - names if isinstance(n, str))
            # a __getattribute__ written in Python is a hook; a C type's slot wrapper is the generic lookup,
            # except for the few C types whose lookup is dynamic
            info["inst_getattr"] = any(
                "__getattr__" in vars(k)
                or (k is not object and isinstance(vars(k).get("__getattribute__"), types.FunctionType))
                or (k.__module__, k.__qualname__) in DYNAMIC_C
                for k in mro)
            info["meta_getattr"] = any("__getattr__" in vars(k) for k in meta.__mro__)
            info["inst_dict"] = any("__dict__" in vars(k) for k in mro if k is not type)
            info["custom_meta"] = meta not in STD_META
            info["meta"] = meta.__module__ + "." + meta.__qualname__
            info["mro"] = [[k.__module__, k.__qualname__, src(k), "__dict__" in vars(k)] for k in mro]
        if parent is not None and inspect.isclass(parent) and last:
            st = inspect.getattr_static(parent, last, None)
            info["static"] = isinstance(st, staticmethod)
            info["classmethod"] = isinstance(st, classmethod)
        if callable(o):
            params, text = sig(o)
            if params is None:
                info["sig_error"] = text
            else:
                info["params"], info["signature"] = params, text
        return info
    if op == "members":
        o, parent, last, mod = load(req["name"])
        rows = []
        if inspect.ismodule(o):
            items = sorted(vars(o).items(), key=lambda kv: kv[0])
        else:
            items = []
            seen = set()
            for k in getattr(o, "__mro__", (o,)):
                for n, v in sorted(vars(k).items(), key=lambda kv: kv[0]):
                    if n not in seen:
                        seen.add(n)
                        items.append((n, v))
        for n, v in items:
            if not isinstance(n, str):
                continue
            row = {"name": n, "kind": kind_of(v)}
            if isinstance(v, (staticmethod, classmethod)):
                v = v.__func__
                row["kind"] = "function"
            if isinstance(v, property):
                row["kind"] = "property"
            elif callable(v) and not isinstance(v, type):
                params, text = sig(v)
                if params is not None:
                    row["signature"] = n + text
            rows.append(row)
        return {"ok": True, "kind": kind_of(o), "members": rows}
    if op == "sys":
        import os, sysconfig
        base = {"base": sys.base_prefix, "platbase": sys.base_exec_prefix, "installed_base": sys.base_prefix,
                "installed_platbase": sys.base_exec_prefix}
        dirs = []
        for key in ("stdlib", "platstdlib"):
            try:
                dirs.append(sysconfig.get_path(key, vars=base))
            except KeyError:
                pass
        dirs.append(os.path.join(sys.base_prefix, "DLLs"))
        dirs += [p for p in sys.path if p.endswith(".zip")]
        return {"ok": True, "builtin_module_names": sorted(sys.builtin_module_names),
                "stdlib_module_names": sorted(STD), "version": list(sys.version_info[:3]), "platform": sys.platform,
                "stdlib_dirs": [d for d in dict.fromkeys(dirs) if os.path.exists(d)]}
    return {"ok": False, "error": "unknown op " + op}

for line in sys.stdin:
    try:
        res = handle(json.loads(line))
    except BaseException as e:
        res = {"ok": False, "error": type(e).__name__ + ": " + str(e)[:300]}
    out.write(json.dumps(res) + "\n")
    out.flush()
'''


class StdlibOracle:
    """A long-lived ``python -I -S`` child that imports standard-library modules and lists their names."""

    def __init__(self, executable: str):
        self.executable = executable
        self._proc: subprocess.Popen | None = None
        self._memo: dict[tuple, dict] = {}
        self._pending: list[tuple] = []
        self._lock = threading.Lock()
        self.broken: str | None = None

    def _start(self) -> None:
        extra = sorted(getattr(sys, "stdlib_module_names", ()))   # for interpreters older than 3.10
        self._proc = subprocess.Popen(
            [self.executable, "-I", "-S", "-c", _ORACLE_SRC, json.dumps(extra), json.dumps(list(_ORACLE_DENY))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1)

    def prefetch(self, op: str, **kw) -> None:
        """Send a request now and read its answer on the first ask() (the child starts meanwhile)."""
        with self._lock:
            try:
                self._send(op, kw)
                self._pending.append((op, *sorted(kw.items())))
            except (OSError, ValueError, AssertionError) as exc:
                self.broken = f"standard-library oracle failed: {type(exc).__name__}: {exc}"
                self.close()

    def _send(self, op: str, kw: dict) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._pending.clear()
            self._start()
        assert self._proc is not None and self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(json.dumps({"op": op, **kw}) + "\n")
        self._proc.stdin.flush()

    def ask(self, op: str, **kw) -> dict:
        key = (op, *sorted(kw.items()))
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        with self._lock:
            if self.broken:
                return {"ok": False, "error": self.broken}
            try:
                while self._pending:   # answers come back in the order the requests were sent
                    self._memo[self._pending.pop(0)] = _readline(self._proc, ORACLE_TIMEOUT)
                hit = self._memo.get(key)
                if hit is not None:
                    return hit
                self._send(op, kw)
                res = _readline(self._proc, ORACLE_TIMEOUT)
            except (OSError, ValueError, AssertionError, TimeoutError) as exc:
                self.broken = f"standard-library oracle failed: {type(exc).__name__}: {exc}"
                self.close()
                return {"ok": False, "error": self.broken}
        self._memo[key] = res
        return res

    def close(self) -> None:
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
                p.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def __del__(self):  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


def _readline(proc: subprocess.Popen, timeout: float) -> dict:
    box: list = []

    def rd():
        try:
            box.append(proc.stdout.readline())  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            box.append(exc)

    t = threading.Thread(target=rd, daemon=True)
    t.start()
    t.join(timeout)
    if not box:
        raise TimeoutError(f"no answer within {timeout:.0f} s")
    line = box[0]
    if isinstance(line, Exception):
        raise OSError(str(line))
    if not line:
        raise OSError("the oracle process exited")
    return json.loads(line)


# -- static module finder ------------------------------------------------------------------------

@dataclass
class ModSpec:
    """Where a module is on the search path (found statically, nothing imported)."""

    name: str
    kind: str                  # source | stub | package | namespace | compiled | builtin
    file: Path | None = None   # the .py/.pyi file (package: its __init__)
    dirs: list[Path] = field(default_factory=list)   # package / namespace directories
    stub: Path | None = None   # a .pyi next to the source or compiled module


_EXT = (".pyd", ".so")


def _in_dir(d: Path, name: str) -> list[ModSpec]:
    out: list[ModSpec] = []
    pkg = d / name
    if pkg.is_dir():
        for init in ("__init__.py", "__init__.pyi"):
            if (pkg / init).is_file():
                stub = pkg / "__init__.pyi"
                out.append(ModSpec(name, "package", pkg / init, [pkg],
                                   stub if stub.is_file() and init == "__init__.py" else None))
                break
        else:
            out.append(ModSpec(name, "namespace", None, [pkg]))
    src, stub = d / f"{name}.py", d / f"{name}.pyi"
    if src.is_file():
        out.append(ModSpec(name, "source", src, [], stub if stub.is_file() else None))
    elif stub.is_file():
        out.append(ModSpec(name, "stub", stub))
    try:
        for e in os.listdir(d):
            if e.startswith(name + ".") and e.endswith(_EXT) and e.split(".")[0] == name:
                out.append(ModSpec(name, "compiled", d / e, [], stub if stub.is_file() else None))
    except OSError:
        pass
    return out


class ImportUniverse:
    """The places a module can be imported from, for one checked file."""

    def __init__(self, env: EnvInfo, project_dirs: list[Path], builtin_names: set[str] | None = None):
        self.env = env
        self.dirs: list[Path] = []
        self.hooks: list[str] = []            # executable .pth lines that are not known tooling
        self.editable: dict[str, Path] = {}   # setuptools editable installs: top-level name -> directory
        self.builtin = set(builtin_names or ())
        seen: set[str] = set()
        for d in [*project_dirs, *(Path(p) for p in env.sys_path)]:
            k = os.path.normcase(str(d))
            if k not in seen and Path(d).is_dir():
                seen.add(k)
                self.dirs.append(Path(d))
        for site in env.site_dirs:
            self._read_pth(site, seen)

    def _read_pth(self, site: Path, seen: set[str]) -> None:
        try:
            entries = sorted(e for e in os.listdir(site) if e.endswith(".pth"))
        except OSError:
            return
        for e in entries:
            try:
                lines = (site / e).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for ln in lines:
                s = ln.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith(("import ", "import\t")):
                    if not any(b in s or b in e for b in _BENIGN_PTH):
                        self.hooks.append(f"{e}: {s[:80]}")
                    continue
                p = Path(s) if Path(s).is_absolute() else site / s
                k = os.path.normcase(str(p))
                if p.is_dir() and k not in seen:
                    seen.add(k)
                    self.dirs.append(p)
            if e.startswith("__editable__"):
                self._read_editable_finder(site, e)

    def _read_editable_finder(self, site: Path, pth: str) -> None:
        for cand in site.glob("__editable___*_finder.py"):
            try:
                tree = ast.parse(cand.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError, ValueError):
                continue
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MAPPING"
                                                        for t in node.targets):
                    try:
                        mapping = ast.literal_eval(node.value)
                    except ValueError:
                        continue
                    for k, v in (mapping or {}).items():
                        self.editable[str(k)] = Path(v)

    def find(self, dotted: str) -> list[ModSpec]:
        """Every place the module ``dotted`` is found (empty: not found on the search path)."""
        parts = dotted.split(".")
        found = self.find_top(parts[0])
        for part in parts[1:]:
            nxt: list[ModSpec] = []
            for spec in found:
                for d in spec.dirs:
                    nxt += _in_dir(d, part)
            found = nxt
            if not found:
                break
        for s in found:
            s.name = dotted
        return found

    def find_top(self, name: str) -> list[ModSpec]:
        out: list[ModSpec] = []
        if name in self.builtin:
            out.append(ModSpec(name, "builtin"))
        if name in self.editable:
            p = self.editable[name]
            if p.is_dir():
                init = next((p / i for i in ("__init__.py", "__init__.pyi") if (p / i).is_file()), None)
                out.append(ModSpec(name, "package" if init else "namespace", init, [p]))
            elif p.is_file():
                out.append(ModSpec(name, "source", p))
        for d in self.dirs:
            out += _in_dir(d, name)
        # a namespace portion counts only when no regular module of that name exists
        if any(s.kind != "namespace" for s in out):
            regular = [s for s in out if s.kind != "namespace"]
            return regular[:1] + [s for s in regular[1:] if s.kind == "stub"]
        if out:
            return [ModSpec(name, "namespace", None, [d for s in out for d in s.dirs])]
        return out

    def top_level_names(self) -> set[str]:
        """Module names importable at top level (for nearest-name hints)."""
        names = set(self.builtin) | set(self.editable)
        for d in self.dirs:
            try:
                for e in os.listdir(d):
                    if e.endswith((".py", ".pyi")):
                        names.add(e.rsplit(".", 1)[0])
                    elif e.endswith(_EXT):
                        names.add(e.split(".")[0])
                    elif (d / e).is_dir() and e.isidentifier():
                        names.add(e)
            except OSError:
                continue
        return {n for n in names if n.isidentifier()}
