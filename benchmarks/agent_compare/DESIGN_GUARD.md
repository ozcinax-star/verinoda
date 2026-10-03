# Does checking each edit help an agent write working code? (pre-registration, 2026-10-02)

Written before any task of this study was written and before any session ran. The earlier agent studies asked
questions; the agent alone found 95-98 % of the facts by reading the files, and no index added any. Writing code is
different: an agent often writes from what it remembers of an API rather than from the file, and what it remembers
can be wrong or missing for code that changed after its training. verinoda-live 0.4.0 checks every edit of a
Python, Java or Kotlin file with `verinoda check --diff` and tells the model, with the edit's result, the names on
the changed lines that do not exist in the project or its environment, with the nearest real names. This study
measures that feature in real Claude Code sessions.

## Corpora

Graphify (`Graphify-Labs/graphify` at ef4450d, 2026-09-30) and SQLModel (`fastapi/sqlmodel` at 1c46f0d,
2026-09-23): Python, with much of their code changed after the model's training data ends (mid-2026). Each session
gets a fresh clone at that commit with a Verinoda index (built once per repository and copied in), and runs Python
in an environment with the repository's dependencies and not the package itself (`PYTHONPATH` = the clone).

## Tasks

Per repository, agents write tasks a developer of the project might be given: add or change a function so that it
does something specified, where a natural solution uses the project's own internal functions, classes or keyword
arguments, preferring ones added or changed since 2026-06-15 (from the repository's history). Each task has:

- a prompt that says what to build, where (module, name, signature) and how it must behave, without naming the
  internal helpers a solution would use;
- a hidden pytest file that imports the result and checks the behaviour, never shown to the session;
- a reference solution (a patch).

Every task is checked mechanically before the first session: in a fresh clone the hidden test fails, and with the
reference patch applied it passes. An independent reviewer reads every task for leaks (the prompt gives away the
solution or the helper names) and for whether the prompt specifies everything the hidden test checks; a task it
flags is fixed once or dropped. The tasks are fixed and committed before the first session.

## Arms

Two real headless Claude Code sessions (`claude -p`, Claude Opus 5.5 at the session's default effort) per task, the
same prompt, the same fresh clone, the same tools (`Read`, `Edit`, `Write`, `Glob`, `Grep`, `Bash`), the prompt
never mentioning Verinoda:

- `plain`: no plugin;
- `guard`: verinoda-live loaded (`--plugin-dir`), its check after edits on (auto-context and commit review off, as
  they are by default).

## Scoring

- **Pass** (primary): the hidden test passes on the clone the session left (run after the session, with the same
  environment).
- **Absent names left** (primary for the second decision): `verinoda check --diff` on the final clone: absent or
  mismatched sites on lines the session changed.
- Secondary: the session's turns, cost and tokens (its own JSON report), the number of notes the check gave
  (`guard`), wall time.

## Decisions

1. A claim that the check helps an agent write working code is made only if `guard - plain` passes summed over tasks
   has a 95 % percentile-bootstrap interval (over tasks, 10,000 resamples, seed 20261002) above zero.
2. A claim that it leaves fewer non-existent names in the code is made only if `plain - guard` absent sites has its
   interval above zero.

Otherwise: no measurable difference.

## Limits stated in advance

Two repositories, one language; one session per cell; the tasks are written by a model and lean towards using the
project's internals, which is the case the check is for, not a sample of all coding work; the reference solutions
are a model's; `verinoda check` cannot see names made at runtime.
