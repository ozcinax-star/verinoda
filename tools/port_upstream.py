"""Port a pinned Graphify checkout into verinoda.project_index.

Usage: python tools/port_upstream.py <path-to-graphify-checkout>

Copies the upstream `graphify/` package to `verinoda/project_index/` and the
upstream test-suite to `tests_upstream/`, rewriting only module paths
(`graphify.<mod>` -> `verinoda.project_index.<mod>`) and the distribution
name used for version lookups. Everything else is copied byte-for-byte so a
future upstream sync is a re-run of this script plus a review of the diff.
The upstream commit is recorded in `verinoda/project_index/UPSTREAM_COMMIT`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NEW_PKG = "verinoda.project_index"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


PORT_CONFTEST = '''

# --- Verinoda port adjustments (appended by tools/port_upstream.py) --------
# test_skillgen.py regenerates upstream's own skill bodies and audits them
# against blobs from upstream's git history (`git show <sha>:graphify/...`).
# That history does not exist in this repository, and those skill bodies are
# not Verinoda's product skills, so the suite is excluded (docs/UPSTREAM.md).
collect_ignore = ["test_skillgen.py"]

_PORT_DIVERGENT = {
    # Upstream's installer compares the skill stamp to the *graphifyy* version
    # line (0.8.x/0.9.x). Verinoda ships as 0.1.x, so the "stale skill"
    # fixture looks newer instead of older and takes the downgrade branch.
    "test_the_warning_names_the_stale_destination_and_the_exact_command":
        "version-line divergence: verinoda 0.1.x < graphifyy 0.8.36 fixture",
}


def _verinoda_port_marks(items):
    for item in items:
        reason = _PORT_DIVERGENT.get(item.name)
        if reason:
            item.add_marker(pytest.mark.xfail(reason=reason, strict=True))


_upstream_modifyitems = pytest_collection_modifyitems


def pytest_collection_modifyitems(items):  # noqa: F811
    _upstream_modifyitems(items)
    _verinoda_port_marks(items)
'''


def _modules(pkg_dir: Path) -> set[str]:
    mods = {p.stem for p in pkg_dir.glob("*.py")}
    mods |= {p.name for p in pkg_dir.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    return mods


def rewrite(text: str, mods: set[str], *, is_test: bool) -> str:
    alt = "|".join(sorted((re.escape(m) for m in mods), key=len, reverse=True))
    text = re.sub(rf"(?<![\w./-])graphify\.({alt})\b", rf"{NEW_PKG}.\1", text)
    text = re.sub(r"\bfrom graphify import\b", f"from {NEW_PKG} import", text)
    text = re.sub(r"^(\s*)import graphify$", rf"\1import {NEW_PKG} as graphify", text, flags=re.M)
    text = re.sub(r"(\"-m\",\s*)\"graphify\"", rf'\1"{NEW_PKG}"', text)
    text = re.sub(r"(-m) graphify(\s)", rf"\1 {NEW_PKG}\2", text)
    text = text.replace('_pkg_version("graphifyy")', '_pkg_version("verinoda")')
    if is_test:
        # Repo-relative joins that meant "the package directory".
        text = re.sub(
            r"((?:REPO_ROOT|REPO|ROOT|parent\.parent|parents\[1\])\s*/\s*)\"graphify\"",
            r'\1"verinoda" / "project_index"',
            text,
        )
        # Package-relative data paths (skill bodies, references, always-on blocks).
        text = re.sub(
            r"(f?[\"'])graphify/(skill|skills/|always_on|\{)",
            r"\1verinoda/project_index/\2",
            text,
        )
        text = text.replace('f"graphify.{', 'f"verinoda.project_index.{')
        text = re.sub(r"\"tests/test_", '"tests_upstream/test_', text)
        text = text.replace('parent.parent / "docs" / "how-it-works.md"',
                            'parent.parent / "docs" / "upstream" / "how-it-works.md"')
        # Upstream root docs live under docs/upstream/ in this repo.
        text = re.sub(
            r"(parent\.parent\s*/\s*)\"(ARCHITECTURE|README|CHANGELOG)\.md\"",
            r'\1"docs" / "upstream" / "\2.md"',
            text,
        )
    return text


def port(src: Path) -> str:
    sha = subprocess.check_output(["git", "-C", str(src), "rev-parse", "HEAD"], text=True).strip()
    mods = _modules(src / "graphify")
    dst_pkg = REPO / "verinoda" / "project_index"
    dst_tests = REPO / "tests_upstream"
    for d in (dst_pkg, dst_tests):
        if d.exists():
            shutil.rmtree(d)
    shutil.copytree(src / "graphify", dst_pkg, ignore=IGNORE)
    shutil.copytree(src / "tests", dst_tests, ignore=IGNORE)
    for root, is_test in ((dst_pkg, False), (dst_tests, True)):
        for f in root.rglob("*"):
            if f.suffix not in (".py", ".md") or "fixtures" in f.parts:
                continue
            old = f.read_text(encoding="utf-8")
            new = rewrite(old, mods, is_test=is_test)
            if new != old:
                f.write_text(new, encoding="utf-8", newline="")
    # Upstream repo-level files some upstream tests read.
    for rel in ("tools/skillgen", "tools/__init__.py"):
        s, d = src / rel, REPO / rel
        if d.exists():
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        (shutil.copytree(s, d, ignore=IGNORE) if s.is_dir() else shutil.copy2(s, d))
    for rel in ("tools/skillgen",):
        for f in (REPO / rel).rglob("*.py"):
            old = f.read_text(encoding="utf-8")
            new = rewrite(old, mods, is_test=True)
            if new != old:
                f.write_text(new, encoding="utf-8", newline="")
    up = REPO / "docs" / "upstream"
    up.mkdir(parents=True, exist_ok=True)
    for name in ("ARCHITECTURE.md", "README.md", "CHANGELOG.md", "BENCHMARKS.md", "SECURITY.md"):
        shutil.copy2(src / name, up / name)
    shutil.copy2(src / "docs" / "how-it-works.md", up / "how-it-works.md")
    conftest = dst_tests / "conftest.py"
    conftest.write_text(conftest.read_text(encoding="utf-8") + PORT_CONFTEST, encoding="utf-8")
    (dst_pkg / "UPSTREAM_COMMIT").write_text(sha + "\n", encoding="utf-8")
    return sha


if __name__ == "__main__":
    print(port(Path(sys.argv[1]).resolve()))
