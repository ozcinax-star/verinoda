"""Package -> source commit by content (docs/DESIGN.md D14, the strongest mapping).

A published artifact (sdist, crate, npm tarball, wheel) is compared with a
commit of the source repository by *git blob hashes*: every artifact file is
hashed the way git hashes a blob (``sha1("blob <len>\\0" + bytes)``) and
looked up among the blobs of ``git ls-tree -r <commit>``. Paths are ignored,
so a src-layout or packaging directory does not matter; only the trees are
needed, which a blobless mirror (``git clone --bare --filter=blob:none``)
already has.

Files the packaging step writes or rewrites are skipped (``PKG-INFO``,
``*.egg-info/``, ``*.dist-info/``, ``setup.cfg`` of sdists built by setuptools,
``.cargo_vcs_info.json``, ``Cargo.toml`` (normalised; ``Cargo.toml.orig`` is
the original), ``package.json`` (npm may add ``gitHead``)). A pin needs
``matched == total``; anything less is mismatch M8 with the misses listed -
the ratio is reported and never rounded up to "verified".
"""

from __future__ import annotations

import hashlib
import tarfile
import zipfile
from pathlib import Path

from verinoda.references.gitref import run_git

GENERATED = ("PKG-INFO", "setup.cfg", ".cargo_vcs_info.json", "Cargo.toml", "package.json", "RECORD", "WHEEL",
             "METADATA", "INSTALLER", "REQUESTED", "direct_url.json")
GENERATED_DIRS = (".egg-info/", ".dist-info/")
MAX_FILES = 5000
MAX_FILE_BYTES = 20 * 1024 * 1024


def blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _generated(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name in GENERATED or any(d in rel + "/" for d in GENERATED_DIRS) or rel.endswith(".pyc")


def artifact_files(path: Path):
    """``(relative path, bytes)`` of an sdist/crate (tar) or wheel/zip, without the top directory of a tarball."""
    path = Path(path)
    count = 0
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir() or info.file_size > MAX_FILE_BYTES:
                    continue
                count += 1
                if count > MAX_FILES:
                    return
                yield info.filename, z.read(info)
        return
    with tarfile.open(path) as t:
        for m in t.getmembers():
            if not m.isfile() or m.size > MAX_FILE_BYTES:
                continue
            count += 1
            if count > MAX_FILES:
                return
            rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
            f = t.extractfile(m)
            if f is not None:
                yield rel, f.read()


def tree_blobs(git_dir: Path, commit: str, *, subdir: str | None = None) -> dict[str, list[str]]:
    args = ["ls-tree", "-r", "--full-tree", commit]
    if subdir:
        args.append(subdir.rstrip("/") + "/")
    gd = git_dir / ".git" if (git_dir / ".git").exists() else git_dir
    rc, out, err = run_git(args, git_dir=gd)
    if rc != 0:
        raise ValueError(f"git ls-tree {commit[:12]} failed: {err.strip()[-200:]}")
    blobs: dict[str, list[str]] = {}
    for line in out.splitlines():
        meta, _, p = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            blobs.setdefault(parts[2], []).append(p)
    return blobs


def match(git_dir: Path, commit: str, artifact: Path, *, subdir: str | None = None) -> dict:
    """``{matched, total, ratio, misses, skipped, commit}`` for one commit."""
    blobs = tree_blobs(Path(git_dir), commit, subdir=subdir)
    total = matched = skipped = 0
    misses: list[str] = []
    for rel, data in artifact_files(artifact):
        if _generated(rel):
            skipped += 1
            continue
        total += 1
        if blob_sha1(data) in blobs:
            matched += 1
        else:
            misses.append(rel)
    return {"commit": commit, "matched": matched, "total": total,
            "ratio": round(matched / total, 4) if total else 0.0, "misses": misses[:10], "skipped": skipped}


def rank(git_dir: Path, candidates: list[tuple[str, str]], artifact: Path, *, subdir: str | None = None) -> list[dict]:
    """Match each ``(name, commit)``; best first. The winner is a pin only when its ratio is 1.0."""
    out = []
    for name, commit in candidates:
        try:
            m = match(git_dir, commit, artifact, subdir=subdir)
        except ValueError as exc:
            out.append({"name": name, "commit": commit, "error": str(exc)})
            continue
        out.append({"name": name, **m})
    out.sort(key=lambda x: (-(x.get("ratio") or 0.0), -(x.get("matched") or 0)))
    return out


def verdict(ranked: list[dict]) -> dict:
    """Mapping entry for the best candidate: ``content_match`` when exact, else M8 material."""
    best = next((x for x in ranked if "ratio" in x), None)
    if best is None:
        return {"method": "content_match", "strength": "none", "why": "no candidate could be compared"}
    exact = best["total"] > 0 and best["matched"] == best["total"]
    return {"method": "content_match", "strength": "verified" if exact else "partial", "commit": best["commit"],
            "name": best["name"], "matched": best["matched"], "total": best["total"], "ratio": best["ratio"],
            "misses": best["misses"], "others": [{k: x.get(k) for k in ("name", "matched", "total")}
                                                 for x in ranked[1:4] if "ratio" in x]}
