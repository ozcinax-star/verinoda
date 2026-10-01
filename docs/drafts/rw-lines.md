# Definition lines: annotations and Python overloads

## Why

The real-world run of 2026-10-02 (benchmarks/results/realworld-2026-10-02/defects.md) found two ways
Verinoda cited the wrong line for a definition:

- **D4.** A Python function declared with `@overload` stubs was cited at the first stub. sqlmodel's
  `Field` was reported at sqlmodel/main.py:242, an empty `...` body, instead of the implementation
  at :388. A trace or a "what does Field call" question started from the stub.
- **D8.** A Java method with an annotation was cited at the annotation line, while a decorated
  Python def is cited at its `def` line. gson's `doPeek` was reported at JsonReader.java:581
  (`@SuppressWarnings("fallthrough")`) instead of :582 (`int doPeek()`). `@Override` is on almost
  every Java method, so these answers were often one line off.

Both come from the vendored extractor (verinoda/project_index/extractors/engine.py). For every
tree-sitter language except Python, it took the line where the declaration node starts. In Java,
Kotlin and C#, that node starts with its modifiers, so its annotations or attributes come first.
For Python, the first definition of a name won: a redefinition with the same id was dropped, and
its call edges were merged into the first node.

## Decisions

- **The cited line is the line of the name.** `_declaration_line` (engine) and `ts_name_line`
  (anchors) give the line of the node's `name` field when that field is inside the node, and the
  node's first line otherwise. C/C++ declarators and synthesized names (Swift `deinit`) keep the
  first line. The rule is the same for every grammar, not a Java-only special case:
  - Python gets the same answer as before.
  - A TS method decorator is a sibling node, so TS also gets the same answer as before.
  - Java, Kotlin and C# methods and classes move to their name line. So do PHP and Swift attributes
    that sit inside the node; I did not test those two.
- **The span still starts at the annotation.** `index.ts_def_info` returns the old
  `start -> end` map. It also maps each name line to its end and to the definition's first line
  (`heads`). `Graph.span` starts the span there, so:
  - the annotation line still belongs to the method (`symbol_at`, `own_line_count`, rationale,
    affected lines);
  - `source()` still shows the annotation.

  A graph built before this change (located at the annotation) gets exactly the span it had.
  Python spans are unchanged: they start at the `def` and exclude decorators, as before.
- **Anchors facts.** For tree-sitter languages, the `def` field is now the name line. `start` stays
  the node's first line, the same `start`/`def` pair Python facts already had (decorator line, def
  line), so `a in (start, def)` matches in analysis, critique and entail. Fingerprints did not
  change, so the scheme `ts1` and every anchor made under it stay valid. Only the `file_facts` row
  key changes (`cache_scheme`: `ts1.def2`), so facts cached before are computed again once instead of
  being read back with the old `def`.
- **review_rules** (`_ts_def_at`, `ts_header`, `ts_param_count`) finds a definition by either its
  name line or its first line (`_defined_on`). This covers new facts and facts cached before.
- **Python overloads are one node, at the implementation.** I kept the existing convention:
  same-scope redefinitions share one id and one node. Java overloads are separate nodes (D57)
  only because a call binds to one of them by argument count, which does not apply to `@overload`
  stubs.
  - The decorated-definition branch records each `function_definition` under `@overload`
    (`overload` or `typing.overload`).
  - The first stub creates the node, which is flagged `overload_stub: true`.
  - The first def without `@overload` moves the node to its own line and clears the flag.
  - The stubs' lines are kept in the node's `overloads` metadata.
  - With stubs only (a stub module), the node stays at the first stub and keeps the flag.
  - Other redefinitions (a conditional `def` or a property setter) keep first-wins, as before.
- `_AST_CACHE_SCHEMA` 8 -> 9, so cached per-file extractions are made again.

## Measured

Windows 11, Python 3.13, one process at a time. The clones are the pinned ones in C:/vbench.

| repository | gold v1 before (2026-10-02 run) | gold v1 after | crashes | timeouts | clean after revert | scan s before / after | update s before / after |
|---|---|---|---|---|---|---|---|
| fastapi/sqlmodel | 9/10 (`def-field-impl` missed: cited :242) | 10/10 | 0 | 0 | yes | 35.1 / 30.0 | 18.3 / 19.2 |
| google/gson | 9/10 (`def-dopeek` missed: cited :581) | 10/10 | 0 | 0 | yes | 30.0 / 29.8 | 25.1 / 27.5 |

The scan and update times are single runs and are within run-to-run noise. I did not run the other
8 repositories.

## Limits

- The cited line is the name's line. For a declaration whose modifiers wrap onto their own line
  (`@Override` / `public` / `String toString()`), that is the line of `toString`, not the line of
  `public`.
- A method with an annotation on the same line (`@Test void t()`) and one under an annotation now
  differ only in where the span starts. Python spans still exclude decorators while Java spans
  include annotations. This is deliberate: it keeps every Java span exactly as it was.
- Python `@overload` stubs get no node of their own. A question about one stub's signature gets
  the implementation, and the stub lines only through the `overloads` metadata. A stub that comes
  after the implementation is recorded in `overloads` but moves nothing.
- Only Java, Kotlin, C# and TS (methods and classes) and Python are covered by tests. PHP
  attributes, Swift attributes and Scala annotations follow the same rule untested.
- Benchmark files that cite Java lines were not re-checked, apart from the realworld gold of
  sqlmodel and gson: benchmarks/review_fixtures, benchmarks/verdict_audit and the mods results.
  A gold that cites an annotation line as the definition would now miss.
- The extractor's own test suite (tests_upstream) was not run. A grep found no test there that
  asserts the line of an annotated or overloaded definition.

## Upgrading note

- The first `update` after upgrading rebuilds the graph: the extraction stamp and
  `_AST_CACHE_SCHEMA` 9 both change.
- Java, Kotlin and C# methods and classes with an annotation or attribute above their name are then
  cited one or more lines lower, at the name. Their spans do not change.
- Python functions with `@overload` stubs are cited at the implementation, with the stubs' lines
  in `metadata.overloads`.
- Claims citing the old line still resolve: the span covers the annotation, and review and anchor
  lookups accept the first line too.
- Tree-sitter anchor facts are computed again once (new cache key `ts1.def2`). Anchors already
  stored keep their scheme and stay valid.

## Tests

- New tests in tests/test_definition_lines.py (4):
  - Java, Kotlin, C# and TS methods and classes are cited at their name line, including a
    multi-line modifier list.
  - Python `@overload` and `@typing.overload` groups, at module level and in a class, are one node
    at the implementation. The node has `overloads` metadata and the implementation's call edge.
    With stubs only, the node stays at the first stub with `overload_stub`.
  - `ts_def_info` returns the heads and ends. `Graph.span` starts at the annotation for a new graph
    and for an old one, and `symbol_at` still gives the annotation line to the method.
  - Anchor facts use the name line as `def`. `ts_param_count` finds the method from either line,
    and `cache_scheme` keys tree-sitter facts apart.
- tests/test_testcode.py: the JUnit test `coolsByOne` is cited at :8, its span starting at its
  `@Test` line, :7.
- Updated expectations: these tests had recorded the annotation line as the definition line.
  - tests/test_sides.py: the `Mod.onInitialize` entry moves from Mod.java:6 to :7.
  - tests/test_jvm_callbacks.py: the entries move to their name lines (GlowMod.java:20,
    GlowModClient.java:19, ModEvents.java:27, the `ModEvents` class at :16).
