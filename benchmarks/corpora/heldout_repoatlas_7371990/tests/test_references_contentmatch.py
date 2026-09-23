"""Package -> commit by git blob hashes (repoatlas.references.contentmatch), on a synthetic sdist."""

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

from repoatlas.references import contentmatch as cm

FX = Path(__file__).parent / "fixtures" / "references"
_spec = importlib.util.spec_from_file_location("references_helpers", FX / "helpers.py")
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)


def _sdist(repo: Path, commit: str, out: Path, *, extra: dict | None = None, drop: str | None = None) -> Path:
    """An sdist-like tarball of the files at ``commit`` (plus the generated files packaging adds)."""
    names = helpers.git(repo, "ls-tree", "-r", "--name-only", commit).splitlines()
    with tarfile.open(out, "w:gz") as t:
        def add(name, data):
            info = tarfile.TarInfo(f"requests-x/{name}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))

        for n in names:
            if n == drop:
                continue
            data = __import__("subprocess").run(["git", "-C", str(repo), "show", f"{commit}:{n}"],
                                                capture_output=True, check=True).stdout
            add(n, data)
        add("PKG-INFO", b"Metadata-Version: 2.1\nName: requests\n")
        add("requests.egg-info/SOURCES.txt", b"x\n")
        for k, v in (extra or {}).items():
            add(k, v)
    return out


def test_blob_hash_equals_git(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"hello\n")
    assert cm.blob_sha1(b"hello\n") == helpers.git(tmp_path, "hash-object", str(p))


def test_the_sdist_matches_exactly_one_tag(tmp_path):
    shas = helpers.requests_repo(tmp_path / "requests")
    repo = tmp_path / "requests"
    art = _sdist(repo, shas["v2.31.0"], tmp_path / "requests-2.31.0.tar.gz")
    ranked = cm.rank(repo, [(t, shas[t]) for t in ("main", "v2.30.0", "v2.31.0", "v2.32.0")], art)
    assert ranked[0]["name"] == "v2.31.0" and ranked[0]["ratio"] == 1.0 and ranked[0]["skipped"] == 2
    assert all(r["ratio"] < 1.0 for r in ranked[1:])
    v = cm.verdict(ranked)
    assert v["strength"] == "verified" and v["commit"] == shas["v2.31.0"]
    # a file the release does not have in git (generated, patched, vendored) leaves it partial -> M8 material
    patched = _sdist(repo, shas["v2.31.0"], tmp_path / "patched.tar.gz", extra={"requests/_vendor.py": b"x = 1\n"})
    v2 = cm.verdict(cm.rank(repo, [("v2.31.0", shas["v2.31.0"])], patched))
    assert v2["strength"] == "partial" and v2["misses"] == ["requests/_vendor.py"]


def test_wheels_are_zip_files(tmp_path):
    shas = helpers.requests_repo(tmp_path / "requests")
    repo = tmp_path / "requests"
    whl = tmp_path / "requests-2.30.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        for n in ("requests/__init__.py", "requests/sessions.py"):
            z.writestr(n, __import__("subprocess").run(["git", "-C", str(repo), "show", f"{shas['v2.30.0']}:{n}"],
                                                       capture_output=True, check=True).stdout)
        z.writestr("requests-2.30.0.dist-info/METADATA", "Name: requests\n")
    m = cm.match(repo, shas["v2.30.0"], whl)
    assert (m["matched"], m["total"], m["skipped"]) == (2, 2, 1)
