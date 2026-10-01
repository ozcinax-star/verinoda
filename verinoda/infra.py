"""Infrastructure-as-code nodes: Dockerfiles, Compose services, Kubernetes containers and Terraform resources,
each linked to the project file its command runs.

``verinoda infra`` reads the manifests as text (no docker, kubectl or terraform is run, nothing is built):

- **Dockerfile** (``Dockerfile``, ``Dockerfile.*``, ``*.Dockerfile``, ``Containerfile``): the stages (``FROM ...
  AS name``), ``WORKDIR``, ``COPY``/``ADD`` (``--from`` a stage too), ``ENV PYTHONPATH``, ``ENTRYPOINT`` and
  ``CMD`` (exec or shell form). The image's command is the last stage's ``ENTRYPOINT`` + ``CMD``.
- **Compose** (``docker-compose*.yml``, ``compose*.yml``): each service's ``build`` (context, dockerfile),
  ``image``, ``command``, ``entrypoint`` and ``working_dir``, over the Dockerfile it builds.
- **Kubernetes** (YAML documents with ``apiVersion`` and ``kind``): each container's ``image``, ``command`` and
  ``args``, over the Dockerfile whose image it names when one is found.
- **Terraform** (``*.tf``): each ``resource`` and ``data`` block, the project paths its string attributes name
  and a ``command`` list in it.

A command is read for the program it runs: ``python -m pkg.mod`` and ``python file.py``, ``uvicorn``/
``gunicorn`` ``pkg.mod:app``, ``celery -A pkg``, ``node``/``deno``/``bun``/``ts-node`` ``file.js``, ``java -jar
x.jar`` and ``java pkg.Main``, ``go run``, ``sh -c "..."`` (each part of a ``&&`` chain) and scripts named by path.
The container path is mapped back to the project through the ``COPY`` lines (and the build context), so a link
cites the manifest line that names the command and the ``COPY`` line that put the file there.

Statuses: a node's command is ``statically_verified`` (the manifest line says it). A link to a file reached
through ``COPY`` lines, or named by path in a Terraform attribute, is ``strong_inference``: the image could
still run something else (a package installed under the same name, a volume over the folder). A file found only
by its path's ending, an image matched to a Dockerfile by its name, or a jar linked to the build file that makes it
is ``weak_inference``. A command whose program is not a project file (``nginx``, a registry image) is listed
without a link, with the reason. Read-only: no index, claim or file is written.
"""
from __future__ import annotations

import fnmatch
import json
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_BYTES = 1_000_000

# --------------------------------------------------------------------------- YAML (the subset manifests use)


class Y:
    """A YAML value (str, list of Y, dict of str -> Y, or None) with the line it starts on."""

    __slots__ = ("v", "line")

    def __init__(self, v, line: int):
        self.v, self.line = v, line

    def get(self, key: str):
        return self.v.get(key) if isinstance(self.v, dict) else None


_KEY = re.compile(r"""^("(?:[^"\\]|\\.)*"|'[^']*'|[^\s"'#\-\[{][^:#]*?|-[^\s][^:#]*?)\s*:(?:\s+(.*)|$)""")


def _strip_comment(s: str) -> str:
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
        elif ch in "\"'" and (i == 0 or s[i - 1] in " \t[{,:-"):
            q = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s.rstrip()


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    return s


def _split_flow(s: str) -> list[str]:
    """The items of a flow sequence's inside, split on commas outside quotes and brackets."""
    out, cur, q, depth = [], [], None, 0
    for ch in s:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


class _YamlReader:
    def __init__(self, lines: list[tuple[int, int, str, str]]):
        self.lines = lines   # (line number, indent, text without comment, raw text)
        self.i = 0

    def block(self, indent: int) -> Y | None:
        if self.i >= len(self.lines):
            return None
        no, ind, text, _ = self.lines[self.i]
        if ind < indent:
            return None
        if text == "-" or text.startswith("- "):
            return self.seq(ind)
        if _KEY.match(text):
            return self.mapping(ind)
        self.i += 1
        return Y(_unquote(text), no)

    def seq(self, indent: int) -> Y:
        items, first = [], self.lines[self.i][0]
        while self.i < len(self.lines):
            no, ind, text, raw = self.lines[self.i]
            if ind != indent or not (text == "-" or text.startswith("- ")):
                break
            rest = text[1:].lstrip()
            if not rest:
                self.i += 1
                items.append(self.block(indent + 1) or Y(None, no))
                continue
            off = indent + len(text) - len(rest)
            self.lines[self.i] = (no, off, rest, raw)     # the item's content as a line of its own
            items.append(self.block(off) or Y(None, no))
        return Y(items, first)

    def mapping(self, indent: int) -> Y:
        out, first = {}, self.lines[self.i][0]
        while self.i < len(self.lines):
            no, ind, text, raw = self.lines[self.i]
            if ind != indent:
                break
            m = _KEY.match(text)
            if not m:
                break
            key, rest = _unquote(m.group(1)), (m.group(2) or "").strip()
            self.i += 1
            out[key] = self.value(rest, no, indent)
        return Y(out, first)

    def value(self, rest: str, no: int, indent: int) -> Y:
        if not rest or rest.startswith(("&", "!")) and " " not in rest:
            nxt = self.lines[self.i] if self.i < len(self.lines) else None
            if nxt and (nxt[1] > indent or nxt[1] == indent and (nxt[2] == "-" or nxt[2].startswith("- "))):
                n = self.block(nxt[1]) or Y(None, no)
                n.line = no         # a nested value is cited at its key's line
                return n
            return Y(None, no)
        if rest[0] in "|>":
            parts = []
            while self.i < len(self.lines) and self.lines[self.i][1] > indent:
                parts.append(self.lines[self.i][3].strip())
                self.i += 1
            return Y(("\n" if rest[0] == "|" else " ").join(parts), no)
        if rest[0] in "[{":
            text = rest
            while text.count(rest[0]) > text.count("]" if rest[0] == "[" else "}") and self.i < len(self.lines):
                text += " " + self.lines[self.i][2].strip()
                self.i += 1
            if rest[0] == "[":
                inner = text.strip()[1:].rstrip()
                inner = inner[:-1] if inner.endswith("]") else inner
                return Y([Y(_unquote(x), no) for x in _split_flow(inner)], no)
            return Y(text, no)
        return Y(_unquote(rest), no)


def yaml_docs(text: str) -> list[Y]:
    """Each YAML document of ``text`` as :class:`Y` values with their line numbers (block and flow sequences,
    mappings, quoted and block scalars; anchors, tags and multi-line plain scalars are read as text). Helm
    template lines (``{{ ... }}`` alone on a line) are skipped."""
    docs, cur = [], []
    for no, raw in enumerate(text.splitlines(), 1):
        s = raw.rstrip()
        if s in ("---", "...") or s.startswith("--- "):
            docs.append(cur)
            cur = []
            continue
        body = _strip_comment(s)
        if not body.strip() or body.strip().startswith(("{{", "%")):
            continue
        ind = len(body) - len(body.lstrip(" "))
        cur.append((no, ind, body.strip(), s))
    docs.append(cur)
    out = []
    for lines in docs:
        if lines:
            r = _YamlReader(lines)
            node = r.block(lines[0][1])
            if node is not None:
                out.append(node)
    return out


# --------------------------------------------------------------------------- Dockerfile


@dataclass
class Copy:
    srcs: list[str]
    dest: str          # absolute in the image
    from_stage: str | None
    at: str
    text: str


@dataclass
class Stage:
    name: str | None
    base: str
    line: int
    parent: int | None = None
    workdir: str = "/"
    entrypoint: tuple[list[str], str] | None = None    # (argv, at)
    cmd: tuple[list[str], str] | None = None
    copies: list[Copy] = field(default_factory=list)
    pythonpath: list[str] = field(default_factory=list)
    cmd_inherited: bool = False


@dataclass
class Dockerfile:
    rel: str
    stages: list[Stage]

    def stage_index(self, ref: str) -> int | None:
        for i, st in enumerate(self.stages):
            if st.name and st.name.lower() == ref.lower():
                return i
        return int(ref) if ref.isdigit() and int(ref) < len(self.stages) else None


def _instructions(text: str):
    """(line, keyword, argument) per instruction, continuation lines joined."""
    buf, start = [], 0
    for no, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not buf and (not s or s.startswith("#")):
            continue
        if buf and s.startswith("#"):
            continue
        if not buf:
            start = no
        if s.endswith("\\"):
            buf.append(s[:-1])
            continue
        buf.append(s)
        line = " ".join(x.strip() for x in buf).strip()
        buf = []
        kw, _, arg = line.partition(" ")
        yield start, kw.upper(), arg.strip()
    if buf:
        line = " ".join(buf).strip()
        kw, _, arg = line.partition(" ")
        yield start, kw.upper(), arg.strip()


def _exec_or_shell(arg: str) -> list[str]:
    if arg.startswith("["):
        try:
            v = json.loads(arg)
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                return v
        except ValueError:
            pass
    return ["/bin/sh", "-c", arg]


def _abs(path: str, workdir: str) -> str:
    path = path.strip()
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(workdir or "/", path))


def parse_dockerfile(rel: str, text: str) -> Dockerfile:
    stages: list[Stage] = []
    for no, kw, arg in _instructions(text):
        at = f"{rel}:{no}"
        if kw == "FROM":
            toks = [t for t in arg.split() if not t.startswith("--")]
            base = toks[0] if toks else ""
            name = toks[2] if len(toks) >= 3 and toks[1].lower() == "as" else None
            st = Stage(name, base, no)
            for j, prev in enumerate(stages):
                if prev.name and prev.name.lower() == base.lower():
                    st.parent = j
                    st.workdir, st.entrypoint, st.cmd = prev.workdir, prev.entrypoint, prev.cmd
                    st.pythonpath = list(prev.pythonpath)
                    st.cmd_inherited = prev.cmd is not None
            stages.append(st)
            continue
        if not stages:
            continue
        st = stages[-1]
        if kw == "WORKDIR":
            st.workdir = _abs(_unquote(arg), st.workdir)
        elif kw in ("COPY", "ADD"):
            frm, toks = None, []
            body = arg
            while body.startswith("--"):
                flag, _, body = body.partition(" ")
                if flag.startswith("--from="):
                    frm = flag.split("=", 1)[1]
                body = body.strip()
            if body.startswith("["):
                try:
                    toks = [str(x) for x in json.loads(body)]
                except ValueError:
                    toks = body.split()
            else:
                try:
                    toks = shlex.split(body)
                except ValueError:
                    toks = body.split()
            if len(toks) >= 2 and not any(t.startswith(("http://", "https://", "git@")) for t in toks[:-1]):
                dest = toks[-1]
                if len(toks) > 2 and not dest.endswith("/"):
                    dest += "/"
                d = _abs(dest, st.workdir) + ("/" if dest.endswith("/") or dest in (".", "./") else "")
                st.copies.append(Copy(toks[:-1], d, frm, at, f"{kw} {arg}"))
        elif kw == "ENTRYPOINT":
            st.entrypoint = (_exec_or_shell(arg), at)
            if st.cmd_inherited:     # an ENTRYPOINT clears the CMD the base image set
                st.cmd, st.cmd_inherited = None, False
        elif kw == "CMD":
            st.cmd, st.cmd_inherited = (_exec_or_shell(arg), at), False
        elif kw == "ENV":
            m = re.search(r"\bPYTHONPATH[= ]\s*\"?([^\"\s]+)", arg)
            if m:
                st.pythonpath = [p for p in m.group(1).split(":") if p and "$" not in p]
    return Dockerfile(rel, stages)


def _copy_source(cp: Copy, cpath: str) -> str | None:
    """The source path (as ``COPY`` names it) that put ``cpath`` into the image through ``cp``, if it did."""
    dest = cp.dest
    is_dir = dest.endswith("/") or len(cp.srcs) > 1
    d = dest.rstrip("/") or "/"
    for src in cp.srcs:
        s = src.rstrip("/")
        base = posixpath.basename(s)
        src_is_file = "." in base.strip(".")
        if any(ch in s for ch in "*?["):
            if is_dir and posixpath.dirname(cpath) == d and fnmatch.fnmatch(posixpath.basename(cpath), base):
                return posixpath.join(posixpath.dirname(s), posixpath.basename(cpath))
            continue
        if cpath == d and not is_dir:
            return s or "."
        if cpath == d or cpath.startswith(d.rstrip("/") + "/"):
            rest = cpath[len(d):].lstrip("/")
            if not rest:
                continue
            # a folder's contents go under dest; a file copied into a folder keeps its name
            if src_is_file:
                if is_dir and rest == base:
                    return s
                continue
            return posixpath.join(s, rest) if s not in ("", ".") else rest
    return None


def to_project(df: Dockerfile, stage: int, cpath: str, context: str, depth: int = 0):
    """``(project path or None, evidence [at, text], note)`` for an image path, through the ``COPY`` lines."""
    if depth > 8 or stage is None or stage >= len(df.stages):
        return None, [], ""
    st = df.stages[stage]
    for cp in reversed(st.copies):
        src = _copy_source(cp, cpath)
        if src is None:
            continue
        if cp.from_stage is not None:
            j = df.stage_index(cp.from_stage)
            if j is None:
                return None, [(cp.at, cp.text)], f"copied from the image {cp.from_stage}"
            rel, ev, note = to_project(df, j, _abs(src, "/"), context, depth + 1)
            return rel, [(cp.at, cp.text), *ev], note
        rel = posixpath.normpath(posixpath.join(context, src)) if context not in ("", ".") else \
            posixpath.normpath(src)
        if rel.startswith("../") or rel == "..":
            return None, [(cp.at, cp.text)], "copied from outside the project"
        return rel, [(cp.at, cp.text)], ""
    if st.parent is not None:
        return to_project(df, st.parent, cpath, context, depth + 1)
    return None, [], ""


# --------------------------------------------------------------------------- what a command runs

_PY = re.compile(r"^(python[\d.]*|pypy[\d.]*)$")
_NODE = {"node", "nodejs", "bun", "ts-node", "tsx", "nodemon", "deno"}
_SERVERS = {"uvicorn", "gunicorn", "hypercorn", "daphne", "granian"}
_WRAPPERS = {"exec", "env", "tini", "dumb-init", "--", "gosu", "su-exec", "nohup"}
_SHELLS = {"sh", "bash", "ash", "dash", "zsh"}
_APP = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*(\(.*\))?$")


def _segments(argv: list[str]) -> list[list[str]]:
    """The commands an argv runs: ``sh -c "a && b"`` is two; wrappers (``exec``, ``tini --``) are dropped."""
    argv = list(argv)
    if len(argv) >= 3 and posixpath.basename(argv[0]) in _SHELLS and argv[1] in ("-c", "-ec", "-ce", "-exc"):
        out = []
        for part in re.split(r"&&|\|\||;|\n", argv[2]):
            try:
                toks = shlex.split(part)
            except ValueError:
                toks = part.split()
            out.extend(_segments(toks) if toks else [])
        return out
    while argv and (argv[0] in _WRAPPERS or re.match(r"^[A-Z_][A-Z0-9_]*=", argv[0])):
        argv = argv[1:]
    return [argv] if argv else []


def runs(argv: list[str]) -> list[tuple[str, str]]:
    """``(kind, ref)`` for each program an argv runs: kind ``path`` (a file or folder in the image, absolute
    or relative to the working directory), ``module`` (a Python module), ``class`` (a JVM class) or ``program``
    (an installed program, no project file)."""
    out: list[tuple[str, str]] = []
    for seg in _segments(argv):
        prog = posixpath.basename(seg[0])
        args = seg[1:]
        if _PY.match(prog):
            k = 0
            while k < len(args):
                a = args[k]
                if a == "-m" and k + 1 < len(args):
                    out.append(("module", args[k + 1]))
                    break
                if a == "-c":
                    break
                if a in ("-X", "-W", "-Q"):
                    k += 2
                    continue
                if not a.startswith("-"):
                    out.append(("path", a))
                    break
                k += 1
        elif prog in _SERVERS or prog in ("celery", "flask", "streamlit", "fastapi", "rq", "dramatiq"):
            hit = next((a for a in args if _APP.match(a)), None)
            if prog == "celery":
                k = next((i for i, a in enumerate(args) if a in ("-A", "--app")), None)
                hit = args[k + 1] if k is not None and k + 1 < len(args) else next(
                    (a.split("=", 1)[1] for a in args if a.startswith("--app=")), hit)
            elif prog == "flask":
                hit = next((a.split("=", 1)[1] for a in args if a.startswith("--app=")), None) or next(
                    (args[i + 1] for i, a in enumerate(args[:-1]) if a == "--app"), None)
            elif prog in ("streamlit", "fastapi") and "run" in args:
                rest = [a for a in args[args.index("run") + 1:] if not a.startswith("-")]
                if rest:
                    out.append(("path", rest[0]))
                    continue
            elif hit is None and prog in ("uvicorn", "daphne", "hypercorn"):
                hit = next((a for a in args if not a.startswith("-") and re.match(r"^[\w.]+$", a)), None)
            if hit:
                out.append(("module", hit.split(":")[0]))
            else:
                out.append(("program", prog))
        elif prog in _NODE:
            rest = [a for a in args if not a.startswith("-")]
            if prog == "deno" and rest and rest[0] in ("run", "serve"):
                rest = rest[1:]
            if rest:
                out.append(("path", rest[0]))
            else:
                out.append(("program", prog))
        elif prog == "java":
            k = 0
            while k < len(args):
                a = args[k]
                if a == "-jar" and k + 1 < len(args):
                    out.append(("path", args[k + 1]))
                    break
                if a in ("-cp", "-classpath", "--class-path", "-p", "--module-path"):
                    k += 2
                    continue
                if not a.startswith("-"):
                    out.append(("class", a))
                    break
                k += 1
        elif prog == "go" and len(args) >= 2 and args[0] == "run":
            out.append(("path", next((a for a in args[1:] if not a.startswith("-")), ".")))
        elif prog in _SHELLS and args and not args[0].startswith("-"):
            out.append(("path", args[0]))
        elif prog in ("ruby", "php", "perl", "Rscript", "dotnet") and args and not args[0].startswith("-"):
            out.append(("path", args[0]))
        elif "/" in seg[0] or seg[0].endswith((".sh", ".py", ".js")):
            out.append(("path", seg[0]))
            if len(seg) > 1:      # an entrypoint script that execs its arguments ("$@")
                out.extend(r for r in runs(seg[1:]) if r[0] != "program")
        else:
            out.append(("program", prog))
    return out


def _candidates(kind: str, ref: str, workdir: str, pythonpath: list[str]) -> list[str]:
    """Image paths the reference may be (most likely first)."""
    if kind == "path":
        p = _abs(ref, workdir)
        if "." not in posixpath.basename(p) and not ref.endswith("/"):
            return [p, p + ".js", p + ".ts", p + "/index.js", p + "/main.py"]
        return [p]
    if kind == "module":
        mod = ref.replace(".", "/")
        out = []
        for root in [workdir, *pythonpath]:
            out += [_abs(mod + ".py", root), _abs(mod + "/__main__.py", root), _abs(mod + "/__init__.py", root)]
        return out
    return []


def _suffixes(kind: str, ref: str) -> list[str]:
    """Project path endings to search for when the image path cannot be mapped through ``COPY``."""
    if kind == "module":
        mod = ref.replace(".", "/")
        return [mod + ".py", mod + "/__main__.py", mod + "/__init__.py"]
    if kind == "class":
        cls = ref.replace(".", "/")
        return [cls + ".java", cls + ".kt", cls + ".scala"]
    if kind == "path":
        parts = PurePosixPath(ref.lstrip("./")).parts
        return ["/".join(parts[i:]) for i in range(len(parts)) if "/".join(parts[i:])][:3]
    return []


# --------------------------------------------------------------------------- the reader


_DOCKERFILE = re.compile(r"(^|/)(Dockerfile|Containerfile)(\.[^/]+)?$|\.(Dockerfile|dockerfile)$")
_COMPOSE = re.compile(r"(^|/)(docker-)?compose[^/]*\.ya?ml$")
_ENTRY_DEF = r"^(?:async\s+def|def|class)\s+{0}\b|^{0}\s*(?::[^=]*)?="


class _Reader:
    def __init__(self, repo: Path, files: list[str]):
        self.repo, self.files = repo, files
        self.fileset = set(files)
        self.dockerfiles: dict[str, Dockerfile] = {}
        self.not_read: list[str] = []

    def text(self, rel: str) -> str | None:
        try:
            p = self.repo / rel
            if p.stat().st_size > MAX_BYTES:
                self.not_read.append(rel)
                return None
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            self.not_read.append(rel)
            return None

    def dockerfile(self, rel: str) -> Dockerfile | None:
        if rel not in self.dockerfiles:
            t = self.text(rel) if rel in self.fileset else None
            if t is None:
                return None
            self.dockerfiles[rel] = parse_dockerfile(rel, t)
        return self.dockerfiles[rel]

    def entry_line(self, rel: str, attr: str | None) -> int:
        """The line a run starts at: the app object's or function's definition, else the ``__main__`` guard."""
        if not rel.endswith(".py"):
            return 1
        t = self.text(rel) or ""
        pats = ([_ENTRY_DEF.format(re.escape(attr))] if attr else []) + [r"^if\s+__name__\s*==\s*['\"]__main__"]
        for pat in pats:
            for no, ln in enumerate(t.splitlines(), 1):
                if re.match(pat, ln):
                    return no
        return 1

    def by_suffix(self, ends: list[str], near: str) -> list[str]:
        hits: list[str] = []
        for end in ends:
            hits = [f for f in self.files if f == end or f.endswith("/" + end)]
            if hits:
                break
        near = near.strip("/")
        hits.sort(key=lambda f: (not (not near or near == "." or f.startswith(near + "/")), f.count("/"), f))
        return hits

    def resolve(self, argv: list[str], cmd_at: list[tuple[str, str]], *, df: Dockerfile | None, context: str,
                workdir: str | None, near: str) -> dict:
        """The node's ``runs`` entries: one per program its command runs, each with its link or why none."""
        stage = len(df.stages) - 1 if df and df.stages else None
        st = df.stages[stage] if stage is not None else None
        wd = workdir or (st.workdir if st else "/")
        pyp = st.pythonpath if st else []
        out = []
        for kind, ref in runs(argv):
            entry: dict = {"kind": kind, "ref": ref}
            out.append(entry)
            if kind == "program":
                entry["link"] = None
                entry["why"] = "an installed program, not a project file"
                continue
            attr = None
            if kind == "module":
                orig = next((a for a in argv if a.startswith(ref + ":")), "")
                attr = orig.split(":", 1)[1].split("(")[0].split(".")[-1] if orig else None
            mapped_missing = None
            if df is not None and kind in ("path", "module"):
                for cpath in _candidates(kind, ref, wd, pyp):
                    rel, ev, note = to_project(df, stage, cpath, context)
                    if rel and (rel in self.fileset or kind == "path" and self.is_dir(rel)):
                        isdir = rel not in self.fileset
                        entry["link"] = {"file": rel + ("/" if isdir else ""),
                                         "line": None if isdir else self.entry_line(rel, attr),
                                         "status": "strong_inference",
                                         "evidence": [*cmd_at, *[{"at": a, "text": t} for a, t in ev]]}
                        break
                    if (rel or note) and mapped_missing is None:
                        mapped_missing = (cpath, rel, ev, note)
            if "link" in entry:
                continue
            if mapped_missing and mapped_missing[1] and mapped_missing[1].endswith((".jar", ".war", ".dll")):
                rel = mapped_missing[1]
                build = self.build_file(rel)
                entry["why"] = f"{rel} is a build output, not in the project"
                if build:
                    entry["link"] = {"file": build, "line": 1, "status": "weak_inference",
                                     "evidence": [*cmd_at, *[{"at": a, "text": t} for a, t in mapped_missing[2]]],
                                     "note": "the build file nearest the artefact's folder (it may not make it)"}
                continue
            hits = self.by_suffix(_suffixes(kind, ref), near)
            if hits:
                entry["link"] = {"file": hits[0], "line": self.entry_line(hits[0], attr), "status": "weak_inference",
                                 "evidence": list(cmd_at),
                                 "note": "found by its path's ending, not through COPY"
                                         + (f" ({len(hits)} files match; the nearest is shown)"
                                            if len(hits) > 1 else "")}
                continue
            entry["link"] = None
            if mapped_missing:
                cpath, rel, _ev, note = mapped_missing
                entry["why"] = note or f"{cpath} maps to {rel}, which is not in the project"
            else:
                entry["why"] = "no project file found for it"
        return out

    def is_dir(self, rel: str) -> bool:
        return any(f.startswith(rel.rstrip("/") + "/") for f in self.files)

    def build_file(self, rel: str) -> str | None:
        d = posixpath.dirname(rel)
        while True:
            for name in ("pom.xml", "build.gradle.kts", "build.gradle", "build.sbt", "package.json"):
                c = posixpath.join(d, name) if d else name
                if c in self.fileset:
                    return c
            if not d:
                return None
            d = posixpath.dirname(d)


def _argv(y: Y | None) -> list[str] | None:
    if y is None or y.v is None:
        return None
    if isinstance(y.v, list):
        return [str(x.v) for x in y.v if x is not None and x.v is not None]
    try:
        return shlex.split(str(y.v))
    except ValueError:
        return str(y.v).split()


def _image_key(image: str) -> str:
    """``registry:5000/team/api:1.2@sha256:...`` -> ``api``."""
    image = image.split("@")[0]
    last = image.rsplit("/", 1)[-1]
    return last.split(":")[0].lower()


def _cmd_text(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) if (" " in a or not a) else a for a in argv)


def _docker_node(rd: _Reader, df: Dockerfile) -> dict:
    node = {"kind": "dockerfile", "name": df.rel, "at": f"{df.rel}:{df.stages[-1].line if df.stages else 1}",
            "status": "statically_verified"}
    st = df.stages[-1] if df.stages else None
    argv, cmd_at = [], []
    if st and st.entrypoint:
        argv += st.entrypoint[0]
        cmd_at.append({"at": st.entrypoint[1], "text": "ENTRYPOINT " + _cmd_text(st.entrypoint[0])})
    if st and st.cmd:
        argv += st.cmd[0]
        cmd_at.append({"at": st.cmd[1], "text": "CMD " + _cmd_text(st.cmd[0])})
    if st:
        node["workdir"] = st.workdir
    if argv:
        node["command"] = argv
        node["command_at"] = [c["at"] for c in cmd_at]
        ctx = posixpath.dirname(df.rel)
        node["runs"] = rd.resolve(argv, cmd_at, df=df, context=ctx, workdir=None, near=ctx)
    else:
        node["runs"] = []
        node["why"] = "no ENTRYPOINT or CMD in the Dockerfile (the base image's runs)"
    return node


def _compose_nodes(rd: _Reader, rel: str, doc: Y, images: dict) -> list[dict]:
    services = doc.get("services")
    if services is None or not isinstance(services.v, dict):
        return []
    base = posixpath.dirname(rel)
    out = []
    for name, svc in services.v.items():
        if not isinstance(svc.v, dict):
            continue
        node: dict = {"kind": "compose_service", "name": name, "at": f"{rel}:{svc.line}",
                      "status": "statically_verified"}
        df, ctx, df_ev = None, base, []
        build = svc.get("build")
        if build is not None:
            if isinstance(build.v, str):
                ctx_raw, dfile = build.v, "Dockerfile"
            else:
                c, d = build.get("context"), build.get("dockerfile")
                ctx_raw = str(c.v) if c is not None and c.v is not None else "."
                dfile = str(d.v) if d is not None and d.v is not None else "Dockerfile"
            ctx = posixpath.normpath(posixpath.join(base, ctx_raw)) if base else posixpath.normpath(ctx_raw)
            ctx = "" if ctx == "." else ctx
            df_rel = posixpath.normpath(posixpath.join(ctx, dfile)) if ctx else posixpath.normpath(dfile)
            df = rd.dockerfile(df_rel)
            node["build"] = {"context": ctx or ".", "dockerfile": df_rel, "at": f"{rel}:{build.line}"}
            if df is None:
                node["build"]["why"] = "the Dockerfile is not in the project"
        image = svc.get("image")
        if image is not None and image.v:
            node["image"] = str(image.v)
            if df is None and _image_key(str(image.v)) in images:
                df, ctx, df_ev = images[_image_key(str(image.v))]
        ent, cmd = svc.get("entrypoint"), svc.get("command")
        st = df.stages[-1] if df and df.stages else None
        argv, cmd_at = [], []
        if ent is not None:
            argv += _argv(ent) or []
            cmd_at.append({"at": f"{rel}:{ent.line}", "text": "entrypoint: " + _cmd_text(_argv(ent) or [])})
        elif st and st.entrypoint:
            argv += st.entrypoint[0]
            cmd_at.append({"at": st.entrypoint[1], "text": "ENTRYPOINT " + _cmd_text(st.entrypoint[0])})
        if cmd is not None:
            argv += _argv(cmd) or []
            cmd_at.append({"at": f"{rel}:{cmd.line}", "text": "command: " + _cmd_text(_argv(cmd) or [])})
        elif st and st.cmd and ent is None:
            argv += st.cmd[0]
            cmd_at.append({"at": st.cmd[1], "text": "CMD " + _cmd_text(st.cmd[0])})
        wd = svc.get("working_dir")
        cmd_at += df_ev
        node["runs"] = []
        if argv:
            node["command"] = argv
            node["command_at"] = [c["at"] for c in cmd_at if "image" not in c]
            node["runs"] = rd.resolve(argv, cmd_at, df=df, context=ctx, near=ctx or base,
                                      workdir=_abs(str(wd.v), "/") if wd is not None and wd.v else None)
        else:
            node["why"] = "no command in the service or a Dockerfile of the project"
        out.append(node)
    return out


def _containers(y: Y | None, path: list[str]):
    """Every ``containers`` / ``initContainers`` item below ``y``."""
    if y is None:
        return
    if isinstance(y.v, dict):
        for k, v in y.v.items():
            if k in ("containers", "initContainers") and v is not None and isinstance(v.v, list):
                for c in v.v:
                    if isinstance(c.v, dict):
                        yield c, k
            else:
                yield from _containers(v, path + [k])
    elif isinstance(y.v, list):
        for x in y.v:
            yield from _containers(x, path)


def _k8s_nodes(rd: _Reader, rel: str, doc: Y, images: dict) -> list[dict]:
    kind, meta = doc.get("kind"), doc.get("metadata")
    if kind is None or doc.get("apiVersion") is None:
        return []
    name = meta.get("name") if meta is not None else None
    owner = f"{kind.v}/{name.v if name is not None else '?'}"
    out = []
    for c, _k in _containers(doc, []):
        cname = c.get("name")
        node: dict = {"kind": "k8s_container", "name": f"{owner}/{cname.v if cname is not None else '?'}",
                      "at": f"{rel}:{c.line}", "status": "statically_verified"}
        image = c.get("image")
        df, ctx, df_ev = None, posixpath.dirname(rel), []
        if image is not None and image.v:
            node["image"] = str(image.v)
            hit = images.get(_image_key(str(image.v)))
            if hit:
                df, ctx, df_ev = hit
                node["image_built_by"] = {"dockerfile": df.rel, "status": "weak_inference",
                                          "note": "matched by the image's name"}
        cmd, args = c.get("command"), c.get("args")
        st = df.stages[-1] if df and df.stages else None
        argv, cmd_at = [], []
        if cmd is not None:
            argv += _argv(cmd) or []
            cmd_at.append({"at": f"{rel}:{cmd.line}", "text": "command: " + _cmd_text(_argv(cmd) or [])})
        elif st and st.entrypoint:
            argv += st.entrypoint[0]
            cmd_at.append({"at": st.entrypoint[1], "text": "ENTRYPOINT " + _cmd_text(st.entrypoint[0])})
        if args is not None:
            argv += _argv(args) or []
            cmd_at.append({"at": f"{rel}:{args.line}", "text": "args: " + _cmd_text(_argv(args) or [])})
        elif st and st.cmd and cmd is None:
            argv += st.cmd[0]
            cmd_at.append({"at": st.cmd[1], "text": "CMD " + _cmd_text(st.cmd[0])})
        wd = c.get("workingDir")
        node["runs"] = []
        if argv:
            node["command"] = argv
            node["command_at"] = [x["at"] for x in cmd_at]
            node["runs"] = rd.resolve(argv, cmd_at, df=df, context=ctx, near=ctx,
                                      workdir=_abs(str(wd.v), "/") if wd is not None and wd.v else None)
            if df is not None:
                for r in node["runs"]:
                    if r.get("link") and r["link"]["status"] == "strong_inference":
                        r["link"]["status"] = "weak_inference"    # the image was matched by name only
                        r["link"]["note"] = "through the Dockerfile matched by the image's name"
        else:
            node["why"] = "no command, and no Dockerfile of the project builds its image"
        out.append(node)
    return out


# Terraform: the blocks as text (strings and braces counted), the string attributes that name project paths
_TF_BLOCK = re.compile(r'^\s*(resource|data)\s+"([^"]+)"\s+"([^"]+)"\s*\{')
_TF_STR = re.compile(r'^\s*([A-Za-z_][\w-]*)\s*=\s*"([^"]*)"')
_TF_LIST = re.compile(r'^\s*(command|args|entrypoint|entry_point)\s*=\s*\[(.*)\]\s*$')
_TF_PATHMOD = re.compile(r"\$\{path\.(module|root|cwd)\}/?")


def _tf_nodes(rd: _Reader, rel: str, text: str) -> list[dict]:
    out, cur, depth = [], None, 0
    base = posixpath.dirname(rel)
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#")[0] if "#" in raw and '"' not in raw.split("#")[0] else raw
        if cur is None:
            m = _TF_BLOCK.match(line)
            if not m:
                continue
            cur = {"kind": f"terraform_{m.group(1)}", "name": f"{m.group(2)}.{m.group(3)}", "type": m.group(2),
                   "at": f"{rel}:{no}", "status": "statically_verified", "runs": [], "_attrs": []}
            depth = 0
        stripped = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
        depth += stripped.count("{") - stripped.count("}")
        ms = _TF_STR.match(line)
        if ms:
            cur["_attrs"].append((ms.group(1), ms.group(2), no))
        ml = _TF_LIST.match(line)
        if ml:
            argv = [_unquote(x) for x in _split_flow(ml.group(2))]
            cur.setdefault("command", argv)
            cur.setdefault("command_at", [f"{rel}:{no}"])
            cur["runs"] += rd.resolve(argv, [{"at": f"{rel}:{no}", "text": raw.strip()}], df=None, context=base,
                                      workdir=None, near=base)
        if depth <= 0:
            _tf_links(rd, cur, base, rel)
            out.append(cur)
            cur = None
    if cur is not None:
        _tf_links(rd, cur, base, rel)
        out.append(cur)
    return out


def _tf_links(rd: _Reader, node: dict, base: str, rel: str) -> None:
    attrs = node.pop("_attrs")
    dirs = []
    for key, val, no in attrs:
        v = _TF_PATHMOD.sub("", val)
        if not v or "${" in v or "://" in v or v.startswith("/"):
            continue
        p = posixpath.normpath(posixpath.join(base, v)) if base else posixpath.normpath(v)
        if p.startswith(".."):
            continue
        is_file = p in rd.fileset
        is_dir = not is_file and any(f.startswith(p + "/") for f in rd.files)
        if is_file or is_dir:
            if is_dir:
                dirs.append(p)
            node["runs"].append({"kind": "path", "ref": val, "attribute": key,
                                 "link": {"file": p + ("/" if is_dir else ""), "line": 1 if is_file else None,
                                          "status": "strong_inference",
                                          "evidence": [{"at": f"{rel}:{no}", "text": f'{key} = "{val}"'}]}})
    for key, val, no in attrs:     # a function handler ("app.handler") inside the folder the resource packages
        if key != "handler" or "." not in val or "${" in val:
            continue
        mod, func = val.rsplit(".", 1)
        ends = [mod.replace(".", "/") + e for e in (".py", ".js", ".mjs", ".ts")]
        hits = [h for d in dirs for h in rd.by_suffix(ends, d) if h.startswith(d + "/")]
        where = "in the folder the resource names"
        if not hits:
            hits, where = rd.by_suffix(ends, base), "by its path's ending (nearest the .tf file first)"
        entry = {"kind": "module", "ref": val, "attribute": key}
        if hits:
            entry["link"] = {"file": hits[0], "line": rd.entry_line(hits[0], func), "status": "weak_inference",
                             "evidence": [{"at": f"{rel}:{no}", "text": f'{key} = "{val}"'}],
                             "note": f"the handler's module, found {where}"}
        else:
            entry["link"], entry["why"] = None, "no project file found for it"
        node["runs"].append(entry)


def run(repo: Path, files: list[str] | None = None) -> dict:
    """Read the project's infrastructure manifests; see the module docstring."""
    from verinoda.snapshot import listed_files

    repo = Path(repo)
    files = files if files is not None else listed_files(repo)
    rd = _Reader(repo, files)
    df_rels = [f for f in files if _DOCKERFILE.search(f)]
    compose = [f for f in files if _COMPOSE.search(f)]
    yamls = [f for f in files if f.endswith((".yaml", ".yml")) and f not in compose]
    tfs = [f for f in files if f.endswith(".tf")]
    nodes: list[dict] = []
    # images a Dockerfile of the project builds, by name: its folder's name, a Dockerfile.NAME suffix, and the
    # image a compose service with that build names
    images: dict[str, tuple] = {}
    for rel in df_rels:
        df = rd.dockerfile(rel)
        if df is None:
            continue
        nodes.append(_docker_node(rd, df))
        ctx = posixpath.dirname(rel)
        keys = [posixpath.basename(ctx)] if ctx else []
        m = re.search(r"(?:Dockerfile|Containerfile)\.([\w-]+)$|([\w-]+)\.[Dd]ockerfile$", rel)
        if m:
            keys.append(m.group(1) or m.group(2))
        for k in keys:
            images.setdefault(k.lower(), (df, ctx, []))
    compose_docs = {}
    for rel in compose:
        t = rd.text(rel)
        compose_docs[rel] = yaml_docs(t) if t is not None else []
        for doc in compose_docs[rel]:
            services = doc.get("services")
            for name, svc in (services.v.items() if services is not None and isinstance(services.v, dict) else ()):
                if not isinstance(svc.v, dict) or svc.get("build") is None:
                    continue
                b = svc.get("build")
                base = posixpath.dirname(rel)
                ctx_raw = b.v if isinstance(b.v, str) else str((b.get("context") or Y(".", 0)).v or ".")
                dfile = "Dockerfile" if isinstance(b.v, str) else str((b.get("dockerfile") or Y("Dockerfile", 0)).v)
                ctx = posixpath.normpath(posixpath.join(base, ctx_raw))
                ctx = "" if ctx == "." else ctx
                df = rd.dockerfile(posixpath.normpath(posixpath.join(ctx, dfile)) if ctx else dfile)
                if df is None:
                    continue
                img = svc.get("image")
                for k in [name] + ([_image_key(str(img.v))] if img is not None and img.v else []):
                    images[k.lower()] = (df, ctx, [{"at": f"{rel}:{(img or svc).line}", "image": True,
                                                    "text": f"service {name} builds {df.rel}"}])
    for rel in compose:
        for doc in compose_docs[rel]:
            nodes += _compose_nodes(rd, rel, doc, images)
    for rel in yamls:
        t = rd.text(rel)
        if t is None or "apiVersion" not in t or "kind" not in t:
            continue
        for doc in yaml_docs(t):
            if isinstance(doc.v, dict):
                nodes += _k8s_nodes(rd, rel, doc, images)
    for rel in tfs:
        t = rd.text(rel)
        if t is not None:
            nodes += _tf_nodes(rd, rel, t)
    for n in nodes:
        for r in n.get("runs", ()):
            for e in (r.get("link") or {}).get("evidence", ()):
                e.pop("image", None)
    linked = sum(1 for n in nodes if any(r.get("link") for r in n.get("runs", ())))
    return {"status": "found" if nodes else "none", "nodes": nodes, "count": len(nodes), "linked": linked,
            "files_read": {"dockerfile": len(df_rels), "compose": len(compose),
                           "kubernetes": len({n["at"].rsplit(":", 1)[0] for n in nodes
                                              if n["kind"] == "k8s_container"}),
                           "terraform": len(tfs)},
            "not_read": sorted(set(rd.not_read))}


def links_for(res: dict, rel: str) -> list[dict]:
    """The nodes whose command runs the project file ``rel`` (for a reader asking "what runs this file?")."""
    return [n for n in res["nodes"] for r in n.get("runs", ())
            if r.get("link") and r["link"]["file"].rstrip("/") == rel]


def render(res: dict) -> str:
    fr = res["files_read"]
    out = [f"infra: {res['count']} node(s), {res['linked']} linked to a project file "
           f"(Dockerfiles {fr['dockerfile']}, compose files {fr['compose']}, Kubernetes files {fr['kubernetes']}, "
           f"Terraform files {fr['terraform']})"]
    for n in res["nodes"]:
        extra = f"  image {n['image']}" if n.get("image") else ""
        out.append(f"  {n['kind']}  {n['name']}  {n['at']}{extra}")
        if n.get("image_built_by"):
            out.append(f"    image built by {n['image_built_by']['dockerfile']} [weak_inference: by name]")
        if n.get("command"):
            out.append(f"    command  {_cmd_text(n['command'])}  ({', '.join(n['command_at'])})")
        for r in n.get("runs", ()):
            link = r.get("link")
            if link:
                at = f"{link['file']}:{link['line']}" if link.get("line") else link["file"]
                via = [e["at"] for e in link["evidence"]]
                out.append(f"    -> {at}  [{link['status']}]  {r['kind']} {r['ref']}  via {', '.join(via)}")
                if link.get("note"):
                    out.append(f"       {link['note']}")
            else:
                out.append(f"    -> no link: {r['kind']} {r['ref']} ({r.get('why', '')})")
        if n.get("why") and not n.get("runs"):
            out.append(f"    {n['why']}")
    if res["not_read"]:
        out.append(f"not read (over 1 MB or unreadable): {', '.join(res['not_read'][:10])}")
    if not res["nodes"]:
        out.append("no Dockerfile, compose file, Kubernetes manifest or Terraform file in the project")
    out.append("Commands are statically_verified (the manifest line says them); links are strong_inference "
               "through COPY lines or a path the manifest names, weak_inference when found by name.")
    return "\n".join(out)
