# ADR 0001: callbacks run on the next pass

Status: accepted

Callbacks queued with call_soon run on the next pass of run_once, never inside the call that
queued them. Running a callback at once would let it re-enter the loop from the caller's stack,
so the order of callbacks would depend on how deep the caller was.
