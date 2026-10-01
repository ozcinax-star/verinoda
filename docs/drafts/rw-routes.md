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
  - a value of the same file listed by `py_values`: a module constant (`NAME = "..."`, also annotated), a
    class attribute default (`Cls.ATTR`), or `obj.ATTR` for a module-level `obj = Cls()`;
  - at link time, a name imported from another module (`from app.core.config import settings`). That
    module is found among the graph's Python files, read, and given to `py_values`. Re-exports are
    followed up to three times.

  `obj.ATTR` counts only when the `Cls()` call passes no positional argument. A literal keyword argument
  wins over the default. `Settings(**kw)` resolves nothing. A name bound more than once has no value, and
  a function parameter of that name counts as a binding. An attribute that is assigned anywhere
  (`obj.ATTR = ...`, `Cls.ATTR = ...`, or `self.ATTR = ...` in a method) has no value. A class that
  defines `__init__` gives `obj.ATTR` no value, unless it is a `BaseSettings` subclass. An imported name
  that a parameter or an assignment rebinds is not followed.
- **Citing the value.** Every Python row that used a resolved value carries `prefix_from`, which names
  where the value comes from. This includes a same-file constant. A JavaScript `app.use(CONST, router)`
  constant is resolved without a note. For a `BaseSettings` subclass it reads "the class default at config.py:22; the
  environment can set another value at run time". The value is the default, not a value checked at run
  time.
- **A prefix that cannot be resolved.** A call, `os.environ[...]` or a name no module spells still keeps
  its mount and its routes. The row gets `mount: "prefix not resolved: <expr>"`, with the expression as
  written. Several of these are joined with `; `, also together with `not found`. The text view shows
  `(mount prefix not resolved: ...)`. An edge to such a route gets the note "the route's path lacks a
  prefix the text does not spell".
- **Facts version.** Facts carry `prefix_expr`, `prefix_pieces` and `prefix_from`, so `FACTS_VERSION` is
  now 4 (3 before the review round): older cached facts are parsed again. A JavaScript `app.use(CONST, router)` with a module
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
  first against the app it builds or imports. The scope is the file and its full import closure, through
  relative `import`/`require` and the graph's `imports_from` edges. A route is in scope when any file of
  its mount chain is in scope: its own file, or a file that mounts it, up to the app. When candidates are
  in scope, only those count, and the edge notes how many routes outside also match the path. Two
  exceptions apply:
  - A test-client or example call (not supertest) is not narrowed when a route outside the scope names
    more literal segments than every route inside it (`/items` outside, `/{page}` inside). The call
    stays ambiguous. A Python scope rests on the graph's imports, which can miss a package re-export.
  - A supertest call with no candidate in scope is unmatched, with
    `why: "no route read in the app under test fits (this file and the N it imports); M elsewhere do"`.
    This applies when the file imports something or builds an app itself.

  Any other call keeps all candidates. A mount through a package `__init__.py` that only re-exports the
  router (`from app.api.main import api_router`) is followed, up to three re-exports.
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
  `candidates_total`, as before. Its counts are now the full counts. The report itself keeps every
  candidate, also of an ambiguous tRPC/gRPC/GraphQL call (before, 8, with the cut not counted).
- **A route whose prefix is not resolved.** A call that fits only such routes gets no edge. It is
  unmatched, with `why: "only routes whose prefix is not resolved fit: ..."`. When other routes fit too,
  those are kept and the edge notes how many unresolved ones were set aside.
- **Text view.** A grouped ambiguous call shows `also at <file:line>, ... (+N more)` under its line.
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
- **Review round.** A review of 31aef6d confirmed four defects. All four are fixed:
  - A test call's scope stopped three imports deep. A test whose app mounts a router four imports away
    linked confidently to the app's catch-all `/{page}` instead of staying ambiguous with `/items`. The
    scope is now the full import closure. A route counts as in scope through its mount chain, and a more
    specific route outside keeps a non-supertest call ambiguous.
  - For the same reason, a supertest call to a correctly mounted route four `require`s deep was reported
    unmatched. It now links.
  - `def mount(app, API): app.include_router(r, prefix=API)` took the module constant `API`. A parameter
    now counts as a binding, so the prefix is not resolved.
  - `obj.ATTR` took the class default even when `__init__`, a method or a later `obj.ATTR = ...`
    assignment set another value. These now give no value.

  Several smaller points were also fixed: a same-file constant now gets its `prefix_from` note, RPC
  ambiguous calls are no longer cut at 8, the text view shows `also_at`, and a call that fits only routes
  with an unresolved prefix gets no edge. Two earlier statements here were wrong: "every row that used a
  resolved value carries `prefix_from`" was not true for a same-file constant, and "`--all` lists every
  candidate" was not true for RPC calls. Both statements are now true. One point is not fixed; see Limits.

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

Review round (5e3499f), each repository re-run with `run.py`, and read-only `routes --json` counts with
31aef6d and with 5e3499f on the same clone and index:

| | 31aef6d | 5e3499f |
|---|---:|---:|
| full-stack-fastapi-template gold v1 | 9/10 | 9/10 |
| template `routes --json` bytes (harness) | 8,420 | 8,420 |
| template rows with `prefix_from` / not resolved | 23 / 0 | 23 / 0 |
| express gold v1 / v2 | 4/10 / 2/2 | 4/10 / 2/2 |
| express `routes --json` bytes (default / `--all`) | 70,870 / 281,919 | 73,407 / 294,118 |
| express ambiguous (groups) / unmatched / method mismatch | 197 (14) / 513 / 8 | 197 (14) / 513 / 8 |
| express HTTP edges | 21 | 21, the same 21 |
| sqlmodel gold v1 | 9/10 | 9/10 |
| sqlmodel `routes --json` bytes (default / `--all`) | 50,433 / 265,195 | 51,569 / 266,331 |
| sqlmodel ambiguous (groups) / unmatched / edges | 108 (19) / 12 / 16 | 108 (19) / 12 / 16 |

- **Review round.** On the three repositories, the same calls are linked, ambiguous and unmatched as at
  31aef6d. The output grew because 21 express edges now carry the note "N route(s) outside the app under
  test also match the path", and so do all 16 sqlmodel edges. An intermediate version kept a supertest call
  unless every fitting route had a known mount. On express, that gave 614 ambiguous calls (63 groups) and
  a 2.1 MB `--all`, because 36 example routers have no mount found. It was dropped. A route that the app
  under test serves is declared or mounted in a file of the app's import closure.

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
- **sqlmodel.** Not re-run with the harness at 31aef6d. A read-only `routes --json` on its existing index gave
  214,168 bytes before (committed results) and 50,433 after: 108 ambiguous calls in 19 groups.
- **No regressions.** Both runs had 0 crashes and 0 timeouts, and the clones were clean afterwards.

## Limits

- **Values that are resolved.** Only values the text spells as a string literal are resolved: a constant,
  a class attribute default, or an instance attribute default. These are not resolved:
  - a `get_settings()` call (`@lru_cache` style), `os.environ`, or a computed value. These are marked,
    never guessed;
  - a value assigned in `__init__` or another method, or after the class (`obj.ATTR = ...`), and any
    `obj.ATTR` of a class with `__init__` (except `BaseSettings`), or a settings value set by `.env` or by
    the environment. The note says so for `BaseSettings`;
  - a `Field(default=...)`.
- **Bindings are counted per module, not per scope.** A parameter or a local variable with a constant's
  name, anywhere in the module, also unresolves a module-level use of that constant. This is
  conservative: the value is marked "prefix not resolved", never wrong. `setattr(...)` and
  `self.__dict__` writes are not seen. A class with `__init__` already gives no `obj.ATTR`.
- **A route whose prefix is not resolved is not linked alone.** It still matches by the path without the
  prefix. When it is the only fit, the call is unmatched with a `why`.
- **The trailing slash in JVM files.** Spring and JAX-RS paths are built in `jvm_facts` with the old
  `join_path` and still drop a trailing `/`. JavaScript files keep the Express convention.
- **The scope of a test call.** The scope follows only relative JavaScript imports and the graph's
  `imports_from` edges, which can miss a Python package re-export. A test that gets its app from a
  workspace package (a non-relative import) and imports some unrelated local helper is reported as
  unmatched, not ambiguous. It gets no edge either way. Routes loaded at run time (an
  `fs.readdirSync` loader) are not in any scope, so a supertest call to them is unmatched.
- **The harness environment merge.** `report.same_code()` runs `git diff` in the checkout that renders
  the summary. When that checkout knows neither commit, it falls back to separate lines without saying so.
  The merge also ignores changes outside `verinoda/` that can change behaviour, such as dependency pins in
  pyproject. Not changed in the review round.
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
- **Test calls.** A test or example call matches the routes of the app it imports first (the full
  import closure, and routes mounted on it), so fewer calls are ambiguous. Such an edge notes how many
  routes outside also match the path. A supertest call to an imported app with no matching route is now
  `unmatched` with a `why`.
- **Unresolved prefixes.** A call that fits only routes whose prefix is not resolved is now unmatched,
  with a `why`. Before, it was linked with a note.
- **Bounded JSON.** The JSON is bounded by default:
  - `ambiguous` entries are groups (`calls`, `also_at`, `candidates_total`), at most 50 groups of 5
    candidates;
  - `unmatched` and `method_mismatch` hold at most 50 entries each, with `<key>_more` counts;
  - `ambiguous_calls` and `<key>_by_code` hold the totals;
  - `routes --all` restores every call and every candidate.
- **Sidecar counts.** The sidecar's cross-service counts are now exact. Before, they stopped at 200 per
  list.
- **Facts version.** `FACTS_VERSION` is 4, so the first `update` after upgrading parses the route files
  again.
- **Harness.** A routes gold check's `method` now really is checked against `methods`, and `at`
  (file:line) is accepted. summary.md merges environment lines whose `verinoda/` code is the same.

## Tests

- tests/test_routes_prefix.py (new, 41 tests). An indexed project in the template's shape:
  - the settings default is resolved and cited with its file:line;
  - the `/items/` trailing slash is kept and still matched without it;
  - a client links through the resolved prefix, and a call without the prefix is unmatched;
  - `version_prefix()` and `os.environ[...]` give `prefix not resolved` rows;
  - `f"{V2}/x"` with an imported constant resolves;
  - example and test labels are set;
  - a supertest call links to the app it imports, not to the other example app;
  - grouped ambiguous calls and `--all`, and the sidecar's counts and 8-candidate cap.

  Also unit tests of `bounded` (groups, candidates, list caps, the text), `join_path(keep_slash=)`,
  `py_values` (16 cases: rebinding, keyword override, `**kw`, a shadowing parameter, `self.ATTR` in
  `__init__`, an `__init__` that may set anything, `obj.ATTR =` and `Cls.ATTR =` later) and a Flask
  `url_prefix` from an imported constant.

  Review round, a second indexed project:
  - a FastAPI test whose router is four imports away stays ambiguous between `/items` and `/{page}`;
  - an example test whose app has only `/{page}` stays ambiguous when another example names `/orders`;
  - a supertest call links through test, server, app, routes/index and routes/users;
  - a Flask call that fits only a route with an unresolved prefix is unmatched with a `why`.

  Also: a parameter that shadows a constant or an import, attribute overrides, a cited same-file
  constant, RPC candidates uncut, and `also at` in the text view.
- Touched files together: test_routes_prefix.py, test_cross_service.py and test_realworld_harness.py, 193
  passed and 1 skipped (41 + 65 + 87 and 1 skipped).
- tests/test_cross_service.py: one new test. The route table keeps Django's written slash, FastAPI's
  `@router.post("")`, and Express's mount path for a router's `/`. 65 passed.
- tests/test_realworld_harness.py:
  - the routes judge (`methods`, a row for any method, `at`, the exact path);
  - environment lines merged by tree id; not merged for another tree, a dirty run or unknown commits;
    merged by `git diff` for 8d9ab97 and 1ab4a71.

  87 passed, 1 skipped.
- tests/test_docs.py: 19 passed.
