"""Pin precedence ladder and mismatch catalogue (docs/DESIGN.md D11, D12).

Every reference gathers *candidate pins* from what the user and the project
say about it; :func:`choose` takes the highest-ranked candidate and keeps the
others as alternates, so a choice between competing versions is always
recorded (``pin.basis``) and never silent.

Ladder (``BASIS_RANK``, lower wins):

1. ``immutable_in_reference`` - a commit SHA, SWHID, arXiv ``vN``, DOI,
   ``pkg==x.y.z`` / ``purl@v`` / Go pseudo-version in the reference itself
2. ``explicit_text_version`` - a version the text binds to the reference
3. ``explicit_floating_intent`` - "main", "latest", "en son", ``/releases/latest``
4. ``url_tag`` / ``url_pr_head`` - a tag or a PR/MR head named in the URL
5. ``local_project_version`` - lock file > installed metadata > runtime pin
6. ``url_branch`` - a branch (or floating docs slot) in the URL; ranks *above* 5
   unless the question is about local behaviour and the reference is a local
   dependency
7. ``text_date`` - the commit/release at a date in the text
8. ``default`` - default-branch HEAD / newest release / latest ``vN``, always
   with warning ``W_floating``

Invariant I2: when the winning candidate names a version that does not exist
(M10), the reference is unresolved - the next rung is never used in its place.
"""

from __future__ import annotations

BASIS_RANK = {
    "immutable_in_reference": 1,
    "explicit_text_version": 2,
    "explicit_floating_intent": 3,
    "url_tag": 4,
    "url_pr_head": 4,
    "local_project_version": 5,
    "url_branch": 6,
    "text_date": 7,
    "default": 8,
}
# Bases that pin something that moves (a branch tip, "latest", a floating docs slot).
FLOATING_KINDS = {"branch", "default_head", "latest_release", "latest_version", "latest_arxiv", "floating_doc",
                  "content_at_retrieval"}

# code -> (name, default severity)
MISMATCHES = {
    "M1": ("text_version_vs_url_ref", "warn"),
    "M1b": ("path_missing_at_pin", "error"),
    "M1c": ("lines_from_other_ref", "warn"),
    "M2": ("url_ref_vs_local_version", "warn"),
    "M3": ("floating_doc_vs_runtime", "warn"),
    "M4": ("paper_version", "warn"),
    "M5": ("ref_name_collision", "warn"),
    "M6": ("pr_moved_or_squashed", "warn"),
    "M7": ("repo_moved", "info"),
    "M8": ("artifact_source_mismatch", "warn"),
    "M9": ("yanked_or_deprecated", "warn"),
    "M10": ("version_not_found", "error"),
    "M11": ("docs_version_not_hosted", "info"),
    "M12": ("source_not_public", "info"),
}
SEVERITY_ORDER = {"info": 0, "warn": 1, "error": 2}


def mismatch(code: str, detail: str, resolution: str, *, severity: str | None = None, **extra) -> dict:
    name, sev = MISMATCHES[code]
    out = {"code": code, "name": name, "severity": severity or sev, "detail": detail, "resolution": resolution}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def candidate(basis: str, *, pin: dict | None = None, error: str | None = None, source: str | None = None,
              **extra) -> dict:
    """A candidate pin; ``pin`` None with ``error`` means the requested version could not be resolved."""
    return {"basis": basis, "rank": BASIS_RANK[basis], "pin": pin, "error": error, "source": source, **extra}


def make_pin(kind: str, value: str | None, display: str, basis: str, *, immutable: bool, name: str | None = None,
             ref: str | None = None, retrieved_at: str | None = None, **extra) -> dict:
    out = {"kind": kind, "value": value, "display": display, "basis": basis,
           "precedence_rank": BASIS_RANK.get(basis), "immutable": immutable, "name": name, "ref": ref,
           "retrieved_at": retrieved_at}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def is_floating(pin: dict | None) -> bool:
    return bool(pin) and (pin.get("kind") in FLOATING_KINDS)


def order(cands: list[dict], *, demote_branch: bool) -> list[dict]:
    """Candidates by effective rank: ``url_branch`` sits above the local version unless demoted."""
    def eff(c: dict) -> float:
        if c["basis"] == "url_branch" and not demote_branch:
            return 4.5
        return float(c["rank"])

    return sorted(cands, key=lambda c: (eff(c), c.get("tiebreak", 0)))


def choose(cands: list[dict], *, demote_branch: bool) -> tuple[dict | None, list[dict], dict | None]:
    """-> (winning candidate or None, alternates, blocking candidate).

    The blocking candidate is the highest-ranked one that failed (a named
    version that was not found): per I2 nothing below it is used.
    """
    ordered = order(cands, demote_branch=demote_branch)
    for i, c in enumerate(ordered):
        if c.get("pin") is None:
            if c.get("error") and c.get("blocking", True):
                return None, [x for x in ordered if x is not c and x.get("pin")], c
            continue
        return c, [x for x in ordered[i + 1:] if x.get("pin")] + [x for x in ordered[:i] if x.get("pin")], None
    return None, [], None


def worst_severity(mms: list[dict]) -> str | None:
    if not mms:
        return None
    return max((m["severity"] for m in mms), key=lambda s: SEVERITY_ORDER.get(s, 0))
