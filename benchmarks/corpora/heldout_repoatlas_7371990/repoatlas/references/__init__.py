"""Reference resolution: which exact version of which thing the user meant (docs/DESIGN.md D10-D16).

Pipeline: :mod:`~repoatlas.references.mentions` (TR/EN mention extraction) ->
:mod:`~repoatlas.references.classify` (table-driven classes) -> binding and
coreference -> :mod:`~repoatlas.references.pin` (precedence ladder, mismatch
catalogue M1-M12) -> report (schema ``repoatlas.reference_resolution/1``).
Supporting modules: :mod:`~repoatlas.references.local` (lock files, the
project's own venv metadata, runtime pins), :mod:`~repoatlas.references.gitref`
(qualified refs, trees), :mod:`~repoatlas.references.transport` (offline-first
HTTP with cache and cassettes), :mod:`~repoatlas.references.registries`,
:mod:`~repoatlas.references.contentmatch` (package -> commit by blob hashes)
and :mod:`~repoatlas.references.provenance` (PEP 740 source commit).

Public API::

    resolve(store, repo, text, *, explicit=(), network="cache", topic=None, now=None) -> dict
    extract(text) -> list[dict]
    classify(ref_or_mention, kind="auto") -> dict
    local_versions(repo) -> dict
    render_text(result) -> str
"""

from __future__ import annotations

from repoatlas.references.classify import classify
from repoatlas.references.local import local_versions
from repoatlas.references.mentions import extract
from repoatlas.references.render import compact, render_text
from repoatlas.references.resolver import SCHEMA, accounted, resolve

__all__ = ["SCHEMA", "accounted", "classify", "compact", "extract", "local_versions", "render_text", "resolve"]
