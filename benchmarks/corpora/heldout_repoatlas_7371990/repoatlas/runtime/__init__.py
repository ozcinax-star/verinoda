"""Runtime observation (docs/DESIGN.md D28).

``repoatlas.runtime.trace.observe`` runs selected pytest tests in the isolated
experiment runner with the ``sys.monitoring`` call-trace plugin
(:mod:`repoatlas.runtime.calltrace_plugin`), ingests the trace into
``runtime_runs`` / ``runtime_calls`` and returns observed call edges, per-test
reach sets, boundary calls and ``call_trace`` evidence records.

An observation is existential and run-scoped - "in run R at commit C, line L
started B" - never an "always" statement, and edges seen only through test
doubles never support production edges.
"""

from repoatlas.runtime.trace import observe, select_tests

__all__ = ["observe", "select_tests"]
