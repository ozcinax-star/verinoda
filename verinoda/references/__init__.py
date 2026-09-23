"""Reference resolution: which exact version of which thing the user meant (docs/DESIGN.md D10-D16).

Pipeline: :mod:`~verinoda.references.mentions` (TR/EN mention extraction) ->
:mod:`~verinoda.references.classify` (table-driven classes) -> binding and
coreference -> :mod:`~verinoda.references.pin` (precedence ladder, mismatch
catalogue M1-M12) -> report (schema ``verinoda.reference_resolution/1``).
Supporting modules: :mod:`~verinoda.references.local` (lock files, the
project's own venv metadata, runtime pins), :mod:`~verinoda.references.gitref`
(qualified refs, trees), :mod:`~verinoda.references.transport` (offline-first
HTTP with cache and cassettes), :mod:`~verinoda.references.registries`,
:mod:`~verinoda.references.contentmatch` (package -> commit by blob hashes)
and :mod:`~verinoda.references.provenance` (PEP 740 source commit).

Public API::

    resolve(store, repo, text, *, explicit=(), network="cache", topic=None, now=None) -> dict
    extract(text) -> list[dict]
    classify(ref_or_mention, kind="auto") -> dict
    local_versions(repo) -> dict
    render_text(result) -> str
"""

from __future__ import annotations

from verinoda.references.classify import classify
from verinoda.references.local import local_versions
from verinoda.references.mentions import extract
from verinoda.references.render import compact, render_text
from verinoda.references.resolver import SCHEMA, accounted, resolve

__all__ = ["SCHEMA", "accounted", "classify", "compact", "extract", "local_versions", "render_text", "resolve"]
