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


def _check(tmp_path, pkgs, routes=None, network="on", transport=None, now=NOW):
    t = transport if transport is not None else Fake(routes)
    res = pc.check_packages(tmp_path, pkgs, network=network, transport=t, now=now,
                            cache_file=tmp_path / "cache.json")
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
