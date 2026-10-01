"""Several projects from one MCP server process (``verinoda mcp serve --projects A,B``).

* The registry (``verinoda projects add|list|remove``) is ``projects.json`` in the user config folder
  (:func:`verinoda.paths.user_config_dir`): names for project folders, so a server, a client config and a person
  can say ``orders`` instead of a path. No repository writes there.
* :class:`ProjectHub` holds one :class:`~verinoda.mcp.server.AtlasTools` per project. Every tool of a server built
  over a hub takes an optional ``project`` (a name, or an absolute path inside a served project); it may be left
  out when one project is served, or when an absolute path argument of the call lies inside exactly one of them.
  Anything else is ``project_required`` / ``unknown_project``: a call is never answered from a guessed project,
  and a path outside every served project never opens a new one.
* Each project keeps the profile a single-project server would give it (:func:`resolve_profile`: ``--profile``,
  else its own config only when the user trusts it, else the user config). The menu is the union; a call for a
  tool the project's own profile does not serve is refused (``not_served``), so a trusted project asking for the
  full menu does not open it for an untrusted one. Trust and config are read per project by the same code as
  ever (the tools run on that project's root).
* Memory: the projects share one lock (calls are serialised as in a single-project server), and only the
  ``max_loaded`` most recently used projects keep their graph, lexicon and kept answers; an older one drops them
  (:meth:`AtlasTools.drop_caches`) and loads them again on its next call.
"""

from __future__ import annotations

import contextvars
import inspect
import json
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable

REGISTRY_NAME = "projects.json"
DEFAULT_MAX_LOADED = 3
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HUB_TOOLS = ("list_projects", "index_status")
# the arguments the core menu's analyze and code_check take (server.build_server, ``slim``)
CORE_ARGS = {"analyze": {"question", "budget_seconds"},
             "code_check": {"paths", "diff", "snippet", "as_path", "deps"}}
# arguments that can carry a file path: an absolute one inside exactly one served project ties the call to it
PATH_ARGS = ("file_path", "path", "paths", "as_path", "targets", "target_path")


class ProjectError(ValueError):
    """A project spec or registry change that cannot be done (the message says why and what to do)."""


# -- the registry ---------------------------------------------------------------------------

def registry_path() -> Path:
    from verinoda.paths import user_config_dir

    return user_config_dir() / REGISTRY_NAME


def registered() -> list[dict]:
    """The registered projects (``name``, ``path``, ``added``); unreadable rows are left out."""
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("projects") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and isinstance(r.get("name"), str)
            and isinstance(r.get("path"), str) and r["path"]]


def _write(rows: list[dict]) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"version": 1, "projects": rows}, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ProjectError(f"project name {name!r}: use letters, digits, '.', '_' or '-' (at most 64, not "
                           "starting with '.', '_' or '-')")
    return name


def default_name(path: Path) -> str:
    """A registry name from a folder name: characters a name cannot hold become '-'."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name).strip("-._")[:64]
    return name or "project"


def add(path: Path | str, name: str | None = None) -> dict:
    """Register ``path`` under ``name`` (default: its folder name); a path already registered is renamed."""
    from verinoda.paths import _is_home_or_above

    real = Path(os.path.realpath(str(Path(path).expanduser())))
    if not real.is_dir():
        raise ProjectError(f"{real} is not a folder")
    if _is_home_or_above(real) or real.parent == real:
        raise ProjectError(f"{real} is a home folder or a drive root, not a project")
    name = _check_name(name or default_name(real))
    rows = registered()
    for r in rows:
        if r["name"] == name and Path(r["path"]) != real:
            raise ProjectError(f"the name {name!r} is taken by {r['path']}; pick another with --name")
    rows = [r for r in rows if Path(r["path"]) != real]
    row = {"name": name, "path": str(real), "added": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    _write([*rows, row])
    return {**row, "registry": str(registry_path())}


def remove(spec: str) -> dict:
    """Unregister a project by name or path (its folder and index are not touched)."""
    rows = registered()
    target = None if NAME_RE.match(spec) and any(r["name"] == spec for r in rows) else \
        Path(os.path.realpath(str(Path(spec).expanduser())))
    keep = [r for r in rows if not (r["name"] == spec if target is None else Path(r["path"]) == target)]
    if len(keep) == len(rows):
        raise ProjectError(f"no registered project is named or at {spec!r} (`verinoda projects list`)")
    _write(keep)
    return {"removed": spec, "registry": str(registry_path())}


def resolve_specs(specs: list[str], *, all_registered: bool = False) -> list[tuple[str, Path]]:
    """The (name, root) pairs a ``--projects`` list names: registry names, ``NAME=PATH`` or folder paths (a
    registered path keeps its registry name, any other is named after its folder). ``all_registered``: every
    registered project. Two projects with one name or one folder are an error, never a silent pick."""
    reg = registered()
    by_name = {r["name"]: Path(r["path"]) for r in reg}
    by_path = {Path(r["path"]): r["name"] for r in reg}
    out: list[tuple[str, Path]] = []
    items = [(r["name"], r["path"]) for r in reg] if all_registered else []
    for spec in specs:
        spec = spec.strip()
        if not spec:
            continue
        if spec in by_name:
            items.append((spec, str(by_name[spec])))
        elif "=" in spec and NAME_RE.match(spec.split("=", 1)[0]):
            n, p = spec.split("=", 1)
            items.append((n, p))
        else:
            items.append((None, spec))
    if not items:
        raise ProjectError("no project to serve: name registered projects (`verinoda projects add`), or folders, "
                           "in --projects A,B")
    for name, spec in items:
        real = Path(os.path.realpath(str(Path(spec).expanduser())))
        if not real.is_dir():
            raise ProjectError(f"project {spec!r}: {real} is not a folder (and no registered project has that "
                               "name: `verinoda projects list`)")
        name = name or by_path.get(real) or default_name(real)
        for n, p in out:
            if p == real:
                raise ProjectError(f"{real} is named twice ({n}, {name})")
            if n == name:
                raise ProjectError(f"two projects are named {name!r} ({p}, {real}): name one with NAME=PATH or "
                                   "`verinoda projects add PATH --name NAME`")
        out.append((name, real))
    return out


# -- the hub -------------------------------------------------------------------------------

class HubRefusal(Exception):
    def __init__(self, code: str, message: str, hint: str, **extra):
        super().__init__(message)
        self.code, self.message, self.hint, self.extra = code, message, hint, extra

    def as_dict(self, tool: str) -> dict:
        return {"error": self.code, "message": self.message, "hint": self.hint, "tool": tool, **self.extra}


class _Current:
    """Stands for the AtlasTools of the project the running call is for (``t`` in ``build_server``)."""

    def __init__(self, hub: "ProjectHub"):
        self._hub = hub

    def __getattr__(self, attr: str):
        return getattr(self._hub.current(), attr)


def _path_values(args: dict) -> list[str]:
    out: list[str] = []
    for k in PATH_ARGS:
        v = args.get(k)
        for x in ([v] if isinstance(v, str) else v if isinstance(v, list) else []):
            if isinstance(x, str) and x:
                out.append(x)
    return out


class ProjectHub:
    """The projects one server answers for (see the module docstring)."""

    def __init__(self, projects: list[tuple[str, Path]], *, profile: str | None = None,
                 max_loaded: int | None = None):
        from verinoda.mcp.server import resolve_profile, served_tools

        if not projects:
            raise ProjectError("no project to serve")
        self.projects: OrderedDict[str, Path] = OrderedDict((n, Path(p).resolve()) for n, p in projects)
        self.lock = threading.RLock()
        self.max_loaded = DEFAULT_MAX_LOADED if max_loaded is None else max(1, int(max_loaded))
        self._tools: dict[str, Any] = {}
        self._recent: OrderedDict[str, None] = OrderedDict()
        self._current: contextvars.ContextVar[str | None] = contextvars.ContextVar("verinoda_project", default=None)
        self.current_tools = _Current(self)
        self.evictions = 0
        # each project's profile exactly as a server over it alone would resolve it
        self.profiles = {n: resolve_profile(p, profile) for n, p in self.projects.items()}
        self.served = {n: (*served_tools(p, self.profiles[n]), *HUB_TOOLS) for n, p in self.projects.items()}
        self.menu_profile = "full" if "full" in self.profiles.values() else "core"

    # -- what the menu holds ---------------------------------------------------------------
    def menu_served(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for names in self.served.values():
            seen.update(dict.fromkeys(names))
        return tuple(seen)

    def instructions(self, template: str) -> str:
        """The server instructions with the projects in place of the one repository."""
        listing = "; ".join(f"{n} ({p})" for n, p in self.projects.items())
        via = " (through run_tool)" if self.menu_profile == "core" else ""
        rule = (f"Pass project=<name> in every tool call, run_tool included (it may be left out only when a call "
                f"names an absolute file path inside one of them); list_projects and index_status{via} show "
                "them and their indexes.")
        text = template.format(repo="{repo}")
        return text.replace("the repository {repo}.", f"{len(self.projects)} projects: {listing}. {rule}", 1)

    # -- which project a call is for ---------------------------------------------------------
    def owner(self, path: Path) -> str | None:
        """The served project whose root is ``path`` or holds it (the deepest one)."""
        try:
            p = Path(os.path.realpath(str(path)))
        except (OSError, ValueError):
            return None
        best: tuple[int, str] | None = None
        for n, root in self.projects.items():
            if p == root or root in p.parents:
                depth = len(root.parts)
                if best is None or depth > best[0]:
                    best = (depth, n)
        return best[1] if best else None

    def resolve(self, project: Any, args: dict) -> str:
        names = list(self.projects)
        hint = f"pass project: one of {', '.join(names)} (list_projects)"
        if project not in (None, ""):
            if not isinstance(project, str):
                raise HubRefusal("invalid_argument", "project must be a name or a path", hint)
            if project in self.projects:
                return project
            p = Path(project).expanduser()
            found = self.owner(p) if p.is_absolute() else None
            if found:
                return found
            raise HubRefusal("unknown_project", f"no served project is named {project!r} or holds that path", hint,
                             projects=names)
        if len(names) == 1:
            return names[0]
        owners = {self.owner(Path(v)) for v in _path_values(args) if Path(v).is_absolute()}
        owners.discard(None)
        if len(owners) == 1:
            return owners.pop()
        raise HubRefusal("project_required", f"this server answers for {len(names)} projects and the call does not "
                         "say which one", hint, projects=names)

    def refusal(self, name: str, tool: str, given: dict[str, Any], defaults: dict[str, Any]) -> dict | None:
        """A call the project's own profile would not take (a tool it does not serve, or a full-menu argument of
        the core analyze / code_check)."""
        prof = self.profiles[name]
        hint = ("a project's own profile decides: `verinoda mcp serve --profile full`, or trust the project "
                "(`verinoda trust`) and set mcp.profile in its config")
        if tool not in self.served[name]:
            return {"error": "not_served", "tool": tool, "project": name, "hint": hint,
                    "message": f"{tool} is not served for project {name} (profile {prof})"}
        if prof == "core" and tool in CORE_ARGS:
            extra = sorted(k for k, v in given.items() if k not in CORE_ARGS[tool] and k in defaults
                           and v != defaults[k])
            if extra:
                return {"error": "not_served", "tool": tool, "project": name, "hint": hint,
                        "message": f"{tool} takes {', '.join(sorted(CORE_ARGS[tool]))} for project {name} "
                                   f"(profile core), not {', '.join(extra)}"}
        return None

    # -- per-project tools and the memory bound ------------------------------------------------
    def _get(self, name: str):
        from verinoda.mcp.server import AtlasTools

        with self.lock:
            t = self._tools.get(name)
            if t is None:
                t = self._tools[name] = AtlasTools(self.projects[name], lock=self.lock)
            return t

    def touch(self, name: str):
        """The project's tools, marked most recently used; projects beyond ``max_loaded`` drop their caches."""
        with self.lock:
            t = self._get(name)
            self._recent.pop(name, None)
            self._recent[name] = None
            while len(self._recent) > self.max_loaded:
                old, _ = self._recent.popitem(last=False)
                if self._tools[old].graph_loaded:
                    self.evictions += 1
                self._tools[old].drop_caches()
            return t

    def current(self):
        name = self._current.get()
        if name is None:  # a tool called outside a hub call: never answered from a guessed project
            raise HubRefusal("project_required", "no project is selected for this call",
                             f"pass project: one of {', '.join(self.projects)}")
        return self._get(name)

    def tools_for(self, name: str):
        return self._get(name)

    # -- the hub's own answers ---------------------------------------------------------------
    def list_projects(self) -> dict:
        rows = [{**self._get(n).project_entry(n), "profile": self.profiles[n]} for n in self.projects]
        return {"projects": rows, "count": len(rows), "max_loaded": self.max_loaded,
                "graphs_in_memory": sum(1 for r in rows if r["graph_loaded"])}

    def index_status_all(self) -> dict:
        return {"projects": [{"name": n, **self._get(n).index_status()} for n in self.projects],
                "count": len(self.projects)}

    def status(self) -> dict:
        """What the HTTP status route and ``verinoda mcp daemon status`` report."""
        with self.lock:
            loaded = [n for n, t in self._tools.items() if t.graph_loaded]
        return {"projects": {n: str(p) for n, p in self.projects.items()}, "graphs_in_memory": loaded,
                "max_loaded": self.max_loaded, "evictions": self.evictions, "menu_profile": self.menu_profile}

    # -- the tool wrapper ----------------------------------------------------------------------
    def wrap(self, name: str, fn: Callable, emit: Callable[[dict], Any]) -> Callable:
        """``fn`` with an optional ``project`` argument: the call is tied to a project (or refused) and runs
        with that project's tools as ``t``."""
        if name == "list_projects":
            return fn
        from pydantic import Field

        from verinoda.mcp.server import GATEWAY

        sig = inspect.signature(fn)
        defaults = {k: p.default for k, p in sig.parameters.items() if p.default is not inspect.Parameter.empty}
        param = inspect.Parameter(
            "project", inspect.Parameter.KEYWORD_ONLY, default=None,
            annotation=Annotated[str | None, Field(description="The project's name (list_projects).")])
        hub = self

        def call(**kwargs):
            project = kwargs.pop("project", None)
            tool, args = name, kwargs
            if name == GATEWAY:
                tool = kwargs.get("name")
                args = dict(kwargs.get("arguments") or {})
                inner = args.pop("project", None)
                project = project if project not in (None, "") else inner
                kwargs["arguments"] = args
                if tool == "list_projects":
                    return emit(hub.list_projects())
            try:
                if tool == "index_status" and project in (None, "") and len(hub.projects) > 1:
                    return emit(hub.index_status_all())
                pname = hub.resolve(project, args)
            except HubRefusal as r:
                return emit(r.as_dict(str(tool)))
            refused = hub.refusal(pname, str(tool), args, defaults if name != GATEWAY else {})
            if refused:
                return emit(refused)
            if tool != "index_status":  # it never loads the graph: no reason to push another project's out
                hub.touch(pname)
            token = hub._current.set(pname)
            try:
                return fn(**kwargs)
            finally:
                hub._current.reset(token)

        call.__name__ = fn.__name__
        call.__doc__ = fn.__doc__
        call.__signature__ = sig.replace(parameters=[*sig.parameters.values(), param])  # type: ignore[attr-defined]
        return call
