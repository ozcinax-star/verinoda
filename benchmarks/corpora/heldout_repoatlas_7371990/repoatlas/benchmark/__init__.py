"""Reproducible benchmark: raw file search vs Graphify's query output vs RepoAtlas.

Public API (used by ``repoatlas benchmark run``)::

    run_benchmark(repo, *, questions=None, out=None, graphify_cmd=None, llm="none", repeat=2) -> dict
    render(res) -> None

The metric definitions, question sets and measured results are documented in
docs/BENCHMARKS.md. The analysed repository is only read: every run works on a
temporary copy, so the index and database it builds never touch the original.
"""

from __future__ import annotations

from repoatlas.benchmark.report import markdown, render
from repoatlas.benchmark.runner import builtin_sets, load_questions, run_benchmark

__all__ = ["run_benchmark", "render", "markdown", "load_questions", "builtin_sets"]
