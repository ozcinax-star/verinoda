"""Record HTTP cassettes for the reference-resolver tests (manual, needs the network).

Usage::

    python tools/record_reference_cassettes.py [--out tests/fixtures/references/cassettes] [--only NAME ...]
    python tools/record_reference_cassettes.py --list

Each scenario fetches one URL live through :class:`verinoda.references.transport.LiveTransport`
(SSRF-guarded, host intervals honoured, no credentials), trims large bodies to what the tests need
(marked ``"trimmed": true``) and writes ``<out>/<scenario>/<nn>-<host>-<key12>.json`` in the
``verinoda.http_cassette/1`` format - the same format as the runtime cache, so a recording is a fixture.

What is never stored: Authorization, Cookie and Set-Cookie headers, credential-like query parameters,
the User-Agent contact. Re-record to see API shape drift: ``git diff tests/fixtures/references/cassettes``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from verinoda.references.transport import (  # noqa: E402
    LiveTransport,
    Response,
    to_entry,
)

RECORDER = "tools/record_reference_cassettes.py/1"


def _json_trim(keep):
    def trim(resp: Response) -> tuple[bytes, bool]:
        try:
            data = json.loads(resp.body)
        except ValueError:
            return resp.body, False
        out = keep(data)
        return json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8"), out != data
    return trim


def _pypi_keep(versions):
    def keep(d):
        info = {k: d.get("info", {}).get(k) for k in ("name", "version", "project_urls", "home_page",
                                                      "requires_python", "yanked", "yanked_reason")}
        rel = {v: [{k: f.get(k) for k in ("filename", "packagetype", "digests", "upload_time",
                                          "upload_time_iso_8601", "yanked", "yanked_reason", "url")}
                   for f in files[:2]]
               for v, files in (d.get("releases") or {}).items() if v in versions}
        return {"info": info, "releases": rel}
    return keep


def _npm_keep(versions):
    def keep(d):
        return {"name": d.get("name"), "dist-tags": d.get("dist-tags"), "modified": d.get("modified"),
                "versions": {v: {k: i.get(k) for k in ("name", "version", "deprecated")}
                             for v, i in (d.get("versions") or {}).items() if v in versions}}
    return keep


def _html_head(resp: Response) -> tuple[bytes, bool]:
    text = resp.body.decode("utf-8", errors="replace")
    m = re.search(r"</title>", text, re.I)
    if not m:
        return resp.body[:4096], len(resp.body) > 4096
    cut = text[: m.end()] + "\n</head><body>(trimmed by the recorder)</body></html>\n"
    return cut.encode("utf-8"), True


def _github_issue_keep(d):
    return {k: d.get(k) for k in ("url", "html_url", "number", "title", "state", "updated_at", "closed_at",
                                  "pull_request")} | {"body": (d.get("body") or "")[:600]}


SCENARIOS = {
    "pypi_requests": ("https://pypi.org/pypi/requests/json", None,
                      _json_trim(_pypi_keep({"2.29.0", "2.30.0", "2.31.0", "2.32.0", "2.32.1", "2.32.3"}))),
    "pypi_missing": ("https://pypi.org/pypi/this-package-does-not-exist-verinoda/json", None, None),
    "arxiv_1706": ("http://export.arxiv.org/api/query?id_list=1706.03762", None, None),
    "arxiv_1706_oai": ("http://export.arxiv.org/oai2?verb=GetRecord&identifier=oai:arXiv.org:1706.03762"
                       "&metadataPrefix=arXivRaw", None, None),
    "python_docs_3": ("https://docs.python.org/3/library/asyncio-task.html", None, _html_head),
    "rtd_requests_v2310": ("https://app.readthedocs.org/api/v3/projects/requests/versions/v2.31.0/", None, None),
    "rtd_requests_stable": ("https://app.readthedocs.org/api/v3/projects/requests/versions/stable/", None, None),
    "github_issue": ("https://api.github.com/repos/psf/requests/issues/6000", "application/vnd.github+json",
                     _json_trim(_github_issue_keep)),
    "go_net_info": ("https://proxy.golang.org/golang.org/x/net/@v/v0.20.0.info", None, None),
    "go_net_list": ("https://proxy.golang.org/golang.org/x/net/@v/list", None, None),
    "npm_react": ("https://registry.npmjs.org/react", "application/vnd.npm.install-v1+json",
                  _json_trim(_npm_keep({"18.2.0", "18.3.1", "19.0.0"}))),
    "pypi_provenance": ("https://pypi.org/integrity/requests/2.34.2/requests-2.34.2.tar.gz/provenance",
                        "application/vnd.pypi.integrity.v1+json", None),
}


def record(name: str, out: Path, live: LiveTransport) -> Path:
    url, accept, trim = SCENARIOS[name]
    resp = live.get(url, accept=accept)
    trimmed = False
    if trim is not None and resp.ok:
        resp.body, trimmed = trim(resp)
    entry = to_entry(resp, accept=accept, trimmed=trimmed, recorder=RECORDER)
    d = out / name
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.json"):
        old.unlink()
    host = re.sub(r"[^a-z0-9]+", "_", (resp.url.split("/")[2] if "://" in resp.url else "host").lower())
    p = d / f"01-{host}-{entry['key'][:12]}.json"
    p.write_bytes(json.dumps(entry, ensure_ascii=False, indent=1).encode("utf-8"))
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(ROOT / "tests" / "fixtures" / "references" / "cassettes"))
    ap.add_argument("--only", nargs="*", default=None, help="scenario names (default: all)")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)
    if a.list:
        for n, (url, accept, _) in SCENARIOS.items():
            print(f"{n:22} {url}" + (f"  [{accept}]" if accept else ""))
        return 0
    live = LiveTransport()
    rc = 0
    for name in a.only or list(SCENARIOS):
        try:
            p = record(name, Path(a.out), live)
            e = json.loads(p.read_text(encoding="utf-8"))
            print(f"{name:22} HTTP {e['response']['status']} -> {p.relative_to(ROOT) if p.is_relative_to(ROOT) else p}"
                  + (" (trimmed)" if e["response"]["trimmed"] else ""))
        except Exception as exc:  # noqa: BLE001 - keep recording the others
            rc = 1
            print(f"{name:22} FAILED {type(exc).__name__}: {exc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
