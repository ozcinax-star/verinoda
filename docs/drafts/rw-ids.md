# Distinct ids for names that mint one id (real-world defect 1)

## Why

The real-world run of 2026-10-02 (benchmarks/results/realworld-2026-10-02/defects.md, defect 1)
found two distinct symbols sharing one node. `normalize_id` (verinoda/project_index/ids.py) folds
case and drops leading and repeated underscores, so axios `Axios.request` (lib/core/Axios.js:40) and
`Axios._request` (:83) both mint `lib_core_axios_axios_request`. In express, the selector
`lib/response.js::sendFile` resolved to the module function `sendfile` (:927) as an exact match.

Verinoda already had guards against this. They missed both cases:

- `verinoda.case_ids.split_case_collisions`, run by `index._distinct_case_ids` around extraction
  and graph building, splits nodes of one file that share an id. Two things kept it from the axios
  case:
  1. It only handled names equal after `casefold`. `request` and `_request` are not.
  2. It never saw the second node. The generic tree-sitter extractor
     (`extractors/engine.py`, `_extract_generic`) keeps a `seen_ids` set, and `add_node` silently
     drops a definition whose id is already taken. So `_request` was gone before any post-pass ran.
     Its `method` edge and every call in its body were still emitted under the shared id, and
     `this._request()` became a self-call on `request`, which was then dropped.
- Upstream has its own salts for Python (`_python_pre_scan_underscore_collisions`: module-level
  functions and direct methods only, the public name keeps the id) and for Go (exported versus
  unexported names). No other language had one, and Python nested functions were not covered.
- `verinoda/portable_ids.py` only rewrites ids minted from an absolute path. It has nothing to do
  with name collisions.
- The resolver (`naming.exact_nodes` -> `retrieval._names_symbol`) compares names with `fold_tr`,
  which lower-cases them. It already preferred the node with the right case when several were
  found. But when the only candidate differed in case (express: `sendfile` for `sendFile`), it
  returned that node as `exact`, with no note.

## Decisions

- **The extractor no longer drops the second definition.** It gives it its own id. `add_node`
  records which name holds each id. Before a function or method definition takes an id,
  `_distinct_def_id` checks that record. If a different name already holds the id, the new
  definition gets `<id>_<first 6 hex of sha1(name)>`. That is the same form the Python and Go salts
  and `case_ids` already use. If a third name already holds that salted id, the hash gets longer
  (10, 16, 40).
  - The check covers functions, methods, nested functions (JS and Python), JS `const f = () =>`,
    `exports.f =`, `X.prototype.f =`, `this.f =` / `api.f =` member assignments, and class-field
    arrows.
  - The same name again keeps one id, as before: an overload, a getter and its setter, Ruby
    `def self.x` beside `def x`.
  - Names are compared by their last part (`Foo::bar` against `bar`), so a C++ out-of-class
    definition still meets its declaration.
  - In PHP, `fold_case` makes names that differ only in case one symbol.
- **Who keeps the plain id:** the name with the fewest leading underscores (the public name), in
  any declaration order. During the walk the definition met first holds the plain id. When the file
  is done, `_public_def_swaps` gives the plain id to a later definition with fewer leading
  underscores, and the earlier holder gets the salted id of its own name. All of the file's nodes,
  edges and `raw_calls` callers are renamed together. A Java overload group moves with its first
  member. Names with the same number of underscores, such as a case pair (`getX` / `getx`) or a
  class `Foo` beside a function `foo`, keep declaration order. This is the rule of Python's upstream
  pre-scan and of the `case_ids` net (see the review round below).
- **The net in `case_ids` is widened** to names that mint one id. That means names that differ in
  case or underscores. A label that is not an identifier (a heading `Foo bar` beside `Foo-bar`)
  still needs case alone to be split.
  - Underscore differences split in every language. Case differences still split only in
    case-sensitive languages.
  - Of a split group, the member with the fewest leading underscores keeps the id, so the public
    name keeps it. For case-only groups this is the same code-point order as before.
  - The "split the nodes already show" pass now also routes edges that end at the plain id when the
    two names differ in underscores. The extractor's own import edge for `import { _helper }` is
    minted as `make_id(stem, "_helper")`, which is `helper`'s id. The line text now moves that edge
    to `_helper`.
- **Calls bind to the exact name.** The in-file call maps (`label_to_nid`) and the cross-file
  resolvers were already exact. Once both definitions exist, `this._request()` binds to
  `_request`, and `request()` to `request`.
- **A code name matched only case-insensitively is a labelled fallback, never `exact`.**
  `naming.resolve` runs a new check, `_case_folded`. It applies when every node found is in a
  case-sensitive language and none of them is named with the written case.
  - One node: the result is `similar`, with the note "no symbol is named `sendFile` with this case;
    ... matched sendfile() (lib/response.js:927) only case-insensitively". `trace` reports that
    note under `fuzzy`.
  - Several nodes: the result is `ambiguous`, with the same note.
  - An exact-case node, when one exists, still wins as before.
- **The AST cache schema goes from 9 to 11** (together with the definition-line fix, which took 9) (`project_index/cache.py`), so cached extractions made
  by the old code are not reused. 9 was the first version of this branch.
- The vendored changes are marked "Verinoda patch". docs/UPSTREAM.md lists them.
- **Review round.** A reviewer found three defects in the first version. Each is fixed and has a
  regression test in tests/test_distinct_ids.py.
  1. *A public function lost its id to an earlier private const.* In `const _config = {}` followed
     by `export function config()`, the walk met the const first, so the const kept
     `src_store_config` and the function moved to a salted id. The base graph had the function on
     that id. The claim that "no id that existed before changes" was false. Now the public name
     takes the plain id in any order (see "Who keeps the plain id"). The extractor and the
     `case_ids` net now follow one rule. Test:
     `test_the_public_name_keeps_the_plain_id_whichever_is_declared_first` (also `__a` / `_a` / `a`
     declared in that order).
  2. *Java ids that already existed changed, and a numbered overload id named a different method.*
     The base graph already kept Java `_fetch` as its own node through overload numbering
     (`..._fetch_3`), in the family of the public name. The first version salted `_fetch` but
     still numbered its second overload in that family, so `fetch_3` changed from `_fetch(int)` to
     `_fetch(int,int)`. Now a salted method's overloads are numbered from its own salted id
     (`..._fetch_<hash>_2`). Java `_x` ids therefore change (see the upgrading note). The calls
     also improve: in the base graph, `fetch(a)` inside `_fetch(int,int)` also bound to
     `_fetch(int)`, because both took one argument. Now it binds only to `fetch`. Test:
     `test_java_overloads_of_a_salted_method_are_numbered_from_its_own_id` (both declaration
     orders).
  3. *A multi-line ES import of the private twin left a false import edge on the public twin.* For
     `import {` / `_helper,` / `} from './h'`, the edge's own line names neither twin. `case_ids`
     now reads an import whose `{` opens a list on to its `}`. An edge that nothing places is
     dropped when its source already reaches another member of the pair, through the same
     relation and from the same line. Tests:
     `test_the_build_net_reads_an_import_across_its_lines` and the multi-line import in the
     public-name test.

  Of the minor findings, one is fixed: `_def_symbol_name` now splits only a qualified name. A
  label such as `{ _helper: priv }` is its own name, which
  `test_a_label_that_is_no_qualified_name_is_its_own_symbol_name` checks. The other minor findings
  are listed under Limits. The AST cache schema went to 11 (10 on the branch), so caches made by the first version of
  this branch are not reused.

## Measured

On Windows 11 with Python 3.13.14, one process at a time.

- **Real-world benchmark** (`benchmarks/realworld/run.py --repos <repo> --out <scratch>`, clones in
  C:/vbench), before (benchmarks/results/realworld-2026-10-02) and after:

  | repo | gold v1 before | gold v1 after | gold v2 before | gold v2 after | crashes | clean after |
  |---|---:|---:|---:|---:|---:|---|
  | axios/axios v1.20.0 | 7/10 | 9/10 | - | - | 0 | yes |
  | expressjs/express v5.2.1 | 4/10 | 4/10 | 2/2 | 2/2 | 0 | yes |

  After the review round, both repositories were run again, one at a time. The gold, crashes and
  clean results are the same as in the table.

  - axios: `request-calls-private` and `private-request-merges-config` now hit.
    `request-reaches-adapter` still misses, for a different reason than before. Before, the source
    was unresolved. Now `_request` resolves (lib/core/Axios.js:83), but there is no directed path.
    Both hops are absent from the graph:
    - `dispatchRequest.call(this, newConfig)` (Axios.js:242);
    - `adapters.getAdapter(...)` (dispatchRequest.js:52).

    `trace _request dispatchRequest` and `trace dispatchRequest lib/adapters/adapters.js::getAdapter`
    each answer "no directed path". These are not id problems.
  - express: `sendfile-calls-helper` still misses, because `res.sendFile = function sendFile` is no
    node (defect 2). `trace lib/response.js::sendFile lib/response.js::sendfile` now returns the
    status "ambiguous: both endpoints resolved to the same node" with
    `fuzzy.source` = "no symbol is named `sendFile` with this case; `lib/response.js::sendFile`
    matched sendfile() (lib/response.js:927) only case-insensitively (in this language names that
    differ in case are different symbols)". The defect asked for exactly that warning.
  - I ran both repositories twice: once during development, then again on the final code. Both
    runs gave the same gold. Times of the final run:

    | repo | scan before | scan after | `update` before | `update` after |
    |---|---:|---:|---:|---:|
    | axios | 29.6 s | 29.7 s | 21.0 s | 30.7 s |
    | express | 11.1 s | 13.6 s | 9.1 s | 10.1 s |

    The development run measured axios at 25.8 s / 20.6 s and express at 14.1 s / 11.2 s. The
    review-round run measured axios at 29.3 s / 22.9 s and express at 11.4 s / 9.4 s. The spread
    between runs of the same code is as large as the before/after differences, so I do not
    attribute the times to this change.
- **Split pairs in the axios graph after the review-round run: 5** (3,889 nodes). Each one
  checked by hand:
  - `.request()` / `._request()` (lib/core/Axios.js);
  - `._transform()` (:11) keeps the plain id and `.__transform()` (:6) is salted
    (lib/helpers/ZlibHeaderTransformStream.js). Before the review round it was the other way
    round, because `__transform` comes first. By the base extractor's rule (it kept the definition
    met first), the base graph had `__transform` on the plain id and no node for `_transform`. I
    did not rebuild axios with the base package to confirm this. If it holds,
    this plain id now names a different method;
  - `setProxy()` / `__setProxy`, `isNodeEnvProxyEnabled()` / `__isNodeEnvProxyEnabled` and
    `isSameOriginRedirect()` / `__isSameOriginRedirect` (lib/adapters/http.js:1488-1490,
    `export const __setProxy = setProxy`). Before, each pair minted one id. I did not check
    whether the old graph merged or dropped the const.

  In the http.js pairs the plain id stays on the function. The express graph has 0 split pairs
  (441 nodes).
- **Upgrade without a file change** (fixture of 6 files):
  1. Build with the unmodified competitor-backlog package (`git archive`): the graph has
     `.request()` only, and the call at line 7 is read as `request`'s.
  2. Run `verinoda update` with the new code. It reported
     `"extraction": {"was": "s8-a8ca4efb242c", "now": "s9-f154e26c06a6"}` and
     `"index_mode": "full"`.
  3. The graph then had `lib_axios_axios_request_ee5c95` (`._request()`), the edge
     `request -calls-> _request`, and the call to mergeConfig on `_request`.

  Review round, with the reviewer's `const _config` / `function config` fixture: `verinoda scan .`
  with the unmodified package gave one node `src_store_config` (`config`, L13). `verinoda update`
  with the new code reported `"extraction": {"was": "s8-0b3160ad868f", "now":
  "s10-7fb9bff7fd72"}` and `"index_mode": "full"`. The graph then had `src_store_config`
  (`config()`, L13, still the id of the importer's and the callers' edges) and
  `src_store_config_7e810b` (`_config`, L12).
- **The new tests against the old code:** all 5 tests of tests/test_distinct_ids.py fail on the
  unmodified package (missing node `..._request_ee5c95`, missing nested `_step`, no split in the
  net, `exact` instead of `similar`; the unit test cannot import `_distinct_def_id`). All 5 pass
  with the change. The 4 review-round tests fail on the branch's first version and pass now.

## Limits

- **Language coverage.** Only the definition sites of the generic tree-sitter extractor are
  covered: JS/TS, Python, Java, C, C++, C#, Kotlin, Scala, Swift, Ruby, Lua, PHP, Groovy. The
  dedicated extractors (Rust, Dart, Elixir, Julia, Pascal, ...) are not changed. When they emit two
  nodes with one id, the widened `case_ids` net splits them. When they drop one themselves, nothing
  recovers it.
- **Class-like nodes are not checked.** Classes, properties, fields and object-literal owners still
  go through `add_node` without the check. Two classes `Q` and `_Q` in one file still merge into one
  node. The methods of the second class are minted under the shared id, so `class _Q { run() }`
  after `class Q` gives `src_k_q_run`, which reads as a method of `Q`. That is worse than a plain
  merge.
- **Declaration order decides only between names with the same number of leading underscores**: a
  case pair (`getX` / `getx`), or a class `Foo` beside a function `foo`. In the extractor the one
  declared first keeps the plain id. The `case_ids` net, for the nodes that still arrive with one
  id, takes the first by code point. Moving `getx` above `getX` swaps their ids on the next build.
- **C++ out-of-line twins.** Take a class that declares `int area(); int _area();` and defines
  `int Shape::_area()` out of line. The node it gets (`Shape::_area()`,
  `src_shape_shape_area_<hash>`) cannot be named: `_area`, `Shape._area`, `Shape::_area` and
  `src/shape.cpp::_area` all return `not_a_symbol` or `not_found`. `Shape::area` gets no `calls`
  edge to it, and the in-class declaration's `defines` edge still lands on `area`. This was
  already so before this change, so C++ is covered only for in-class definitions.
- **Import edges.** A cross-file import or call that upstream mints as `make_id(stem, name)` lands
  on the plain id, which belongs to the twin. Only the `case_ids` line-text routing moves it, and
  only when the edge's line names exactly one of the two. Two examples it cannot move:
  `import { helper, _helper }` on one line, and a call whose line writes neither name.
- **Multi-line imports are read only across braces.** An import edge whose line names neither
  twin is placed by reading on to the closing `}`. Otherwise it is dropped when the same source
  already has an edge of that relation and line to the other twin. With no such edge, it stays on
  the twin that kept the id. A single-line `import { helper, _helper }` names both, and its edges
  are left as they are.
- **Case-folded matching applies only to text read as code.** A plain capitalised word such as
  `Area` still resolves `exact` to `area` in a .cpp file. That is by design: a plain word is
  matched loosely.
- **Owners and module paths still match case-insensitively.** `axios.request` names
  `Axios.request`. Only the last name has to match its case. `q` (`f.name = "..."`) compares
  exactly, as before.
- **Calls not seen.** Calls through `fn.call(this, ...)` and member calls on an imported object
  (`adapters.getAdapter`) are still not edges (axios `request-reaches-adapter`).
- **Express needs defect 2.** Its `sendfile-calls-helper` gold fact needs `res.sendFile = function
  sendFile` to become a symbol.

## Upgrading note

`verinoda update` is enough, and no `scan` is needed:

- The extractor's source changed and the AST cache schema went up (now 11). So the extraction stamp
  recorded in build_stats.json no longer matches, and the next `update` rebuilds the whole graph,
  even when no file changed. The old AST cache entries are not reused: they live under
  `cache/ast/v<version>-s8/`, which is swept.
- `verinoda scan` rebuilds everything too.
- Most ids stay. The definitions that used to vanish get new `<id>_<6 hex>` ids. Some ids that
  existed before change, or now name a different symbol:
  - When a private twin was declared before its public twin (`__transform` before `_transform` in
    axios, `_Q` before `function q`), the old graph gave the plain id to the private one. Now it
    goes to the public one, and the private one gets `<id>_<hash>`.
  - Java `_x` methods that share an id with `x` were numbered in `x`'s overload family
    (`..._x_2`, `..._x_3`). They now have `<id>_<hash>` and `<id>_<hash>_2`. `x`'s own overloads
    keep `..._x_2` and so on, but a number that used to belong to `_x` can now belong to an
    overload of `x`.

  A record keyed by such a node id (a claim's subject id, a saved selector that is a node id) names
  the other symbol after the rebuild. Evidence anchors pin cited lines by symbol name and
  fingerprint (`verinoda/anchors.py`), not by node id. Records about a merged node were already
  about a mix of two symbols. After the rebuild they stay on the public definition.
- The rebuild record (`rebuild_record.json`) is keyed by a stamp of project_index and index.py, and
  both changed, so it is not reused.
- `python_facts.json` and the Python cross-file cache are keyed by their own code stamps, which did
  not change. They hold no node ids of the split definitions: their call sources are re-routed by
  `case_ids` on each build.
- A process that keeps running across the upgrade, such as an MCP server, sees changed code files
  and stops trusting its rebuild record. Restart it to pick up the new resolver.
- Answers change in one visible way. A selector written as code that matches a symbol only when
  case is folded (`sendFile` for `sendfile`, in a case-sensitive language) is now `similar` with a
  note, or `ambiguous` when it matches several, never `exact`. `trace` shows the note under
  `fuzzy`. The rename preview (`rename_preview`) and the callers/callees view (`butterfly`) run
  only on an exact match. They now return the matched node as a candidate, with the note, instead
  of running.

## Tests

tests/test_distinct_ids.py (new, 9 tests, all with small fixtures):

- `test_javascript_definitions_that_mint_one_id_are_two_nodes_with_their_own_calls`: the axios
  shape is one class with `request` and `_request`, plus `getX` and `getx` at module level, plus an
  importer of `getx` and `_helper`. The test checks:
  - both definitions are nodes, and `request` keeps its old id;
  - `request -calls-> _request` at the right line, and `_request -calls-> mergeConfig` (not
    `request`);
  - the importer's call and import edges land on `getx` and `_helper`, never on their twins;
  - `resolve("_request")` is exact, and `trace request mergeConfig` runs through `_request`.
- `test_python_methods_and_nested_functions_that_mint_one_id_are_two_nodes`:
  - methods `fetch` / `_fetch` (the upstream rule) and nested `step` / `_step` (new) are separate
    nodes, and the call between the nested pair binds;
  - module-level `load` / `_load` across files: the import, the call and the calls made in
    `_load`'s and `_run`'s bodies land on the private twins.
- `test_the_extractor_salts_only_a_different_name`: unit test of `_distinct_def_id`. It covers the
  same name, an underscore difference, a case difference, PHP case folding, an id nobody holds, a
  salted id already taken (a longer hash), and a qualified `Foo::bar` against `bar`.
- `test_the_build_net_splits_names_that_differ_in_underscores_and_keeps_the_public_id`: the
  `case_ids` net in either line order; PHP folds case but not underscores; ids shared by another
  route (`b` / `a_b`, `Foo bar` / `Foo-bar`) are left alone; an `import { _helper }` edge minted
  with the plain id moves to `_helper`.
- Review round:
  - `test_the_public_name_keeps_the_plain_id_whichever_is_declared_first`: `const _config` before
    `function config`, `__a` / `_a` / `a` in that order, and a multi-line import of `_a`;
  - `test_java_overloads_of_a_salted_method_are_numbered_from_its_own_id`: `fetch` / `_fetch`, two
    overloads each, in both declaration orders;
  - `test_the_build_net_reads_an_import_across_its_lines`: the `case_ids` routing of a multi-line
    import, and the copy that is dropped;
  - `test_a_label_that_is_no_qualified_name_is_its_own_symbol_name`.
- `test_a_code_name_matched_only_case_insensitively_is_labelled_and_never_exact`: the express
  shape. `lib/response.js::sendFile` is `similar` with the note, and the exact-case selector and
  the plain word stay `exact`. The `trace` status and `fuzzy.source` match the express output. When
  both `getX` and `getx` exist, each resolves exactly to its own node.

Run: `pytest tests/test_distinct_ids.py tests/test_case_ids.py tests/test_docs.py`.
