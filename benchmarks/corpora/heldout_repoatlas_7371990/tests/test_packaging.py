"""Packaging: what the wheel ships, and that it works on its own.

* ``pyproject.toml`` package-data covers every runtime data file in the source tree.
* ``uv build --wheel`` (from a temp copy of the sources) ships those files, every
  module, the licence files in ``*.dist-info``, and nothing Graphify-branded: no
  top-level ``graphify`` package, no ``graphify`` / ``graphify-mcp`` scripts.
* The wheel installs into a FRESH ``uv venv`` and the installed ``repoatlas`` runs
  ``--version``, ``--help``, ``doctor --json``, ``scan`` and ``analyze --json`` on a
  git-committed copy of examples/orders_app, without Graphify being importable.
* ``uv tool install`` (``UV_TOOL_DIR`` / ``UV_TOOL_BIN_DIR`` in temp dirs) and
  ``pipx install`` via ``uv tool run pipx`` (``PIPX_HOME`` / ``PIPX_BIN_DIR`` in
  temp dirs) produce a working ``repoatlas`` executable.

Nothing is written outside pytest temp dirs except the uv/pip download caches:
HOME/USERPROFILE are redirected for every installed-CLI run, and uv never gets
the user's real tool dir. The build/install tests need ``uv`` on PATH (skipped
otherwise) and network access unless the uv cache is warm. They take ~10-40 s
together, so they run by default; the pipx check is marked ``slow`` (it installs
with pip from the index): run it with ``pytest tests/test_packaging.py -m slow``.
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "repoatlas"
EXAMPLE = ROOT / "examples" / "orders_app"
QUESTION = "How does an order get from the API handler to the database?"

# Non-Python files under repoatlas/ that are deliberately NOT shipped (never read at runtime).
NOT_RUNTIME = {
    "repoatlas/project_index/extractors/MIGRATION.md": "developer note on porting extractors",
}
# Data files that must be in every wheel, whatever else changes.
MUST_SHIP = (
    "repoatlas/project_index/UPSTREAM_COMMIT",
    "repoatlas/project_index/skill.md",
    "repoatlas/project_index/always_on/claude-md.md",
    "repoatlas/project_index/skills/claude/references/query.md",
    "repoatlas/agents/templates/claude_SKILL.md",
    "repoatlas/agents/templates/codex_SKILL.md",
    "repoatlas/benchmark/questions/orders_app.json",
    "repoatlas/benchmark/questions/graphify_core.json",
)
LICENSES = ("LICENSE", "LICENSE-MIT", "NOTICE")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.egg-info", ".pytest_cache", ".repoatlas",
                                "build", "dist", "*.db")


def _pyproject() -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _owning_package(path: Path, top: Path) -> Path:
    """Deepest ancestor directory of ``path`` that is a package (has __init__.py)."""
    d = path.parent
    while d != top.parent:
        if (d / "__init__.py").is_file():
            return d
        d = d.parent
    raise AssertionError(f"{path} is not inside a package")


def _files(pkg_root: Path) -> tuple[set[str], set[str]]:
    """(python modules, other data files) under ``pkg_root`` as wheel-style posix paths."""
    py, data = set(), set()
    for p in pkg_root.rglob("*"):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"):
            continue
        rel = p.relative_to(pkg_root.parent).as_posix()
        (py if p.suffix == ".py" else data).add(rel)
    return py, data


# -- fast: configuration only ------------------------------------------------------

def test_package_data_globs_cover_every_runtime_data_file():
    pdata = _pyproject()["tool"]["setuptools"]["package-data"]
    matched: set[str] = set()
    for pkg, globs in pdata.items():
        pdir = ROOT.joinpath(*pkg.split("."))
        assert (pdir / "__init__.py").is_file(), f"package-data names a non-package: {pkg}"
        for g in globs:
            hits = [p for p in pdir.glob(g) if p.is_file()]
            assert hits, f"package-data glob {pkg}:{g} matches nothing"
            matched |= {p.relative_to(ROOT).as_posix() for p in hits}
    _, data = _files(PKG)
    missing = sorted(data - matched - set(NOT_RUNTIME))
    assert not missing, f"data files not shipped by package-data: {missing}"
    # Globs are relative to the OWNING package: a file in a sub-package needs its own entry.
    for rel in matched:
        owner = _owning_package(ROOT / rel, PKG)
        assert owner.relative_to(ROOT).as_posix().replace("/", ".") in pdata, rel


def test_pyproject_ships_only_repoatlas():
    pp = _pyproject()
    assert set(pp["project"]["scripts"]) == {"repoatlas"}
    assert "gui-scripts" not in pp["project"] and "entry-points" not in pp["project"]
    assert pp["tool"]["setuptools"]["packages"]["find"]["include"] == ["repoatlas*"]
    assert set(pp["project"]["license-files"]) == set(LICENSES)
    assert all((ROOT / f).is_file() for f in LICENSES)


# -- wheel build --------------------------------------------------------------------

def _uv() -> str:
    exe = shutil.which("uv")
    if exe is None:
        pytest.skip("uv not on PATH")
    return exe


def _clean_env(**extra: str) -> dict:
    """Environment for child processes: no source tree on the path, no inherited venv."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "VIRTUAL_ENV", "GRAPHIFY_OUT", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT")
           and not k.startswith(("UV_TOOL_", "PIPX_"))}
    env.update({"PYTHONIOENCODING": "utf-8", "UV_PYTHON_DOWNLOADS": "never", "UV_NO_PROGRESS": "1"})
    env.update(extra)
    return env


def _run(cmd: list, *, cwd: Path, env: dict, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, capture_output=True, text=True, check=False,
                          encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)


def _ok(r: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    assert r.returncode == 0, f"{what}: rc={r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-3000:]}"
    return r


def _base_python() -> str:
    """The interpreter behind this test run (a venv's base), so uv never downloads one."""
    return getattr(sys, "_base_executable", None) or sys.executable


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Wheel built by `uv build --wheel` from a temp copy of the sources (the repo is not touched)."""
    uv = _uv()
    base = tmp_path_factory.mktemp("wheel")
    src = base / "src"
    src.mkdir()
    for f in ("pyproject.toml", "README.md", *LICENSES):
        shutil.copy2(ROOT / f, src / f)
    shutil.copytree(PKG, src / "repoatlas", ignore=IGNORE)
    out = base / "dist"
    cmd = [uv, "build", "--wheel", "--python", _base_python(), "--out-dir", out, src]
    r = _run(cmd, cwd=base, env=_clean_env())
    if r.returncode != 0:  # offline: try the cached build backend before failing
        r = _run([*cmd[:2], "--offline", *cmd[2:]], cwd=base, env=_clean_env())
    _ok(r, "uv build --wheel")
    wheels = list(out.glob("repoatlas-*.whl"))
    assert len(wheels) == 1, wheels
    return {"wheel": wheels[0], "src": src}


def test_wheel_contents(built):
    whl = built["wheel"]
    with zipfile.ZipFile(whl) as z:
        names = set(z.namelist())
        dist_info = {n.split("/")[0] for n in names if n.split("/")[0].endswith(".dist-info")}
        assert len(dist_info) == 1, dist_info
        di = dist_info.pop()
        eps = configparser.ConfigParser()
        eps.read_string(z.read(f"{di}/entry_points.txt").decode("utf-8"))
        top_level = z.read(f"{di}/top_level.txt").decode("utf-8").split()
        meta = z.read(f"{di}/METADATA").decode("utf-8")

    # Only the repoatlas package: no top-level graphify package or module.
    assert {n.split("/")[0] for n in names} == {"repoatlas", di}
    assert top_level == ["repoatlas"]
    assert not [n for n in names if n.split("/")[0].lower().startswith("graphify")]
    # Only the repoatlas console script: no graphify / graphify-mcp.
    assert eps.sections() == ["console_scripts"]
    assert dict(eps["console_scripts"]) == {"repoatlas": "repoatlas.cli:main"}
    assert "Name: repoatlas" in meta

    # Every module and every runtime data file of the built tree is in the wheel.
    py, data = _files(built["src"] / "repoatlas")
    assert not sorted(py - names), sorted(py - names)[:20]
    assert not sorted(data - names - set(NOT_RUNTIME)), sorted(data - names - set(NOT_RUNTIME))
    for rel in MUST_SHIP:
        assert rel in names, rel
    skills = [n for n in names if n.startswith("repoatlas/project_index/skill") and n.endswith(".md")]
    refs = [n for n in names if n.startswith("repoatlas/project_index/skills/") and n.endswith(".md")]
    assert len(skills) >= 16 and len(refs) >= 8, (len(skills), len(refs))
    # Licence files travel in the dist-info (PEP 639: .dist-info/licenses/).
    for lic in LICENSES:
        assert f"{di}/licenses/{lic}" in names or f"{di}/{lic}" in names, lic


# -- installed wheel ------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)


def _orders_repo(base: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    dst = base / "orders_app"
    shutil.copytree(EXAMPLE, dst, ignore=IGNORE)
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    return dst


def _exe(bindir: Path, name: str) -> Path:
    for cand in (bindir / f"{name}.exe", bindir / name):
        if cand.is_file():
            return cand
    raise AssertionError(f"{name} not found in {bindir}: {sorted(p.name for p in bindir.iterdir())}")


def _drive_installed_cli(ra: Path, base: Path, *, full: bool) -> dict:
    """Run the installed `repoatlas` with a throw-away HOME; returns a few facts for the caller."""
    home = base / "home"
    work = base / "work"
    home.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    env = _clean_env(HOME=str(home), USERPROFILE=str(home))
    r = _ok(_run([ra, "--version"], cwd=work, env=env), "--version")
    assert r.stdout.startswith("repoatlas ")
    r = _ok(_run([ra, "--help"], cwd=work, env=env), "--help")
    assert all(c in r.stdout for c in ("scan", "analyze", "doctor", "install", "index"))
    doc = json.loads(_ok(_run([ra, "doctor", "--json"], cwd=work, env=env), "doctor --json").stdout)
    checks = {c["check"]: c for c in doc["checks"]}
    assert doc["ok"] is True and checks["package"]["ok"] and checks["upstream_base"]["ok"]
    # "repoatlas <version> installed at <site-packages>": where the running code comes from.
    facts = {"location": Path(checks["package"]["detail"].split(" installed at ", 1)[1]).resolve()}
    if full:
        repo = _orders_repo(base)
        scan = json.loads(_ok(_run([ra, "scan", repo, "--json"], cwd=repo, env=env), "scan").stdout)
        assert scan["graph"]["nodes"] > 20 and scan["snapshot"]["commit_sha"]
        assert (repo / ".repoatlas" / "index" / "graph.json").is_file()
        an = json.loads(_ok(_run([ra, "analyze", QUESTION, "--repo", repo, "--json"], cwd=repo, env=env),
                            "analyze").stdout)
        assert an["claims"] and all(c["evidence"] for c in an["claims"])
        # The installer-type upstream commands stay blocked in an installed build.
        r = _run([ra, "index", "--", "install"], cwd=repo, env=env)
        assert r.returncode == 2 and "repoatlas install" in r.stderr
        facts.update(nodes=scan["graph"]["nodes"], claims=len(an["claims"]))
    assert not any(home.iterdir()), f"wrote into HOME: {sorted(p.name for p in home.iterdir())}"
    return facts


def test_wheel_installs_and_runs_in_fresh_venv(built, tmp_path):
    uv = _uv()
    env = _clean_env()
    venv = tmp_path / "venv"
    _ok(_run([uv, "venv", venv, "--python", _base_python()], cwd=tmp_path, env=env), "uv venv")
    bindir = venv / ("Scripts" if os.name == "nt" else "bin")
    py = _exe(bindir, "python")
    _ok(_run([uv, "pip", "install", "--python", py, built["wheel"]], cwd=tmp_path, env=env), "uv pip install")

    facts = _drive_installed_cli(_exe(bindir, "repoatlas"), tmp_path, full=True)
    assert facts["location"].is_relative_to(venv.resolve()), facts

    # Runs from the venv, not the source tree, and Graphify is not needed (nor present).
    probe = ("import importlib.util, sys, repoatlas; print(repoatlas.__file__); "
             "sys.exit(importlib.util.find_spec('graphify') is not None)")
    r = _run([py, "-c", probe], cwd=tmp_path / "work", env=env)
    assert r.returncode == 0, "graphify is importable in the fresh venv"
    assert Path(r.stdout.strip()).resolve().is_relative_to(venv.resolve()), r.stdout
    listed = _ok(_run([uv, "pip", "list", "--python", py], cwd=tmp_path, env=env), "uv pip list").stdout.lower()
    assert "repoatlas" in listed and "graphifyy" not in listed


def test_uv_tool_install_in_temp_tool_dir(built, tmp_path):
    uv = _uv()
    tools, bins = tmp_path / "uv-tools", tmp_path / "uv-bin"
    env = _clean_env(UV_TOOL_DIR=str(tools), UV_TOOL_BIN_DIR=str(bins))
    _ok(_run([uv, "tool", "install", "--python", _base_python(), built["wheel"]], cwd=tmp_path, env=env),
        "uv tool install")
    ra = _exe(bins, "repoatlas")
    assert not [p for p in bins.iterdir() if p.name.lower().startswith("graphify")]
    facts = _drive_installed_cli(ra, tmp_path, full=True)
    assert facts["location"].is_relative_to(tools.resolve()), facts
    listed = _ok(_run([uv, "tool", "list"], cwd=tmp_path, env=env), "uv tool list").stdout
    assert "repoatlas" in listed and "graphify" not in listed


@pytest.mark.slow
def test_pipx_install_via_uv_tool_run(built, tmp_path):
    uv = _uv()
    base_py = _base_python()
    env = _clean_env(PIPX_HOME=str(tmp_path / "pipx-home"), PIPX_BIN_DIR=str(tmp_path / "pipx-bin"),
                     PIPX_MAN_DIR=str(tmp_path / "pipx-man"), PIP_CACHE_DIR=str(tmp_path / "pip-cache"),
                     PIPX_DEFAULT_PYTHON=base_py)
    probe = _run([uv, "tool", "run", "--python", base_py, "pipx", "--version"], cwd=tmp_path, env=env)
    if probe.returncode != 0:
        pytest.skip(f"pipx not reachable via `uv tool run pipx`: {probe.stderr.strip()[-300:]}")
    _ok(_run([uv, "tool", "run", "--python", base_py, "pipx", "install", built["wheel"]], cwd=tmp_path, env=env),
        "pipx install")
    facts = _drive_installed_cli(_exe(tmp_path / "pipx-bin", "repoatlas"), tmp_path, full=False)
    assert facts["location"].is_relative_to((tmp_path / "pipx-home").resolve()), facts
