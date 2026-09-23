"""Offline-first transport, registry parsers, PEP 740 provenance (no network: cassettes and fakes only)."""

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import io  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402
import urllib.error  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda.references import provenance  # noqa: E402
from verinoda.references import registries as reg  # noqa: E402
from verinoda.references import transport as tp  # noqa: E402

CASSETTES = Path(__file__).parent / "fixtures" / "references" / "cassettes"


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def cassette(*names):
    return tp.CassetteTransport(*[CASSETTES / n for n in names])


def test_cassette_replay_and_loud_miss():
    t = cassette("pypi_requests")
    r = t.get("https://pypi.org/pypi/requests/json")
    assert r.ok and r.from_cache and r.json()["info"]["name"] == "requests"
    with pytest.raises(tp.CassetteMiss):
        t.get("https://pypi.org/pypi/numpy/json")
    assert t.misses == ["https://pypi.org/pypi/numpy/json"] and t.cache_hits == 1


def test_recorded_cassettes_carry_no_credentials():
    for p in CASSETTES.rglob("*.json"):
        e = json.loads(p.read_text(encoding="utf-8"))
        assert e["schema"] == tp.SCHEMA and e["key"] == tp.cache_key("GET", e["request"]["url"],
                                                                      e["request"]["headers"].get("Accept"))
        assert set(e["request"]["headers"]) <= {"Accept", "User-Agent"}
        assert e["request"]["headers"].get("User-Agent") == "<redacted>"
        assert not {k.lower() for k in e["response"]["headers"]} & {"set-cookie", "authorization", "cookie"}
        assert "token=" not in e["request"]["url"]


def test_clean_url_drops_credential_parameters():
    u = tp.clean_url("https://api.example.com/x?access_token=s3cret&page=2&private_token=t")
    assert u == "https://api.example.com/x?page=2"
    assert tp.cache_key("GET", "https://a/x?token=1", None) == tp.cache_key("GET", "https://a/x", None)


class _FakeResp(io.BytesIO):
    def __init__(self, body, status=200, headers=None, url="https://x"):
        super().__init__(body)
        self.status, self.headers, self._url = status, _H(headers or {}), url

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _H(dict):
    def items(self):
        return list(super().items())


def _fake_opener(monkeypatch, responses):
    from verinoda.project_index import security as sec

    calls = []

    class Opener:
        def open(self, req, timeout=None):
            calls.append(req.full_url)
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    monkeypatch.setattr(sec, "validate_url", lambda u: u)
    monkeypatch.setattr(sec, "_build_opener", lambda: Opener())
    return calls


def test_cache_modes(monkeypatch, tmp_path):
    calls = _fake_opener(monkeypatch, [_FakeResp(b'{"a": 1}', headers={"Content-Type": "application/json",
                                                                         "Set-Cookie": "sid=1"})])
    live = tp.LiveTransport(intervals={"pypi.org": 0.0})
    cache = tp.CacheTransport(tmp_path / "http-cache", mode="cache", inner=live)
    first = cache.get("https://pypi.org/pypi/x/json")
    again = cache.get("https://pypi.org/pypi/x/json")
    assert first.json() == {"a": 1} and not first.from_cache and again.from_cache and len(calls) == 1
    stored = json.loads(next((tmp_path / "http-cache").glob("*.json")).read_text(encoding="utf-8"))
    assert "set-cookie" not in stored["response"]["headers"]
    off = tp.CacheTransport(tmp_path / "http-cache", mode="off")
    assert off.get("https://pypi.org/pypi/x/json").from_cache
    with pytest.raises(tp.Offline):
        off.get("https://pypi.org/pypi/y/json")
    # "on" revalidates, and falls back to the cache when the network fails
    _fake_opener(monkeypatch, [urllib.error.URLError("down")])
    on = tp.CacheTransport(tmp_path / "http-cache", mode="on", inner=tp.LiveTransport(intervals={"pypi.org": 0.0}))
    assert on.get("https://pypi.org/pypi/x/json").from_cache


def test_rate_limit_headers_stop_further_calls(monkeypatch, tmp_path):
    body = b'{"message": "API rate limit exceeded"}'
    err = urllib.error.HTTPError("https://api.github.com/x", 403, "Forbidden",
                                 {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4102444800"}, io.BytesIO(body))
    calls = _fake_opener(monkeypatch, [err])
    live = tp.LiveTransport(intervals={"api.github.com": 0.0}, state_path=tmp_path / "hosts.json")
    r = live.get("https://api.github.com/x")
    assert r.status == 403
    assert reg._status_fail(r, "GitHub")["reason"] == "rate_limited"
    with pytest.raises(tp.RateLimited):
        live.get("https://api.github.com/y")
    assert len(calls) == 1  # the second request never left
    # the block survives the process (persisted with the reset time)
    again = tp.LiveTransport(intervals={"api.github.com": 0.0}, state_path=tmp_path / "hosts.json")
    assert reg.github_issue(again, "o", "r", 1)["reason"] == "rate_limited"


def test_registry_parsers_on_recorded_responses():
    t = cassette("pypi_requests", "pypi_missing", "arxiv_1706", "arxiv_1706_oai", "python_docs_3",
                 "rtd_requests_v2310", "rtd_requests_stable", "github_issue", "go_net_info", "npm_react")
    p = reg.pypi_project(t, "requests")
    assert p["ok"] and {"2.30.0", "2.31.0", "2.32.0"} <= set(p["versions"])
    assert p["releases"]["2.32.0"]["yanked"] and not p["releases"]["2.31.0"]["yanked"]
    assert reg.source_repo_from_urls(p["project_urls"]) == "https://github.com/psf/requests"
    assert reg.pypi_project(t, "this-package-does-not-exist-repoatlas")["reason"] == "not_found"
    a = reg.arxiv_latest(t, "1706.03762")
    assert a["ok"] and a["latest"] >= 7 and "Attention" in a["title"]
    vs = reg.arxiv_versions(t, "1706.03762")
    assert vs["versions"][0] == {"version": 1, "date": "2017-06-12"} and len(vs["versions"]) >= 7
    d = reg.python_docs_release(t, "https://docs.python.org/3/library/asyncio-task.html")
    assert d["ok"] and d["release"].startswith("3.")
    v = reg.rtd_version(t, "requests", "v2.31.0")
    assert v["ok"] and v["built"] is False and v["identifier"] == "147c8511ddbfa5e8f71bbf5c18ede0c4ceb3bba4"
    assert reg.rtd_version(t, "requests", "stable")["type"] == "tag"
    i = reg.github_issue(t, "psf", "requests", 6000)
    assert i["ok"] and i["state"] == "closed" and not i["is_pull_request"]
    g = reg.go_info(t, "golang.org/x/net", "v0.20.0")
    assert g["origin"]["Ref"] == "refs/tags/v0.20.0" and len(g["origin"]["Hash"]) == 40
    n = reg.npm_package(t, "react")
    assert n["ok"] and "18.2.0" in n["versions"]


def test_offline_registry_lookups_say_offline(tmp_path):
    off = tp.CacheTransport(tmp_path / "empty", mode="off")
    assert reg.pypi_project(off, "requests")["reason"] == "offline"
    assert reg.arxiv_latest(off, "1706.03762")["reason"] == "offline"


def test_pep740_provenance_names_the_source_commit():
    t = cassette("pypi_provenance")
    got = provenance.pypi_provenance(t, "requests", "2.34.2", "requests-2.34.2.tar.gz")
    assert got["ok"]
    att = got["attestations"][0]
    assert att["repository"] == "https://github.com/psf/requests"
    assert att["commit"] == "6e83187b8feb273ed4c6cdab5efd8d54901dfab3" and att["ref"] == "refs/tags/v2.34.2"
    assert att["signature_checked"] is False and att["strength"] == "attested"
    assert att["subject"][0]["name"] == "requests-2.34.2.tar.gz"
