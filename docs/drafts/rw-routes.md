# Routes: mount prefixes, trailing slashes, a bounded report (real-world defects 3 and 9, harness H1-H3)

## Why

The real-world run of 2026-10-02 (benchmarks/results/realworld-2026-10-02/defects.md) found two `routes`
defects and three harness defects.

- D3. full-stack-fastapi-template mounts every router with
  `app.include_router(api_router, prefix=settings.API_V1_STR)`. `py_facts` read only a string literal
  there, so the prefix became `""`. The table showed `/login/access-token` instead of
  `/api/v1/login/access-token`, with no note. Two gold facts missed. `@router.get("/")` under
  `prefix="/items"` was shown as `/items`, but FastAPI serves `/items/`. An `APIRouter(prefix=<expr>)`
  lost its prefix the same way, and a `register_blueprint(url_prefix=<expr>)` dropped the mount.
- D9. `routes --json` was 248 KB on express. 880 supertest calls in test/ were ambiguous between the routes of
  unrelated example apps and other test files. 200 of them were listed with 8 candidates each, the rest only
  counted, and nothing showed that the code was test or example code.
- H1. The harness's routes judge compared `method` with a row key that does not exist (rows have `methods`).
- H2. The judge matches `handler` as text of the row, and that text is a node id. The README did not say so.
- H3. summary.md printed one environment line per commit, although the two commits differed only in
  benchmark files.

## Decisions

- **Prefix values.** A prefix that is not a literal goes through `py_pieces`. Its parts resolve in this
  order:
  - a module constant of the same file;
  - a value of the same file listed by `py_values`: a module constant (`NAME = "..."`, also annotated), a
    class attribute default (`Cls.ATTR`), or `obj.ATTR` for a module-level `obj = Cls()`;
  - at link time, a name imported from another module (`from app.core.config import settings`). That
    module is found among the graph's Python files, read, and given to `py_values`. Re-exports are
    followed up to three times.

  `obj.ATTR` counts only when the `Cls()` call passes no positional argument. A literal keyword argument
  wins over the default. `Settings(**kw)` resolves nothing. A name bound more than once has no value.
- **Citing the value.** Every row that used a resolved value carries `prefix_from`, which names where the
  value comes from. For a `BaseSettings` subclass it reads "the class default at config.py:22; the
  environment can set another value at run time". The value is the default, not a value checked at run
  time.
- **A prefix that cannot be resolved.** A call, `os.environ[...]` or a name no module spells still keeps
  its mount and its routes. The row gets `mount: "prefix not resolved: <expr>"`, with the expression as
  written. Several of these are joined with `; `, also together with `not found`. The text view shows
  `(mount prefix not resolved: ...)`. An edge to such a route gets the note "the route's path lacks a
  prefix the text does not spell".
- **Facts version.** Facts carry `prefix_expr`, `prefix_pieces` and `prefix_from`, so `FACTS_VERSION` is
  now 3: older cached facts are parsed again. A JavaScript `app.use(CONST, router)` with a module
  constant is read as well.
- **Trailing slash.** `join_path(..., keep_slash=True)` keeps a trailing `/` of the last part that has
  one. It is used for routes in Python files: FastAPI, Flask and Django serve the path as written, so
  `/items` + `/` is `/items/`. It is not used for JavaScript files, because Express serves a router's `/`
  at the mount path itself. Matching splits on `/` and drops empty segments, so a call to `/items` still
  fits `/items/`. Only the displayed path changed.
- **Labels.** `code_kind(path)` gives `"test"` (`testcode.is_test_file`) or `"example"` (`examples/`,
  `example/`, `samples/`, `demos/`, `docs_src/`, `tutorials/`). It labels route rows, ambiguous calls
  and their candidates, unmatched calls and method mismatches as `code`.
- **Scope of a test or example call.** A call in test or example code, or a supertest call, is matched
  first against its own file and the files it imports. This scope uses relative `import`/`require`
  and the graph's `imports_from` edges, up to three levels deep. When a candidate route is in that scope,
  only those candidates count. When none is and the call is a supertest `request(app)`, the call is
  unmatched with `why: "no route read in the app under test fits (this file and N it imports)"`. This
  applies when the file imports something or builds an app itself. Any other call keeps all candidates.
- **Bounded report.** The linker now keeps every ambiguous call, unmatched call and method mismatch, with
  every candidate, so counts are exact. `cross_service.bounded()` makes the view for `verinoda routes`:
  - ambiguous calls with the same protocol, method, URL and candidate set become one group: the first
    call, `calls`, and up to 5 `also_at`;
  - at most 50 groups (`AMBIGUOUS_GROUPS`) of 5 candidates (`AMBIGUOUS_CANDIDATES`), with
    `candidates_total` when cut;
  - at most 50 unmatched calls and 50 method mismatches (`LIST_CAP`);
  - every cut is counted (`also_at_more`, `<list>_more`), and `ambiguous_calls` plus `<list>_by_code`
    give the totals;
  - a `bounded` line says how to see everything: `routes --all` shows every call and every candidate,
    ungrouped.
- **Sidecar.** The sidecar keeps the first 200 ambiguous calls (`REPORT_CAP`) with 8 candidates each and
  `candidates_total`, as before. Its counts are now the full counts.
- **H1.** `_judge_route` requires the check's `method` in the row's `methods` list. A row for any method
  (`methods: null`) does not count. A miss lists the rows on the same path that differ.
- **H2.** The README (step 3) says the full path includes prefixes and a written trailing `/`, and that
  `handler` is the node id. A new key `at` (the route's declaration file:line) is accepted in its place.
- **H3.** `run.py` records `verinoda_code_tree` (`git rev-parse HEAD:verinoda`). `report.env_lines` merges
  environments that differ only in the commit and have the same `verinoda/` code. The test is the tree id
  when both have one, else `git diff --quiet A B -- verinoda/` in this checkout. A dirty run or a commit
  git does not know is never merged. The committed summary.md of 2026-10-02 was re-rendered: its two
  lines are now "at 8d9ab97 and 1ab4a71 (the same verinoda/ code: the commits differ only in files
  outside verinoda/)".

## Measured

Windows 11, Python 3.13.14, one process at a time. "Before" numbers come from the committed results of
2026-10-02, code 488b553, or from a read-only `routes --json` with that code on the same clone and index.
"After" numbers are from `benchmarks/realworld/run.py --repos <repo>` at 31aef6d, with results in a scratch
folder.

| | before | after |
|---|---:|---:|
| full-stack-fastapi-template gold v1 | 7/10 | 9/10 |
| `route-login-access-token`, `route-read-item` | miss, miss | hit, hit |
| template `routes --json` bytes | 4,402 | 8,420 |
| express gold v1 / v2 | 4/10 / 2/2 | 4/10 / 2/2 |
| express `routes --json` bytes (default) | 247,767 | 70,871 |
| express `routes --json --all` bytes | - | 281,919 |
| express ambiguous calls | 880 (200 listed, 680 counted) | 197 (14 groups) |
| express unmatched calls | 1 | 513 (50 listed) |
| express HTTP edges | 27 | 21 |

- **Template.** All 23 routes now carry `/api/v1`. The 8 paths written with a trailing `/` keep it, for
  example `GET /api/v1/items/` and `POST /api/v1/reset-password/`. I read `items.py:13` (`"/"` under
  `prefix="/items"`), `login.py:77`, `private.py:23` and `utils.py:29` in the clone to confirm. The JSON grew
  because every row now carries its `prefix_from` note.
- **Template links.** `linked` stays 0. The only client call found is `frontend/tests/utils/mailcatcher.ts:16`.
  The generated SDK (`frontend/src/client/sdk.gen.ts`, `url: '/api/v1/...'` inside a request-options
  object) is not read as a client call.
- **Express edges.** 26 edges were removed and 20 added. Each removed edge linked a test call to a route in
  a file the test neither defines nor imports (test/app.param.js, test/app.router.js, test/req.baseUrl.js,
  examples/route-separation/index.js). The 20 added edges link test/acceptance/route-separation.js and
  test/acceptance/vhost.js to the example apps they import, and test/res.format.js to its own routes.
- **Express unmatched.** Most of the 513 unmatched calls are acceptance tests of example apps whose routes are
  not read. For example, examples/auth requires `../../` instead of `express`, so it is not seen as an
  Express app.
- **Express routes.** The route table (286 rows, 45 KB) is the largest part of the bounded output.
- **sqlmodel.** Not re-run with the harness. A read-only `routes --json` on its existing index gave
  214,168 bytes before (committed results) and 50,433 after: 108 ambiguous calls in 19 groups.
- **No regressions.** Both runs had 0 crashes and 0 timeouts, and the clones were clean afterwards.

## Limits

- **Values that are resolved.** Only values the text spells as a string literal are resolved: a constant,
  a class attribute default, or an instance attribute default. These are not resolved:
  - a `get_settings()` call (`@lru_cache` style), `os.environ`, or a computed value. These are marked,
    never guessed;
  - a value assigned in `__init__`, or a settings value set by `.env` or by the environment. The note says
    so for `BaseSettings`;
  - a `Field(default=...)`.
- **A route whose prefix is not resolved can still be linked.** The match uses the path without the
  prefix, and the edge carries a note.
- **The trailing slash in JVM files.** Spring and JAX-RS paths are built in `jvm_facts` with the old
  `join_path` and still drop a trailing `/`. JavaScript files keep the Express convention.
- **The scope of a test call.** The scope follows only relative imports and the graph's `imports_from`
  edges, three levels deep. A test that gets its app from a workspace package (a non-relative import) and
  imports some unrelated local helper is reported as unmatched, not ambiguous. It gets no edge either way.
- **Example apps the scope cannot help.** Calls to example apps whose routes are not read stay unmatched.
  In express, examples that `require('../../')` instead of `express` are not seen as Express apps.
  This is a separate extraction gap.
- **The route table itself is not capped.** It is 286 rows on express. The caps apply to the ambiguous,
  unmatched and method-mismatch lists.
- **The SDK of the template.** The generated SDK's `__request(OpenAPI, {url: ...})` calls are not read as
  clients, so the template's front end still links to no route.

## Upgrading note

`verinoda routes` has these changes:

- **Prefixes.** It now shows FastAPI, Flask and APIRouter prefixes given by an expression:
  - A module constant or a pydantic settings default is resolved, also from another module, and the row
    cites it in `prefix_from`.
  - Any other expression keeps its routes, marked `mount: "prefix not resolved: <expr>"`. Before, the
    prefix was silently dropped, or a `register_blueprint` mount was lost.
  - The text view's `(mount not found)` is now `(mount <reason>)`.
- **Trailing slash.** Python route paths keep the trailing `/` the code writes, so `/api/v1/items/` is no
  longer shown as `/api/v1/items`. Gold files or scripts that compared the path without the slash need
  updating.
- **Labels.** Rows and calls in test or example code have `code: "test"` or `code: "example"`.
- **Test calls.** A test or example call matches the routes of its own file and the files it imports
  first, so fewer calls are ambiguous. A supertest call to an imported app with no matching route is now
  `unmatched` with a `why`.
- **Bounded JSON.** The JSON is bounded by default:
  - `ambiguous` entries are groups (`calls`, `also_at`, `candidates_total`), at most 50 groups of 5
    candidates;
  - `unmatched` and `method_mismatch` hold at most 50 entries each, with `<key>_more` counts;
  - `ambiguous_calls` and `<key>_by_code` hold the totals;
  - `routes --all` restores every call and every candidate.
- **Sidecar counts.** The sidecar's cross-service counts are now exact. Before, they stopped at 200 per
  list.
- **Facts version.** `FACTS_VERSION` is 3, so the first `update` after upgrading parses the route files
  again.
- **Harness.** A routes gold check's `method` now really is checked against `methods`, and `at`
  (file:line) is accepted. summary.md merges environment lines whose `verinoda/` code is the same.

## Tests

- tests/test_routes_prefix.py (new, 23 tests). An indexed project in the template's shape:
  - the settings default is resolved and cited with its file:line;
  - the `/items/` trailing slash is kept and still matched without it;
  - a client links through the resolved prefix, and a call without the prefix is unmatched;
  - `version_prefix()` and `os.environ[...]` give `prefix not resolved` rows;
  - `f"{V2}/x"` with an imported constant resolves;
  - example and test labels are set;
  - a supertest call links to the app it imports, not to the other example app;
  - grouped ambiguous calls and `--all`, and the sidecar's counts and 8-candidate cap.

  Also unit tests of `bounded` (groups, candidates, list caps, the text), `join_path(keep_slash=)`,
  `py_values` (7 cases: rebinding, keyword override, `**kw`) and a Flask `url_prefix` from an imported
  constant.
- tests/test_cross_service.py: one new test. The route table keeps Django's written slash, FastAPI's
  `@router.post("")`, and Express's mount path for a router's `/`. 65 passed.
- tests/test_realworld_harness.py:
  - the routes judge (`methods`, a row for any method, `at`, the exact path);
  - environment lines merged by tree id; not merged for another tree, a dirty run or unknown commits;
    merged by `git diff` for 8d9ab97 and 1ab4a71.

  87 passed, 1 skipped.
- tests/test_docs.py: 19 passed.
