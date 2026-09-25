"""What each Python file says about imports and calls, kept between builds.

Symbol resolution walks every node of every ``.py`` file twice on each build (once for
``from … import`` statements, once for calls in top-level functions): about 6.5 of the 28
seconds of a ``verinoda update`` of Verinoda's own repository. What those walks find depends
only on the file's bytes and its path, so it is kept per file, keyed by a hash of the bytes;
where an import points is still worked out on every build, because that depends on which
other files exist. The upstream pass is replaced for the duration of a build (no file under
``verinoda/project_index`` is edited) and does exactly what the upstream one does, in the
same order; ``tests/test_python_facts.py`` compares the two.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

VERSION = 2
FILE = "python_facts.json"


def _stamp(res) -> str:
    """What the kept facts were made with: the upstream module doing the walks and the Python grammar."""
    from importlib import metadata

    try:
        grammar = metadata.version("tree-sitter-python")
    except metadata.PackageNotFoundError:
        grammar = "?"
    try:
        code = hashlib.blake2b(Path(res.__file__).read_bytes(), digest_size=8).hexdigest()
    except OSError:
        code = "?"
    return f"{VERSION}:{grammar}:{code}"


def parse_current(res, path: Path, data: bytes, parse=None):
    """The upstream parse of ``path``, of the bytes ``data`` just read from it.

    The upstream memo is keyed by (path, mtime, size): in a long-lived process a file changed within
    one mtime tick at the same size gets the older tree. What is kept under the hash of ``data`` must
    come from ``data``, so a memo answer for other bytes is parsed again without the memo. ``parse`` is
    the upstream parse function, for a caller that has replaced it on the module for the moment.
    """
    parsed = (parse or res._parse_python_tree)(path)
    if parsed is None or parsed[0] == data:
        return parsed
    fresh = getattr(res._parse_python_tree_cached, "__wrapped__", None)
    if fresh is None:
        return None
    try:
        st = path.stat()
        return fresh(str(path), st.st_mtime_ns, st.st_size)
    except Exception:  # noqa: BLE001 - as the upstream parse: any error means "skip this file"
        return None


def _kept_ok(hit) -> bool:
    """Is a kept entry shaped as this module writes it (a damaged file is walked again, not trusted)?"""
    try:
        for _line, _level, _module, names in hit["imports"]:
            for _imported, _local in names:
                pass
        for _source_id, flat in hit["calls"]:
            if len(flat) % 2:
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def _local_facts(res, path: Path, data: bytes) -> dict | None:
    """The imports and calls of one file, as the upstream walks find them; None if it does not parse."""
    parsed = parse_current(res, path, data)
    if parsed is None:
        return None
    source, root_node = parsed
    imports = []
    for node in res._walk_python_tree(root_node):
        if node.type != "import_from_statement":
            continue
        module = res._python_import_from_module(node, source)
        if module is None:
            continue
        level, module_name = module
        imports.append([node.start_point[0] + 1, level, module_name,
                        [list(x) for x in res._python_imported_names(node, source)]])
    calls = []  # per top-level function: [source id, [name, line, name, line, ...]]
    for source_id, body in res._python_top_level_function_bodies(path, root_node, source):
        flat = []
        for node in res._walk_python_tree(body):
            name = res._python_call_identifier(node, source)
            if name is not None:
                flat += [name, node.start_point[0] + 1]
        if flat:
            calls.append([source_id, flat])
    return {"imports": imports, "calls": calls}


class python_facts_cache:
    """Context manager: during a build, the Python symbol facts pass reads unchanged files from the cache."""

    def __init__(self, index_dir: Path):
        self.path = Path(index_dir) / FILE
        self.hits = self.misses = 0

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return {}
        files = data.get("files") if isinstance(data, dict) and data.get("stamp") == self.stamp else None
        return files if isinstance(files, dict) else {}  # a file of another shape counts as empty

    def _save(self, files: dict) -> None:
        text = json.dumps({"stamp": self.stamp, "files": files}, separators=(",", ":"))
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="python_facts.", suffix=".tmp", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)  # refused on Windows while another process has the file open
            tmp = None
        except OSError:
            pass  # a cache: the next build walks the files again
        finally:
            if tmp is not None:  # not moved into place: no temp file is left behind
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def __enter__(self):
        from verinoda.project_index.extractors import resolution as res

        self.res, self.real = res, res._collect_python_symbol_resolution_facts
        self.stamp = _stamp(res)
        cache = self._load()

        def collect(paths, root, facts):
            py_paths = [path for path in paths if path.suffix == ".py"]
            if not py_paths:
                return
            seen, changed, local = {}, False, {}
            for path in py_paths:
                key = str(path)
                if key in local:
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    local[key] = None  # unreadable: skipped, as the upstream parse skips it
                    continue
                digest = hashlib.blake2b(data, digest_size=16).hexdigest()
                hit = cache.get(key)
                if isinstance(hit, dict) and hit.get("h") == digest and _kept_ok(hit):
                    self.hits += 1
                    found = hit
                else:
                    self.misses += 1
                    found = _local_facts(res, path, data)
                    if found is not None:
                        found = {"h": digest, **found}
                        changed = True
                local[key] = found
                if found is not None:
                    seen[key] = found
            # the upstream order: every file's imports, then every file's calls
            for path in py_paths:
                found = local.get(str(path))
                if found is None:
                    continue
                for line, level, module_name, names in found["imports"]:
                    target_path = res._resolve_python_module_path(module_name, path, root, level)
                    if target_path is not None:
                        pkg_dir = target_path.parent if target_path.name == "__init__.py" else None
                    else:
                        pkg_dir = res._resolve_python_namespace_dir(module_name, path, root, level)
                        if pkg_dir is None:
                            continue
                    for imported_name, local_name in names:
                        if pkg_dir is not None:
                            sub_py = pkg_dir / f"{imported_name}.py"
                            sub_pkg = pkg_dir / imported_name / "__init__.py"
                            submodule = sub_py if sub_py.is_file() else (sub_pkg if sub_pkg.is_file() else None)
                            if submodule is not None:
                                facts.module_imports.append((path, submodule, line, local_name))
                                continue
                        if target_path is None:
                            continue
                        facts.imports.append(res._SymbolImportFact(path, local_name, target_path, imported_name, line))
                        if path.name == "__init__.py":
                            facts.exports.append(res._SymbolExportFact(
                                path, local_name, line, target_path=target_path, target_name=imported_name))
            for path in py_paths:
                found = local.get(str(path))
                if found is None:
                    continue
                for source_id, flat in found["calls"]:
                    for i in range(0, len(flat), 2):
                        facts.uses.append(res._SymbolUseFact(path, source_id, flat[i], "calls", "call", flat[i + 1]))
            if changed or set(seen) != set(cache):  # only this build's files are kept
                self._save(seen)
                cache.clear()
                cache.update(seen)

        res._collect_python_symbol_resolution_facts = collect
        return self

    def __exit__(self, *a):
        self.res._collect_python_symbol_resolution_facts = self.real
        return False
