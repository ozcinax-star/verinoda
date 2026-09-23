"""PEP 740 provenance -> the source repository, commit and ref an artifact was built from.

PyPI serves ``/integrity/<name>/<version>/<filename>/provenance`` for files
uploaded with Trusted Publishing. Each attestation carries a Fulcio
certificate whose X.509 extensions (OID arc ``1.3.6.1.4.1.57264.1``) record
the build: ``.12`` source repository URI, ``.13`` source commit digest,
``.14`` source ref, ``.9`` build signer URI, ``.18``/``.19`` build config.

This module reads those fields with the standard library only (a DER scan for
the OIDs). It does **not** verify the Sigstore signature: results say
``signature_checked: False`` and rank as ``attested`` (below a content match),
never as verification on their own.
"""

from __future__ import annotations

import base64
import json
import urllib.parse

OIDS = {
    12: "source_repository_uri", 13: "source_repository_digest", 14: "source_repository_ref",
    9: "build_signer_uri", 18: "build_config_uri", 19: "build_config_digest", 8: "issuer",
}


def _oid_bytes(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    out = [40 * parts[0] + parts[1]]
    for p in parts[2:]:
        enc = [p & 0x7F]
        p >>= 7
        while p:
            enc.append(0x80 | (p & 0x7F))
            p >>= 7
        out += reversed(enc)
    return bytes(out)


def _read_len(b: bytes, i: int) -> tuple[int, int]:
    n = b[i]
    i += 1
    if n < 0x80:
        return n, i
    k = n & 0x7F
    return int.from_bytes(b[i:i + k], "big"), i + k


def ext_value(der: bytes, dotted: str) -> str | None:
    """The string value of one certificate extension (``None`` when absent)."""
    ob = _oid_bytes(dotted)
    pat = bytes([0x06, len(ob)]) + ob
    i = der.find(pat)
    if i < 0:
        return None
    i += len(pat)
    if der[i] == 0x01:  # optional BOOLEAN "critical"
        i += 3
    if der[i] != 0x04:
        return None
    n, i = _read_len(der, i + 1)
    inner = der[i:i + n]
    if inner and inner[0] in (0x0C, 0x13, 0x16):  # UTF8String / PrintableString / IA5String (v2 extensions)
        n2, j = _read_len(inner, 1)
        return inner[j:j + n2].decode("utf-8", errors="replace")
    return inner.decode("utf-8", errors="replace")


def fulcio_fields(cert_der: bytes) -> dict:
    return {name: ext_value(cert_der, f"1.3.6.1.4.1.57264.1.{n}") for n, name in OIDS.items()}


def from_provenance(doc: dict) -> list[dict]:
    """One entry per attestation: repository, commit, ref, subject, publisher (signature not checked)."""
    out = []
    for bundle in doc.get("attestation_bundles") or []:
        pub = bundle.get("publisher") or {}
        for att in bundle.get("attestations") or []:
            try:
                cert = base64.b64decode(att["verification_material"]["certificate"])
            except (KeyError, ValueError, TypeError):
                continue
            f = fulcio_fields(cert)
            subject = None
            try:
                st = json.loads(base64.b64decode(att["envelope"]["statement"]))
                subject = [{"name": s.get("name"), "sha256": (s.get("digest") or {}).get("sha256")}
                           for s in st.get("subject") or []]
            except (KeyError, ValueError, TypeError):
                pass
            out.append({"repository": f["source_repository_uri"], "commit": f["source_repository_digest"],
                        "ref": f["source_repository_ref"], "signer": f["build_signer_uri"], "subject": subject,
                        "publisher": pub, "signature_checked": False, "method": "attested", "strength": "attested"})
    return out


def pypi_provenance(transport, name: str, version: str, filename: str) -> dict:
    """Fetch and decode the PEP 740 provenance of one PyPI file (``ok: False`` when none is published)."""
    from verinoda.references.registries import _get, _status_fail

    url = (f"https://pypi.org/integrity/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/"
           f"{urllib.parse.quote(filename)}/provenance")
    resp, err = _get(transport, url, accept="application/vnd.pypi.integrity.v1+json")
    if err:
        return err
    bad = _status_fail(resp, f"PyPI provenance {filename}")
    if bad:
        return bad
    try:
        doc = resp.json()
    except ValueError:
        return {"ok": False, "reason": "bad_response", "error": "invalid provenance JSON"}
    atts = from_provenance(doc)
    if not atts:
        return {"ok": False, "reason": "not_found", "error": "no decodable attestation"}
    return {"ok": True, "attestations": atts, "from_cache": resp.from_cache, "retrieved_at": resp.retrieved_at}
