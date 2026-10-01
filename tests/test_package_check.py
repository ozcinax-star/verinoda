"""Registry existence and squatting signals (``check --deps --registry``, ``decide ask --registry``).

Every registry answer here comes from a fake transport: no test sends a request. A transport that must not
be called raises, so "nothing was sent" is checked, not assumed.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
import time
from pathlib import Path

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import pytest  # noqa: E402

from verinoda import package_check as pc  # noqa: E402
from verinoda.references.transport import Response, TransportError  # noqa: E402

NOW = 1_790_000_000.0          # 2026-09-21T..Z
DAY = 86400.0


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Fake:
    """Routes URL -> (status, body) or an exception; records every URL asked."""

    def __init__(self, routes: dict | None = None):
        self.routes = dict(routes or {})
        self.asked: list[str] = []

    def get(self, url, *, accept=None):
        self.asked.append(url)
        r = self.routes.get(url)
        if r is None:
            return Response(status=404, body=b'{"message": "Not Found"}', url=url, retrieved_at=_iso(NOW))
        if isinstance(r, Exception):
            raise r
        status, body = r
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        return Response(status=status, body=body, url=url, retrieved_at=_iso(NOW))


class Forbidden:
    def get(self, url, *, accept=None):
        raise AssertionError(f"no request may be sent, but {url} was asked")


PYPI = "https://pypi.org/pypi/{}/json"
SIMPLE = "https://pypi.org/simple/{}/"
NPM = "https://registry.npmjs.org/{}"
NPM_DL = "https://api.npmjs.org/downloads/point/last-week/{}"


def _pypi(name, *, first=NOW - 3000 * DAY, latest="2.0", yanked=False, vulns=(), status="active"):
    rel = {"1.0": [{"upload_time_iso_8601": _iso(first), "yanked": False}],
           latest: [{"upload_time_iso_8601": _iso(first + 100 * DAY), "yanked": yanked,
                     "yanked_reason": "broken build" if yanked else None}]}
    return {PYPI.format(name): (200, {"info": {"name": name, "version": latest}, "releases": rel,
                                      "vulnerabilities": list(vulns)}),
            SIMPLE.format(name): (200, {"name": name, "project-status": {"status": status}})}


def _npm(name, *, created=NOW - 3000 * DAY, latest="1.0.0", deprecated=None, downloads=500000, descr=""):
    v = {"name": name, "version": latest, **({"deprecated": deprecated} if deprecated else {})}
    return {NPM.format(name): (200, {"name": name, "description": descr, "dist-tags": {"latest": latest},
                                     "versions": {latest: v}, "time": {"created": _iso(created)}}),
            NPM_DL.format(name): (200, {"downloads": downloads, "package": name})}


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """The user's own index and Go settings must not change these tests: no environment, no `go env`."""
    monkeypatch.setattr(pc, "os_environ", lambda: {})
    monkeypatch.setattr(pc, "_go_private", lambda env: [(x, "GOPRIVATE") for x in
                                                        str(env.get("GOPRIVATE") or "").split(",") if x])


def _check(tmp_path, pkgs, routes=None, network="on", transport=None, now=NOW, **kw):
    t = transport if transport is not None else Fake(routes)
    res = pc.check_packages(tmp_path, pkgs, network=network, transport=t, now=now,
                            cache_file=tmp_path / "cache.json", **kw)
    return res, t


def _one(res) -> dict:
    assert len(res["packages"]) == 1
    return res["packages"][0]


def _kinds(p) -> set[str]:
    return {s["signal"] for s in p["signals"]}


# -- registry facts ---------------------------------------------------------------------------------------------

def test_missing_pypi_package_flagged_with_url_and_time(tmp_path):
    res, t = _check(tmp_path, [{"ecosystem": "python", "name": "fastjsonschema-validatorx"}])
    p = _one(res)
    assert p["verdict"] == "flagged" and p["exists"] is False
    nf = next(s for s in p["signals"] if s["signal"] == "not_found")
    assert nf["status"] == "observed" and nf["url"] == PYPI.format("fastjsonschema-validatorx")
    assert nf["observed_at"] == _iso(NOW) and "no package named" in nf["claim"]
    assert "do not install" in p["next_step"]
    assert res["summary"]["flagged"] == 1


def test_missing_npm_package_flagged(tmp_path):
    p = _one(_check(tmp_path, [{"ecosystem": "npm", "name": "react-query-hooks-pro-x"}])[0])
    assert p["verdict"] == "flagged" and "not_found" in _kinds(p)


def test_existing_old_popular_package_is_ok(tmp_path):
    res, t = _check(tmp_path, [{"ecosystem": "python", "name": "requests"}], _pypi("requests"))
    p = _one(res)
    assert p["verdict"] == "ok" and p["exists"] is True and p["signals"] == []
    assert p["facts"]["latest"] == "2.0" and p["facts"]["age_days"] == 3000


def test_young_npm_package_with_few_downloads(tmp_path):
    routes = _npm("leftpad-ultra", created=NOW - 5 * DAY, downloads=12)
    p = _one(_check(tmp_path, [{"ecosystem": "npm", "name": "leftpad-ultra"}], routes)[0])
    assert p["verdict"] == "caution"
    assert {"young", "few_downloads"} <= _kinds(p)
    assert all(s["status"] == "observed" for s in p["signals"] if s["signal"] in ("young", "few_downloads"))
    assert p["facts"]["downloads"] == {"count": 12, "period": "last week", "url": NPM_DL.format("leftpad-ultra")}


def test_yanked_latest_and_vulnerabilities_on_pypi(tmp_path):
    routes = _pypi("oldlib", yanked=True, vulns=[{"id": "PYSEC-2026-1"}])
    p = _one(_check(tmp_path, [{"ecosystem": "python", "name": "oldlib"}], routes)[0])
    assert p["verdict"] == "caution" and {"yanked", "known_vulnerabilities"} <= _kinds(p)
    assert "broken build" in next(s for s in p["signals"] if s["signal"] == "yanked")["claim"]


def test_pypi_quarantine_from_the_simple_api(tmp_path):
    routes = {PYPI.format("evilpkg"): (404, b"{}"),
              SIMPLE.format("evilpkg"): (200, {"name": "evilpkg", "project-status": {"status": "quarantined"}})}
    p = _one(_check(tmp_path, [{"ecosystem": "python", "name": "evilpkg"}], routes)[0])
    assert p["verdict"] == "flagged" and _kinds(p) == {"quarantined"} and p["exists"] is True


def test_npm_deprecated_and_security_holding(tmp_path):
    routes = {**_npm("request-old", deprecated="use got instead"),
              **_npm("crossenv-x", latest="0.0.1-security", descr="security holding package")}
    res, t = _check(tmp_path, [{"ecosystem": "npm", "name": "request-old"},
                               {"ecosystem": "npm", "name": "crossenv-x"}], routes)
    dep, hold = res["packages"]
    assert dep["verdict"] == "caution" and "deprecated" in _kinds(dep)
    assert hold["verdict"] == "flagged" and "security_holding" in _kinds(hold)
    assert "remove" in hold["next_step"]


def test_crates_go_and_maven(tmp_path):
    routes = {"https://crates.io/api/v1/crates/serde": (200, {"crate": {
                  "name": "serde", "created_at": _iso(NOW - 4000 * DAY), "recent_downloads": 300_000_000,
                  "max_stable_version": "1.0.229", "num_versions": 316}, "versions": [{"num": "1.0.229",
                                                                                         "yanked": False}]}),
              "https://proxy.golang.org/github.com/!burnt!sushi/toml/@latest": (200, {"Version": "v1.4.0",
                                                                                      "Time": _iso(NOW - DAY)}),
              "https://repo1.maven.org/maven2/com/google/guava/guava/maven-metadata.xml": (
                  200, "<metadata><versioning><release>33.0</release><versions><version>33.0</version></versions>"
                       "</versioning></metadata>")}
    res, t = _check(tmp_path, [{"ecosystem": "cargo", "name": "serde"},
                               {"ecosystem": "go", "name": "github.com/BurntSushi/toml"},
                               {"ecosystem": "maven", "name": "com.google.guava:guava"},
                               {"ecosystem": "maven", "name": "org.nowhere:nothing"}], routes)
    cr, go, mv, absent = res["packages"]
    assert cr["verdict"] == "ok" and cr["facts"]["downloads"]["period"] == "last 90 days"
    assert go["verdict"] == "ok" and go["facts"]["latest"] == "v1.4.0"
    assert mv["verdict"] == "ok" and mv["facts"]["latest"] == "33.0"
    assert absent["verdict"] == "flagged" and "another repository" in absent["signals"][0]["claim"]


# -- local name heuristics ---------------------------------------------------------------------------------------

def test_typo_of_a_popular_name():
    sig = pc.name_signals("pypi", "reqeusts")
    assert sig and sig[0]["signal"] == "typo_of" and sig[0]["like"] == "requests"
    assert sig[0]["status"] == "strong_inference"
    assert pc.name_signals("pypi", "requests") == []
    assert pc.name_signals("npm", "lodahs")[0]["like"] == "lodash"


def test_separator_and_hallucination_style():
    assert pc.name_signals("pypi", "pythondateutil")[0]["signal"] == "separator_confusable"
    assert pc.name_signals("pypi", "python_dateutil") == []   # PEP 503: the same project
    assert pc.name_signals("npm", "lodash_es")[0]["signal"] == "separator_confusable"
    h = pc.name_signals("pypi", "numpy-utils")
    assert h and h[0]["signal"] == "hallucination_style" and h[0]["status"] == "weak_inference"
    assert pc.name_signals("pypi", "zz") == []


def test_typo_signal_makes_caution_even_when_the_package_exists(tmp_path):
    routes = _pypi("reqeusts")
    p = _one(_check(tmp_path, [{"ecosystem": "python", "name": "reqeusts"}], routes)[0])
    assert p["exists"] is True and p["verdict"] == "caution" and "typo_of" in _kinds(p)


def test_standard_library_imports_are_not_asked(tmp_path):
    res, t = _check(tmp_path, [{"ecosystem": "python", "name": "json", "imported": True},
                               {"ecosystem": "npm", "name": "node:fs", "imported": True}],
                    transport=Forbidden())
    assert [p["verdict"] for p in res["packages"]] == ["skipped", "skipped"]


# -- network modes and the cache ------------------------------------------------------------------------------------

def test_network_off_sends_nothing_and_is_unknown(tmp_path):
    res, t = _check(tmp_path, [{"ecosystem": "python", "name": "somepkg"}], network="off", transport=Forbidden())
    p = _one(res)
    assert p["verdict"] == "unknown" and p["exists"] is None
    assert "--network on" in p["next_step"] and "network is off" in p["unknown"]


def test_network_off_still_gives_name_signals(tmp_path):
    p = _one(_check(tmp_path, [{"ecosystem": "pypi", "name": "reqeusts"}], network="off", transport=Forbidden())[0])
    assert p["exists"] is None and p["verdict"] == "caution" and "typo_of" in _kinds(p)


def test_cache_reused_then_expired(tmp_path):
    pkgs = [{"ecosystem": "npm", "name": "nonexistent-thing-q"}]
    res, t = _check(tmp_path, pkgs, {}, network="cache")
    assert len(t.asked) == 1 and _one(res)["from_cache"] is False
    stored = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
    assert stored["schema"] == pc.SCHEMA and "npm:nonexistent-thing-q" in stored["entries"]
    again = _one(_check(tmp_path, pkgs, network="cache", transport=Forbidden(), now=NOW + 3600)[0])
    assert again["from_cache"] is True and again["verdict"] == "flagged"
    assert again["observed_at"] == _iso(NOW)                 # the time it was observed, not the time reused
    later, t2 = _check(tmp_path, pkgs, {}, network="cache", now=NOW + 25 * 3600)
    assert len(t2.asked) == 1 and _one(later)["from_cache"] is False
    off = _one(_check(tmp_path, pkgs, network="off", transport=Forbidden())[0])
    assert off["verdict"] == "unknown" and "cached answer" in off["unknown"]


def test_network_on_always_asks_and_falls_back_to_the_cache(tmp_path):
    pkgs = [{"ecosystem": "python", "name": "requests"}]
    _check(tmp_path, pkgs, _pypi("requests"))
    res, t = _check(tmp_path, pkgs, _pypi("requests"), now=NOW + 60)
    assert t.asked and _one(res)["from_cache"] is False
    down = Fake({PYPI.format("requests"): TransportError("TimeoutError: timed out")})
    p = _one(_check(tmp_path, pkgs, transport=down)[0])
    assert p["from_cache"] is True and p["verdict"] == "ok" and "could not be read" in p["unknown"]


def test_timeout_is_unknown_and_not_cached(tmp_path):
    t = Fake({PYPI.format("somepkg"): TransportError("TimeoutError: timed out")})
    p = _one(_check(tmp_path, [{"ecosystem": "python", "name": "somepkg"}], transport=t)[0])
    assert p["verdict"] == "unknown" and "unreachable" in p["unknown"]
    assert not (tmp_path / "cache.json").exists()


@pytest.mark.parametrize("body,status", [(b"<html>not json", 200), (b'["a list"]', 200), (b"{}", 503),
                                         (b'{"info": "text"}', 200)])
def test_malformed_or_failing_answers_are_unknown(tmp_path, body, status):
    t = Fake({PYPI.format("somepkg"): (status, body), SIMPLE.format("somepkg"): (200, b"{}")})
    p = _one(_check(tmp_path, [{"ecosystem": "python", "name": "somepkg"}], transport=t)[0])
    assert p["verdict"] == "unknown" and p["exists"] is None
    assert not (tmp_path / "cache.json").exists()


def test_malformed_npm_and_maven(tmp_path):
    t = Fake({NPM.format("x-pkg"): (200, b'{"versions": "nope"}'),
              "https://repo1.maven.org/maven2/a/b/maven-metadata.xml": (200, b"<metadata")})
    res, _ = _check(tmp_path, [{"ecosystem": "npm", "name": "x-pkg"}, {"ecosystem": "maven", "name": "a:b"}],
                    transport=t)
    assert [p["verdict"] for p in res["packages"]] == ["unknown", "unknown"]


def test_json_404_without_a_readable_simple_api_is_not_called_missing(tmp_path):
    t = Fake({PYPI.format("somepkg"): (404, b"{}"), SIMPLE.format("somepkg"): TransportError("reset")})
    p = _one(_check(tmp_path, [{"ecosystem": "python", "name": "somepkg"}], transport=t)[0])
    assert p["verdict"] == "unknown" and p["exists"] is None


def test_bad_network_mode(tmp_path):
    with pytest.raises(pc.PackageCheckError):
        pc.check_packages(tmp_path, [], network="always")


def test_unsupported_ecosystem_is_unknown(tmp_path):
    p = _one(_check(tmp_path, [{"ecosystem": "gradle-plugin", "name": "org.x"}], transport=Forbidden())[0])
    assert p["verdict"] == "unknown" and "no registry lookup" in p["unknown"]


# -- check --deps --registry and decide ask --registry -----------------------------------------------------------

def _git(root: Path, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


@pytest.fixture()
def gitproj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "requirements.txt").write_text("requests==2.31\n", encoding="utf-8")
    (root / "app.py").write_text("import requests\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    (root / "requirements.txt").write_text("requests==2.32\nhuggingface-cli-tools==1.0\n", encoding="utf-8")
    (root / "app.py").write_text(textwrap.dedent("""\
        import requests
        import huggingface_cli_tools
        import made_up_helper_lib
        """), encoding="utf-8")
    return root


def test_added_dependencies_skip_version_changes(gitproj):
    from verinoda.guards import declared_dependencies

    items = declared_dependencies(gitproj)["items"]
    added = pc.added_dependencies(gitproj, items)
    assert [i["name"] for i in added] == ["huggingface-cli-tools"]
    # a name inside an old one is still new: "request" next to "requests"
    (gitproj / "requirements.txt").write_text("requests==2.31\nrequest==1.0\n", encoding="utf-8")
    added = pc.added_dependencies(gitproj, declared_dependencies(gitproj)["items"])
    assert [i["name"] for i in added] == ["request"]
    # a manifest git does not know yet adds all of its declarations
    (gitproj / "package.json").write_text('{"dependencies": {"lodash": "^4", "lodahs": "^1"}}', encoding="utf-8")
    added = pc.added_dependencies(gitproj, declared_dependencies(gitproj)["items"])
    assert {i["name"] for i in added} == {"request", "lodash", "lodahs"}
    with pytest.raises(pc.PackageCheckError):
        pc.added_dependencies(gitproj, [], rev="no-such-rev")


def test_check_deps_registry_flags_names_the_registry_lacks(gitproj, monkeypatch, capsys):
    from verinoda import cli
    from verinoda.references import transport as tr

    fake = Fake(_pypi("requests"))
    monkeypatch.setattr(tr, "LiveTransport", lambda **kw: fake)
    rc = cli.main(["check", "--deps", "--registry", "--network", "on", "--repo", str(gitproj), "--env", "none",
                   "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 3
    flagged = {f["package"]: f for f in data["findings"] if f["finding"] == "not_in_registry"}
    assert set(flagged) == {"huggingface-cli-tools", "made-up-helper-lib"}
    f = flagged["huggingface-cli-tools"]
    assert f["status"] == "observed"
    assert {e["role"] for e in f["evidence"]} == {"declaration", "registry"}
    assert any(e["locator"] == "requirements.txt:2" for e in f["evidence"])
    assert data["registry"]["which"] == "new" and data["summary"]["not_in_registry"] == 2
    assert not any("pypi.org/pypi/requests/" in u for u in fake.asked)   # a version change is not asked about
    assert all(u.startswith(("https://pypi.org/",)) for u in fake.asked)
    rc = cli.main(["check", "--deps", "--registry", "--repo", str(gitproj), "--env", "none"])
    out = capsys.readouterr().out
    assert "registry check (network off)" in out and "--network on" in out


def test_check_deps_registry_all_and_argument_errors(gitproj, monkeypatch, capsys):
    from verinoda import cli
    from verinoda.references import transport as tr

    fake = Fake(_pypi("requests"))
    monkeypatch.setattr(tr, "LiveTransport", lambda **kw: fake)
    cli.main(["check", "--deps", "--registry", "all", "--network", "on", "--repo", str(gitproj), "--env", "none",
              "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {p["name"] for p in data["registry"]["packages"]} >= {"requests", "huggingface-cli-tools"}
    with pytest.raises(SystemExit):
        cli.main(["check", "--registry", "--repo", str(gitproj)])
    with pytest.raises(SystemExit):
        cli.main(["check", "--deps", "--diff", "HEAD", "--repo", str(gitproj)])


def test_decide_ask_registry(gitproj, monkeypatch, capsys):
    from verinoda import cli
    from verinoda.references import transport as tr

    monkeypatch.setattr(tr, "LiveTransport", lambda **kw: Forbidden())
    cli.main(["decide", "ask", "app.py", "reqeusts", "--registry", "--repo", str(gitproj), "--json"])
    data = json.loads(capsys.readouterr().out)
    p = data["registry"]["packages"][0]
    assert p["name"] == "reqeusts" and p["verdict"] == "caution" and p["exists"] is None
    monkeypatch.setattr(tr, "LiveTransport", lambda **kw: Fake({}))
    cli.main(["decide", "ask", "web/x.ts", "@acme/not-real/sub", "--registry", "--network", "on",
              "--repo", str(gitproj), "--json"])
    data = json.loads(capsys.readouterr().out)
    p = data["registry"]["packages"][0]
    assert p["name"] == "@acme/not-real" and p["verdict"] == "flagged"


# -- review round: sources that are no registry, private registries, names as declared ------------------------

def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


SOURCES = {
    "package.json": json.dumps({"dependencies": {
        "mylib": "workspace:*", "loc": "file:../loc", "lnk": "link:../l", "prt": "portal:../p",
        "gitdep": "git+https://example.com/y.git", "ghdep": "github:a/b", "short": "a/b#v1",
        "tar": "https://example.com/t.tgz", "myalias": "npm:left-pad@^1", "JSONStream": "^1"}}),
    "Cargo.toml": """\
        [package]
        name = "mylogger"

        [dependencies]
        log = "0.4"
        mypath = { path = "../p" }
        mygit = { git = "https://example.com/y" }
        ws = { workspace = true }
        json = { package = "serde_json", version = "1" }
        corp = { version = "1", registry = "corp" }
        """,
    "requirements.txt": "direct @ https://example.com/direct.whl\n",
    "go.mod": """\
        module example.com/m

        require (
            github.com/a/local v1.0.0
            github.com/a/old v1.0.0
        )

        replace github.com/a/local => ../local
        replace github.com/a/old => github.com/b/new v1.2.0
        """,
}


def _crate(name):
    return (200, {"crate": {"name": name, "created_at": _iso(NOW - 3000 * DAY)}})


def test_local_vcs_url_sources_are_never_sent_and_aliases_are_read(tmp_path):
    from verinoda.guards import declared_dependencies

    root = _write(tmp_path / "p", SOURCES)
    pkgs = pc.from_declarations(declared_dependencies(root)["items"], "declared", root)
    t = Fake({**_npm("left-pad"), **_npm("JSONStream"),
              "https://crates.io/api/v1/crates/serde_json": _crate("serde_json"),
              "https://crates.io/api/v1/crates/log": _crate("log"),
              "https://proxy.golang.org/github.com/b/new/@latest": (200, {"Version": "v1.2.0"})})
    res = pc.check_packages(root, pkgs, network="on", transport=t, now=NOW, cache_file=tmp_path / "c.json",
                            env={})
    by = {p.get("declared_as") or p["name"]: p for p in res["packages"]}
    for local in ("mylib", "loc", "lnk", "prt", "gitdep", "ghdep", "short", "tar", "mypath", "mygit", "ws",
                  "direct", "github.com/a/local"):
        assert by[local]["verdict"] == "skipped", local
        assert not any(local in u for u in t.asked), local
    assert by["corp"]["verdict"] == "unknown" and "not sent" in by["corp"]["unknown"]
    assert by["myalias"]["name"] == "left-pad" and by["myalias"]["verdict"] == "ok"
    assert by["json"]["name"] == "serde_json" and by["json"]["verdict"] == "ok"
    assert by["github.com/a/old"]["name"] == "github.com/b/new"
    assert NPM.format("JSONStream") in t.asked and NPM.format("jsonstream") not in t.asked
    assert by["log"]["verdict"] == "ok"
    assert not any("crates.io/api/v1/crates/corp" in u for u in t.asked)


def test_private_registries_keep_names_home(tmp_path):
    root = _write(tmp_path / "p", {".npmrc": "@corp:registry=https://npm.corp.example/\n"})
    res, t = _check(tmp_path, [{"ecosystem": "npm", "name": "@corp/billing"},
                               {"ecosystem": "npm", "name": "@other/thing"}],
                    private=pc.private_sources(root, {}))
    corp, other = res["packages"]
    assert corp["verdict"] == "unknown" and ".npmrc:1" in corp["unknown"]
    assert not any("corp" in u for u in t.asked) and other["verdict"] == "flagged"


@pytest.mark.parametrize("files,env", [
    ({"requirements.txt": "--extra-index-url https://pypi.corp.example/simple\nrequests\n"}, {}),
    ({"requirements.txt": "-i https://pypi.corp.example/simple\n"}, {}),
    ({"pip.conf": "[global]\nindex-url = https://pypi.corp.example/simple\n"}, {}),
    ({"pyproject.toml": '[[tool.uv.index]]\nname = "corp"\nurl = "https://pypi.corp.example/simple"\n'}, {}),
    ({"pyproject.toml": '[[tool.poetry.source]]\nname = "corp"\nurl = "https://pypi.corp.example/simple"\n'}, {}),
    ({}, {"PIP_EXTRA_INDEX_URL": "https://pypi.corp.example/simple"}),
])
def test_a_private_python_index_means_no_name_is_sent_to_pypi(tmp_path, files, env):
    root = _write(tmp_path / "p", files)
    res, t = _check(tmp_path, [{"ecosystem": "python", "name": "corp-billing"}], transport=Forbidden(),
                    private=pc.private_sources(root, env))
    p = _one(res)
    assert p["verdict"] == "unknown" and p["unknown"].startswith("not sent") and "leak" in p["unknown"]


def test_cargo_replaced_and_go_private(tmp_path):
    root = _write(tmp_path / "p", {".cargo/config.toml": '[source.crates-io]\nreplace-with = "corp"\n'
                                                          '[source.corp]\nregistry = "https://x"\n'})
    res, _ = _check(tmp_path, [{"ecosystem": "cargo", "name": "corp-core"}], transport=Forbidden(),
                    private=pc.private_sources(root, {}))
    assert _one(res)["verdict"] == "unknown"
    res, t = _check(tmp_path, [{"ecosystem": "go", "name": "git.corp.example/team/lib"},
                               {"ecosystem": "go", "name": "github.com/corp/x/sub"}], {},
                    env={"GOPRIVATE": "git.corp.example,github.com/corp/*"})
    assert [p["verdict"] for p in res["packages"]] == ["unknown", "unknown"] and t.asked == []


def test_go_and_rust_built_ins_and_package_paths():
    assert pc.skip_reason("go", "fmt") and pc.skip_reason("go", "net/http")
    for t in ("std::collections::HashMap", "core::fmt", "alloc", "proc_macro", "crate::x", "self::y", "super::z"):
        p = pc.from_import_name("src/lib.rs", t)
        assert pc.skip_reason("cargo", p["name"], imported=True), t
    assert pc.from_import_name("main.go", "github.com/stretchr/testify/assert")["name"] == \
        "github.com/stretchr/testify"
    assert pc.go_module_of("example.com/x/y/z", ["example.com/x/y"]) == "example.com/x/y"
    assert "hold" in pc.from_import_name("main.go", "example.com/x/y")
    g = pc.from_import_name("x.py", "google.cloud.storage")
    assert "hold" in g and "namespace" in g["hold"]
    assert pc.from_import_name("x.py", "google.protobuf")["name"] == "protobuf"


def test_decide_ask_registry_skips_go_and_rust_built_ins(gitproj, monkeypatch, capsys):
    from verinoda import cli
    from verinoda.references import transport as tr

    monkeypatch.setattr(tr, "LiveTransport", lambda **kw: Forbidden())
    for source, target in (("main.go", "fmt"), ("main.go", "net/http"), ("src/lib.rs", "std::io")):
        cli.main(["decide", "ask", source, target, "--registry", "--network", "on", "--repo", str(gitproj),
                  "--json"])
        assert json.loads(capsys.readouterr().out)["registry"]["packages"][0]["verdict"] == "skipped", target


def test_scoped_npm_names_are_not_separator_lookalikes(tmp_path):
    for n in ("@types/express", "@sentry/node", "@types/node", "@babel/core", "@types/react"):
        assert pc.name_signals("npm", n) == [], n
    res, _ = _check(tmp_path, [{"ecosystem": "npm", "name": "@types/express"}], network="off",
                    transport=Forbidden(), private={})
    assert _one(res)["verdict"] == "unknown"


def test_maven_and_npm_names_keep_their_case(tmp_path):
    from verinoda.guards import declared_dependencies

    root = _write(tmp_path / "p", {"pom.xml": """\
        <project><dependencies><dependency><groupId>org.antlr</groupId><artifactId>ST4</artifactId>
        <version>4.3</version></dependency></dependencies></project>
        """})
    pkgs = pc.from_declarations(declared_dependencies(root)["items"], "declared", root)
    assert [p["name"] for p in pkgs] == ["org.antlr:ST4"]
    url = "https://repo1.maven.org/maven2/org/antlr/ST4/maven-metadata.xml"
    res, t = _check(tmp_path, pkgs, {url: (200, "<metadata><versioning><release>4.3</release></versioning>"
                                                "</metadata>")}, private={})
    assert t.asked == [url] and _one(res)["verdict"] == "ok"
    assert pc.from_import_name("A.java", "org.antlr:ST4")["name"] == "org.antlr:ST4"


def test_pypi_project_without_installable_release_exists(tmp_path):
    routes = {PYPI.format("emptyproj"): (404, b"{}"),
              SIMPLE.format("emptyproj"): (200, {"name": "emptyproj", "files": []})}
    p = _one(_check(tmp_path, [{"ecosystem": "python", "name": "emptyproj"}], routes, private={})[0])
    assert p["exists"] is True and p["verdict"] == "caution" and "no_release" in _kinds(p)
    assert "not_found" not in _kinds(p)


@pytest.mark.parametrize("entry", [
    {"epoch": NOW},                                                           # no answer
    {"epoch": "yesterday", "answer": {"registry": "npm", "name": "x", "url": "u", "exists": True}},
    {"epoch": NOW, "answer": ["a", "list"]},
    {"epoch": NOW, "answer": {"name": "x", "url": "u", "exists": True}},     # no registry
    {"epoch": NOW, "answer": {"registry": "npm", "name": "x", "url": "u", "exists": True, "facts": "text"}},
    "not a dict",
])
def test_damaged_cache_entries_are_misses(tmp_path, entry):
    (tmp_path / "cache.json").write_text(json.dumps({"schema": pc.SCHEMA, "entries": {"npm:somepkg": entry}}),
                                         encoding="utf-8")
    pkgs = [{"ecosystem": "npm", "name": "somepkg"}]
    off = _one(_check(tmp_path, pkgs, network="off", transport=Forbidden(), private={})[0])
    assert off["verdict"] == "unknown" and "cached answer" not in off["unknown"]
    res, t = _check(tmp_path, pkgs, {}, network="cache", private={})
    assert t.asked == [NPM.format("somepkg")] and _one(res)["verdict"] == "flagged"


def test_cache_writes_merge_with_other_runs_and_replace_atomically(tmp_path):
    path = tmp_path / "cache.json"
    good = {"epoch": NOW, "observed_at": _iso(NOW),
            "answer": {"registry": "npm", "name": "a", "url": "u", "exists": True, "facts": {}, "observed": []}}
    pc._save_cache(path, {"npm:a": good})
    # this run read nothing; the file another run wrote meanwhile keeps its entry
    _check(tmp_path, [{"ecosystem": "npm", "name": "b-missing"}], {}, network="on", private={})
    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    assert {"npm:a", "npm:b-missing"} <= set(entries)
    assert not list(tmp_path.glob(".package-check.*.tmp"))
    path.write_text('{"schema": "verinoda.package_check/1", "entries": {"npm:a"', encoding="utf-8")  # truncated
    _check(tmp_path, [{"ecosystem": "npm", "name": "c-missing"}], {}, network="cache", private={})
    assert set(json.loads(path.read_text(encoding="utf-8"))["entries"]) == {"npm:c-missing"}


def test_save_merges_entries_written_after_this_run_read(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    real_load = pc._load_cache
    calls = []

    def load(p):
        calls.append(p)
        if len(calls) == 2:   # the reload just before saving: another run has written "npm:other" meanwhile
            return {"npm:other": {"epoch": NOW, "answer": {"registry": "npm", "name": "other", "url": "u",
                                                           "exists": True}}}
        return real_load(p)

    monkeypatch.setattr(pc, "_load_cache", load)
    _check(tmp_path, [{"ecosystem": "npm", "name": "mine-missing"}], {}, private={})
    assert set(json.loads(path.read_text(encoding="utf-8"))["entries"]) == {"npm:other", "npm:mine-missing"}


def test_observed_findings_and_name_signals_are_separate(gitproj, monkeypatch, capsys):
    from verinoda import cli
    from verinoda.references import transport as tr

    (gitproj / "requirements.txt").write_text("requests==2.31\nreqeusts==1.0\n", encoding="utf-8")
    monkeypatch.setattr(tr, "LiveTransport", lambda **kw: Fake({}))
    rc = cli.main(["check", "--deps", "--registry", "--network", "on", "--repo", str(gitproj), "--env", "none",
                   "--json"])
    data = json.loads(capsys.readouterr().out)
    mine = [f for f in data["findings"] if f["package"] == "reqeusts" and f["finding"] in pc.REGISTRY_FINDINGS]
    assert rc == 3 and {f["finding"]: f["status"] for f in mine} == {"not_in_registry": "observed",
                                                                     "lookalike_name": "strong_inference"}
    nf = next(f for f in mine if f["finding"] == "not_in_registry")
    assert "edit" not in nf["claim"] and "no package named" in nf["claim"]


def test_widely_used_near_name_is_only_weak(tmp_path):
    p = _one(_check(tmp_path, [{"ecosystem": "npm", "name": "lodahs"}], _npm("lodahs", downloads=2_000_000),
                    private={})[0])
    assert p["verdict"] == "ok"
    assert next(s for s in p["signals"] if s["signal"] == "typo_of")["status"] == "weak_inference"


def test_bad_revision_is_an_error_not_zero_flagged(gitproj, capsys):
    from verinoda import cli

    rc = cli.main(["check", "--deps", "--registry", "--diff", "no-such-rev", "--repo", str(gitproj), "--env",
                   "none", "--json"])
    assert rc == 2 and json.loads(capsys.readouterr().out)["status"] == "error"
    rc = cli.main(["check", "--deps", "--registry", "all", "--diff", "HEAD", "--repo", str(gitproj), "--env",
                   "none"])
    assert rc == 2
    capsys.readouterr()


def test_not_a_git_work_tree_checks_the_imports_and_says_so(tmp_path, monkeypatch):
    from verinoda import depcheck

    def no_git(repo, rev):
        raise pc.NotGitError("no git")

    root = _write(tmp_path / "nogit", {"requirements.txt": "requests\n", "app.py": "import requests\n"})
    monkeypatch.setattr(pc, "resolve_rev", no_git)
    res = pc.add_to_deps(root, depcheck.check_deps(root, env="none"), network="off", private={})
    assert any("not a git work tree" in x for x in res["registry"]["limits"])
    assert not any("--stdin" in x for x in res["registry"]["limits"])


def test_new_reads_declared_names_not_text_and_cargo_lines(tmp_path):
    from verinoda.guards import declared_dependencies

    root = tmp_path / "r"
    root.mkdir()
    head = '[package]\nname = "mylogger"\n# serde is planned\n\n[dependencies]\nserde_json = "1"\n'
    (root / "Cargo.toml").write_text(head, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    (root / "Cargo.toml").write_text(head + 'log = "0.4"\nserde = "1"\n', encoding="utf-8")
    items = declared_dependencies(root)["items"]
    added = {i["name"]: i["line"] for i in pc.added_dependencies(root, items)}
    assert added == {"log": 7, "serde": 8}


def test_poetry_key_found_inside_its_table():
    from verinoda.research import _key_line

    lines = ['[tool.poetry]', 'name = "requests-helper"', '', '[tool.poetry.dependencies]', 'python = "^3.11"',
             'requests = "^2"']
    assert _key_line(lines, ("tool.poetry.dependencies",), "requests") == 6


def test_maven_html_answer_is_not_an_artifact(tmp_path):
    url = "https://repo1.maven.org/maven2/a/b/maven-metadata.xml"
    res, _ = _check(tmp_path, [{"ecosystem": "maven", "name": "a:b"}],
                    {url: (200, "<html><body>portal</body></html>")}, private={})
    assert _one(res)["verdict"] == "unknown" and not (tmp_path / "cache.json").exists()


def test_cargo_separators_and_legitimate_near_names():
    assert pc.normalize("cargo", "serde_json") == pc.normalize("cargo", "serde-json")
    for reg, n in (("cargo", "proc_macro2"), ("cargo", "serde-json"), ("pypi", "boto"), ("pypi", "jinja"),
                   ("pypi", "cattrs"), ("pypi", "scapy"), ("npm", "preact"), ("npm", "nest"), ("cargo", "sha1"),
                   ("cargo", "libm")):
        assert pc.name_signals(reg, n) == [], n
