# Run 2 protocol (shared by both variants)

Product: exactly SPEC.md (../../SPEC.md). Both variants are built in three phases, each phase is a separate agent
run. The orchestrator (not the agent) records the token and time numbers of every phase from the agent's usage report
into metrics.csv. Agents do not guess their own token numbers.

## Phases

- P1 Data layer: read SPEC.md and corpus/, write query.py that produces the SPEC JSON. Test with at least 3 queries
  (mitoz, oksijen, an unknown word). Do NOT write serve.py or web/ in P1.
- P2 Server and page: write serve.py and web/ (index.html, style.css, app.js) that use query.py. Do not change the
  JSON contract of query.py without writing it in the log.
- P3 Verify and document: start the server once on your port, check /api/graph?q=mitoz and the static files, write
  README.md (what it does, how to run, what it does NOT do yet), finish the self-report, stop the server.

Phases run in order inside one variant. The two variants run in parallel.

## Ports and folders

- symbiosis variant: folder C:\Users\ozcin\biyo-platform\symbiosis\ , port 8000. Folder kb\ is the Verinoda project
  (corpus mirror plus the current verinoda index, rebuilt 2026-10-09). Do not delete kb\ and do not write into it
  except through verinoda's own commands.
- baseline variant: folder C:\Users\ozcin\biyo-platform\baseline\ , port 8001.

## Forbidden to both

- Anything under benchmark\gold\ (SPEC rule 2).
- Anything under archive\ (earlier runs). Both variants start from empty folders on purpose.
- Writing into corpus\ or benchmark\ or devtest\ except your own phase log.
- Starting a server on the other variant's port.

## Phase log (required)

Write devtest\run2\<variant>-p<N>.md with these sections:

1. Steps, one line each: what you did and the time (approximate, HH:MM is enough).
2. Files read: paths only.
3. Tool results: for every search or Verinoda call, the question, the tool name and the approximate number of
   characters of the result you received (estimate from the output, do not guess a token count).
4. Problems: where you got stuck or re-read something.
5. Not verified: what you did not check.

## Variant rules

- symbiosis: you are the Verinoda variant. Use Verinoda where it helps (the verinoda CLI with the kb project:
  `C:\Users\ozcin\verinoda-mod\.venv\Scripts\python.exe -P -m verinoda query "<question>"` run from symbiosis\kb ;
  the MCP tools mcp__verinoda__* are bound to another repo, so do not rely on them for this project).
  Record each Verinoda call.
- baseline: no Verinoda in any form (no mcp__verinoda__*, no verinoda CLI, no verinoda package, nothing under
  C:\Users\ozcin\verinoda-mod or C:\Users\ozcin\verinoda). Ignore the verinoda section of the global CLAUDE.md.
  graphify is allowed only if needed; log every use.

## Final message of each phase

Three lines only: the phase, the files you wrote, and the phase log path. Nothing else.
