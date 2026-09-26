# pyloop (verdict audit fixture)

A tiny event loop used by the verdict audit (`verinoda benchmark verdict-audit`).

- `loop/base.py` - the loop: `call_soon`, `call_later`, `run_once`
- `loop/transport.py` - a transport that queues protocol events on its loop
- `loop/scheduler.py` - a priority queue of named jobs
- `reference/oldloop/` - an excerpt of the loop this project replaced, kept for comparison
  (marked as a reference tree in the audit's config)
